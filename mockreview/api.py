"""The operations behind the CLI and the Hermes plugin tools: one code path, plain dicts in and out.

Errors are ReviewError (message safe to show). Settings resolve per call: an argument here is the per-review
override, else env > config.yaml > default (settings.py).
"""

from __future__ import annotations

import calendar
import functools
import json
import logging
import shutil
import time
import urllib.request
from typing import Any, Dict, List, Optional

from . import config, feedback, herenow, locks, owner, service, settings, store
from .store import ReviewError

log = logging.getLogger("mockreview.api")

MODE_OF_HOST = {"tailnet": "tailscale", "local": "local", "herenow": "herenow"}


def _settings_error(e: settings.SettingsError) -> ReviewError:
    return ReviewError(str(e), 400)


def server_up() -> bool:
    try:
        with urllib.request.urlopen(config.local_base() + "/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def resolve_mode(mode: Optional[str]) -> str:
    if mode in store.MODES:
        return mode
    try:
        host = settings.get("default_host", mode)
    except settings.SettingsError:
        raise ReviewError(f"mode must be tailnet (tailscale), local or herenow, got {mode!r}")
    return MODE_OF_HOST[host]


def check_live_demo(spec: Dict[str, Any], allow: Optional[bool]) -> None:
    """A prototype ("try it live") round needs offer_live_demo, or this call's allow_live_demo."""
    if spec.get("kind") != "prototype":
        return
    if not settings.get("offer_live_demo", allow):
        raise ReviewError(
            "A prototype (\"try it live\") round is turned off: the offer_live_demo setting is false. Turn it on "
            "(plugins.entries.mock-review.settings.offer_live_demo: true) or allow it for this call "
            "(--allow-live-demo / allow_live_demo: true).", 403)


def _spec(decisions: Optional[str], prototype: Optional[str], design: Optional[str], allow: Optional[bool]):
    spec = store.parse_round_spec(decisions=decisions, prototype=prototype, design=design)
    check_live_demo(spec, allow)
    return spec


def _owned(fn):
    """Run an operation against the store the board server owns when this process's own store is not watched."""
    @functools.wraps(fn)
    def run(*a: Any, **kw: Any):
        with owner.bound(owner.resolve_soft()):
            return fn(*a, **kw)
    return run


def create(title: str, origin: Optional[Dict[str, str]] = None, brief: str = "", mode: Optional[str] = None,
           decisions: Optional[str] = None,
           prototype: Optional[str] = None, design: Optional[str] = None, allow_live_demo: Optional[bool] = None,
           wake: Optional[bool] = None, password: Optional[bool] = None, **look: Any) -> Dict[str, Any]:
    """Create a review; `look` = per-review overrides (devices, default_device, default_theme, ...).

    Preflight, all before anything is published: the owning board server is found (or this process's store is the
    one it watches), its wake route works (unless wake is off), and after the board is written the server can load
    it. Any failure raises and leaves nothing behind.
    """
    unknown = set(look) - set(settings.REVIEW_KEYS)
    if unknown:
        raise ReviewError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    mode = resolve_mode(mode)
    try:
        stored = settings.review_settings(look)
        want_wake = bool(settings.get("wake_required", wake))
        want_password = settings.get("herenow_password", password)
    except settings.SettingsError as e:
        raise _settings_error(e)
    origin = store.normalize_origin(origin, wake=want_wake)
    spec = _spec(decisions, prototype, design, allow_live_demo)
    ctx = owner.resolve()
    if want_wake:
        ctx.require_wake()
    if mode == "herenow" and ctx.state == "unreachable":
        raise owner.OwnerError(f"refusing to publish a public board: {ctx.why}. Nothing would poll its feedback.", 503)
    with owner.bound(ctx):
        data = store.create_review(title, brief, origin, mode, spec, look=stored, wake=want_wake, password=want_password)
        if not owner.confirm_visible(data["id"], ctx):
            owner.discard_created(data["id"])
            raise owner.OwnerError(
                f"the board server on {config.local_base()} could not load the review it was just given "
                f"(store {ctx.data_root}); it was removed and nothing was published.", 502)
        res: Dict[str, Any] = {"review_id": data["id"], "url": None, "mode": data["mode"], "round": 1,
                               "settings": stored, "owner": ctx.as_dict()}
        if not want_wake:
            res["wake"] = False
        if mode == "herenow":
            try:
                data = herenow.publish(data["id"])
            except ReviewError as e:
                raise ReviewError(f"review {data['id']} was created but publishing failed: {e.message} "
                                  f"— retry with `mockreview publish {data['id']}`", e.status)
        else:
            res["server_up"] = server_up()
            if not res["server_up"]:
                res["warning"] = (f"mock-review server is not answering on {config.local_base()} — "
                                  "the link won't load until it runs")
            elif mode == "local" and not config.lan_shared():
                res["note"] = ("local link opens on this computer only: the server listens on loopback. Set "
                               "plugins.entries.mock-review.settings.host to 0.0.0.0 (and restart it) to open it from "
                               "other devices on your network.")
        res.update(herenow.password_result(data))
        res["url"] = store.review_url(data)
    return res


def add_round(review_id: str, decisions: Optional[str] = None, prototype: Optional[str] = None,
              design: Optional[str] = None, allow_live_demo: Optional[bool] = None, **look: Any) -> Dict[str, Any]:
    unknown = set(look) - set(settings.REVIEW_KEYS)
    if unknown:
        raise ReviewError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    spec = _spec(decisions, prototype, design, allow_live_demo)
    ctx = owner.resolve_soft()
    with owner.bound(ctx):
        existing = store.load(review_id)
        if settings.get("wake_required") and existing.get("wake", True) is not False:
            ctx.require_wake()
        if existing["mode"] == "herenow" and ctx.state == "unreachable":
            raise owner.OwnerError(f"refusing to republish a public board: {ctx.why}.", 503)
        new_look = None
        if any(v is not None for v in look.values()):
            base = existing.get("settings")
            try:
                new_look = settings.review_settings(look, base=base or settings.review_settings())
            except settings.SettingsError as e:
                raise _settings_error(e)
        data = store.add_round(review_id, spec, look=new_look)
        if data["mode"] == "herenow":
            data = herenow.publish(review_id)
        return {"review_id": review_id, "round": data["rounds"][-1]["n"], "url": store.review_url(data),
                "mode": data["mode"], "owner": ctx.as_dict(), **herenow.password_result(data)}


@_owned
def get_feedback(review_id: str, round_n: Optional[int] = None) -> Dict[str, Any]:
    data = store.load(review_id)
    r = store.get_round(data, round_n)
    return {"review_id": data["id"], "title": data["title"], "round": r["n"], "status": data["status"],
            "kind": r.get("kind", "variants"), "decisions": store.round_index(r),
            "submission": r.get("submission"), "feedback": r.get("feedback"),
            "feedback_path": service.feedback_path(data["id"], r["n"]) if r.get("feedback") else None,
            "markdown": feedback.render_markdown(data, r)}


@_owned
def list_reviews(include_closed: bool = False) -> List[Dict[str, Any]]:
    rows = []
    for rid in store.list_ids():
        try:
            d = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        if not include_closed and d["status"] != "open":
            continue
        cur = d["rounds"][-1]
        rows.append({"review_id": rid, "title": d["title"], "mode": d["mode"], "status": d["status"],
                     "rounds": len(d["rounds"]), "origin": d.get("origin", {}),
                     "thread_id": (d.get("origin") or {}).get("thread_id"),
                     "current_round_feedback": (cur.get("submission") or {}).get("status", "waiting"),
                     "url": store.review_url(d),
                     "password_status": (d.get("herenow") or {}).get("password_status", "legacy"),
                     "updated_at": d.get("updated_at")})
    return rows


@_owned
def review_link(review_id: str) -> Dict[str, Any]:
    d = store.load(review_id)
    return {"review_id": review_id, "title": d["title"], "status": d["status"], "mode": d["mode"],
            "url": store.review_url(d)}


@_owned
def password(review_id: str, enabled: bool) -> Dict[str, Any]:
    data = herenow.set_password(review_id, enabled)
    return {"review_id": review_id, "url": store.review_url(data), **herenow.password_result(data)}


@_owned
def close(review_id: str) -> Dict[str, Any]:
    """Close a review. here.now: final poll, archive the Site Data records, delete the public site."""
    store.load(review_id)
    with locks.exclusive(store.review_dir(review_id) / ".publish.lock"):
        return _close(review_id)


def _close(review_id: str) -> Dict[str, Any]:
    data = store.load(review_id)
    res: Dict[str, Any] = {"review_id": review_id, "mode": data["mode"]}
    if data["mode"] == "herenow" and (data.get("herenow") or {}).get("slug") and data["status"] == "open":
        slug = data["herenow"]["slug"]
        res["final_poll"] = herenow.poll_review(review_id, republish=False)
        records = herenow.fetch_records(slug)
        store.atomic_write(store.review_dir(review_id) / "archive" / "sitedata-records.json",
                           json.dumps(records, indent=2, ensure_ascii=False))
        res["archived_records"] = len(records)
        res["site_deleted"] = herenow.delete_site(slug)
    with store.locked(review_id):
        data = store.load(review_id)
        data["status"] = "closed"
        data["closed_at"] = store.now_iso()
        if data["mode"] == "herenow" and data.get("herenow"):
            data["herenow"]["deleted_at"] = store.now_iso()
        store.save(review_id, data)
    res["status"] = "closed"
    return res


# ---------------------------------------------------------------- housekeeping (the server's worker runs this)

def _ts(iso: Any) -> float:
    try:
        return float(calendar.timegm(time.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def last_activity(data: Dict[str, Any]) -> float:
    """Newest of: review created, a round added, feedback submitted. (updated_at moves on every here.now poll.)"""
    times = [_ts(data.get("created_at"))]
    for r in data.get("rounds") or []:
        times.append(_ts(r.get("created_at")))
        times.append(_ts((r.get("submission") or {}).get("submitted_at")))
    return max(times)


def housekeeping(now: Optional[float] = None) -> Dict[str, Any]:
    """auto_close_idle_days: close idle open reviews (here.now sites get deleted via close()).
    keep_closed_days: delete closed reviews that closed longer ago than that. 0 = off for either."""
    now = time.time() if now is None else now
    idle_days, keep_days = settings.get("auto_close_idle_days"), settings.get("keep_closed_days")
    out: Dict[str, Any] = {"closed": [], "pruned": [], "errors": []}
    if not idle_days and not keep_days:
        return out
    for rid in store.list_ids():
        try:
            data = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        try:
            if idle_days and data.get("status") == "open" and now - last_activity(data) > idle_days * 86400:
                close(rid)
                out["closed"].append(rid)
                log.info("auto-closed %s (idle more than %s days)", rid, idle_days)
            elif keep_days and data.get("status") == "closed" and data.get("closed_at") \
                    and now - _ts(data["closed_at"]) > keep_days * 86400:
                d = store.review_dir(rid)
                with store.locked(rid):
                    shutil.rmtree(d, ignore_errors=True)
                # Windows keeps the open .lock file (and its directory) until the lock closes.
                if d.exists():
                    shutil.rmtree(d)
                out["pruned"].append(rid)
                log.info("pruned %s (closed more than %s days ago)", rid, keep_days)
        except Exception as e:  # one bad review must not stop the pass
            out["errors"].append({"review_id": rid, "error": getattr(e, "message", None) or str(e)})
            log.error("housekeeping of %s failed: %s", rid, e)
    return out
