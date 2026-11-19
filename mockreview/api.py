"""The operations behind the CLI and the Hermes plugin tools: one code path, plain dicts in and out.

Errors are ReviewError (message safe to show). Settings resolve per call: an argument here is the per-review
override, else env > config.yaml > default (settings.py).
"""

from __future__ import annotations

import base64
import calendar
import functools
import json
import logging
import re
import shutil
import time
import urllib.request
from typing import Any, Dict, List, Optional

from . import brand, bundle, config, export, feedback, owner, service, settings, store, targets, users, wakeguard
from .store import ReviewError

log = logging.getLogger("mockreview.api")

MODE_OF_HOST = {"tailnet": "tailscale", "local": "local", "public": "public"}


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
        raise ReviewError(f"mode must be tailnet (tailscale), local or public, got {mode!r}")
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
    return bundle.bundle_artifacts(spec)   # an artifact's media goes inline, on every board: its page can load nothing else


def _brand_round(spec: Dict[str, Any], project: Optional[str], no_design: bool, reason: Optional[str],
                 review: Optional[Dict[str, Any]] = None) -> Any:
    """Settle which design profile a round wears and, when one does, check the mocks against it. Runs before anything
    is written. Order: the explicit design, the review's own (add-round), the project's bound profile, a review made
    with no_design, then refuse when saved profiles exist. Returns (spec, extra result keys)."""
    extra: Dict[str, Any] = {}
    d = spec.get("design")
    if not d and review is not None:
        d = store.inherited_design(review["id"], review)    # store.add_round applies it
    if not d:
        name = brand.project_profile(project) if project else None
        if name:
            d = store.design_dir(name)
            spec = dict(spec, design=d)
            extra["design_auto"] = name
        elif not (no_design or (review or {}).get("no_design")):
            names = store.saved_design_names()
            if names:
                raise ReviewError(brand.refusal(project, names))
    if no_design:
        spec = dict(spec, no_design=True)
    if reason:
        spec = dict(spec, off_brand=reason)
        extra["off_brand"] = reason
    elif d and spec.get("kind") == "decisions":
        brand.enforce(spec["decisions"], d)    # prototype rounds are whole apps: not scanned
    return spec, extra


def _brand_args(design: Optional[str], project: Any, no_design: Any, off_brand: Any):
    """Validate the three brand arguments up front: (project, no_design, reason)."""
    reason = brand.clean_off_brand(off_brand)
    if design and no_design:
        raise ReviewError("design and no_design contradict each other: pass one")
    return brand.clean_project(project), bool(no_design), reason


def check_wake_toolsets(mode: str, wake: bool) -> None:
    """Refuse a board that wakes Hermes while wake_toolsets grants the woken session tools and the way to its Send button
    is not gated: any board while the server listens beyond loopback. Every board of a platform wakes the same route, so
    its toolsets apply to that text too. See wakeguard."""
    reason = wakeguard.exposure(mode, wake)
    if reason:
        raise ReviewError(wakeguard.message(reason), 409)


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
           wake: Optional[bool] = None, project: Optional[str] = None,
           no_design: Optional[bool] = None, off_brand: Optional[str] = None, **look: Any) -> Dict[str, Any]:
    """Create a review; `look` = per-review overrides (devices, default_device, default_theme, ...).

    Design: `design`, else the profile bound to `project` (an absolute path; design_auto in the result), else refused
    when saved profiles exist unless `no_design`. Decision-round mocks must stay inside the profile unless `off_brand`
    carries a reason (see brand.py).

    Preflight, all before anything is published: the owning board server is found (or this process's store is the
    one it watches), its wake route works (unless wake is off), and after the board is written the server can load
    it. Any failure raises and leaves nothing behind.

    The result carries a `note` while the owner has never signed in from another device and this review is reachable
    off this computer (see sign_in_note): the agent hands that link over, because a tailnet or public board answers a
    device that has not signed in with the sign-in page, not the board.
    """
    unknown = set(look) - set(settings.REVIEW_KEYS)
    if unknown:
        raise ReviewError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    project, no_design, reason = _brand_args(design, project, no_design, off_brand)
    mode = resolve_mode(mode)
    try:
        stored = settings.review_settings(look)
        want_wake = bool(settings.get("wake_required", wake))
        _pin_fix, pin_fix_source, _notes = settings.resolve_one("pin_fix", look.get("pin_fix"))
    except settings.SettingsError as e:
        raise _settings_error(e)
    if pin_fix_source == "default":
        # A board that wakes Hermes can be patched in place from a comment; a wake-off board cannot ask anyone to,
        # so the patch path stays off there. An explicit pin_fix — as a call argument, an env var or a config value —
        # wins either way, including a config-level true that survives wake=false.
        stored["pin_fix"] = want_wake
    check_wake_toolsets(mode, want_wake)
    origin = store.normalize_origin(origin, wake=want_wake)
    spec = _spec(decisions, prototype, design, allow_live_demo)
    ctx = owner.resolve()
    if want_wake:
        ctx.require_wake()
    with owner.bound(ctx):
        check_wake_toolsets(mode, want_wake)   # again once bound: the descriptor is the server's, not this store's
        spec, extra = _brand_round(spec, project, no_design, reason)
        data = store.create_review(title, brief, origin, mode, spec, look=stored, wake=want_wake)
        if not owner.confirm_visible(data["id"], ctx):
            owner.discard_created(data["id"])
            raise owner.OwnerError(
                f"the board server on {config.local_base()} could not load the review it was just given "
                f"(store {ctx.data_root}); it was removed and nothing was published.", 502)
        res: Dict[str, Any] = {"review_id": data["id"], "url": None, "mode": data["mode"], "round": 1,
                               "settings": stored, "owner": ctx.as_dict(), **extra}
        if not want_wake:
            res["wake"] = False
        res["server_up"] = server_up()
        if not res["server_up"]:
            res["warning"] = (f"mock-review server is not answering on {config.local_base()} — "
                              "the link won't load until it runs")
        elif mode == "local" and not config.lan_shared():
            res["note"] = ("local link opens on this computer only: the server listens on loopback. Set "
                           "plugins.entries.mock-review.settings.host to 0.0.0.0 (and restart it) to open it from "
                           "other devices on your network.")
        elif mode == "public" and not config.public_url() and not config.funnel_url():
            res["note"] = ("public mode has no public URL yet: run `hermes mockreview public on`, or set "
                           "plugins.entries.mock-review.settings.public_url")
        res["url"] = store.review_url(data)
        res.update(_interactive_hint(spec, stored))
        _add_note(res, sign_in_note(data["mode"]))
    return res


SIGN_IN_NOTE = "Sign in once per device (works once, for 15 minutes): {url}"


def login_link(public: bool = False) -> Dict[str, Any]:
    """Mint a one-time owner sign-in link: the same call `hermes mockreview login` makes, in one dict.

    `url` is this computer's address unless `public`, which prefers the public URL when the board has one
    (`public_url`, else the Tailscale Funnel URL, else the tailnet base). `public_url` is always included when it
    exists, so a caller can choose.
    """
    link = users.create_login_link("cli")
    out: Dict[str, Any] = {"path": link["path"], "user_id": link["user_id"], "expires_at": link["expires_at"],
                           "url": config.local_base() + link["path"]}
    base = config.public_link_base()
    if base:
        out["public_url"] = base + link["path"]
        if public:
            out["url"] = out["public_url"]
    return out


def sign_in_note(mode: str) -> Optional[str]:
    """The first-run sign-in note for a review `mode`, or None.

    Two things must both hold. The review is reachable from another device: `mode` is tailnet or public (a local
    review only opens on this computer, where loopback is already the owner). And the owner has not signed in from
    another device yet; the stored signal for that is `users.owner_signed_in_elsewhere` (a `login_link.use` audit
    row). The note is only produced when a public base exists, because a loopback link is useless off this computer.
    The link is freshly minted, so the agent can hand it over as-is.
    """
    if mode not in ("tailscale", "public") or users.owner_signed_in_elsewhere():
        return None
    url = login_link(public=True).get("public_url")
    if not url:
        return None
    return SIGN_IN_NOTE.format(url=url)


def _add_note(res: Dict[str, Any], note: Optional[str]) -> None:
    """Append to a result's `note`, keeping whatever a caller already put there (create's local and public hints)."""
    if note:
        res["note"] = f"{res['note']} {note}" if res.get("note") else note


SEND_NEXT_ROUND = ("The next round asks only what is still open: blocks marked Change (with `revised`), skipped or "
                   "unanswered ones, and anything new. Do not ask approved blocks or answered questions again: list "
                   "them in a `settled` block. mock_review_add_round refuses a round that re-asks them.")

_CONTROL = re.compile(r"<(select|details|input|textarea)\b", re.I)
INTERACTIVE_HINT = ("interactive_elements is on, but no mock in this round has a working control (select, details, "
                    "input, textarea). Build the controls the mocks show so they respond, or create the review with "
                    "interactive_elements off for static mocks.")


def _interactive_hint(spec: Dict[str, Any], look: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """{"hint": ...} for a decisions round whose mocks are all static while the review asks for working controls. An
    artifact counts as interactive: it runs script."""
    if not (look or {}).get("interactive_elements") or not isinstance(spec.get("decisions"), dict):
        return {}
    if any(b.get("type") == "artifact" for b in spec["decisions"].get("blocks") or []):
        return {}
    html = [o.get("html") or "" for b in spec["decisions"].get("blocks") or [] for o in [b] + list(b.get("options") or [])]
    if not any(html) or any(_CONTROL.search(x) for x in html):
        return {}
    return {"hint": INTERACTIVE_HINT}


def reasked(prev: Dict[str, Any], spec: Dict[str, Any]) -> List[str]:
    """Ids the reviewer settled in `prev` (approved, or a question answered) that `spec` asks again unchanged: the
    same id, markable, with no `revised`. A markable recap is exempt: every round may ask "is this record right?".
    So is the round after a Build. A share-link guest settles nothing for the agent: its marks are owner-only data,
    so a guest's round never makes the next one refuse to ask again."""
    if store.guest_submission(prev):
        return []
    fb = prev.get("feedback") or {}
    marks = fb.get("marks") or {}
    # After Build the next round is the result, which may show the built pieces under their own ids.
    if not isinstance(spec.get("decisions"), dict) or not marks or fb.get("action") == "build":
        return []
    out = []
    for e in store.decision_index(spec["decisions"]):
        m = marks.get(e["id"]) or {}
        if store.entry_kind(e) != "recap" and not e.get("revised") and (m.get("status") == "approve" or m.get("choice")):
            out.append(e["id"])
    return out


def _reask_error(ids: List[str], n: int) -> ReviewError:
    shown = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
    return ReviewError(
        f"this round asks again what round {n} already settled: {shown}. Only blocks marked Change, skipped or left "
        "open come back. Move the settled ones into a `settled` block (context, no controls), or give a block you "
        "really changed a `revised` badge: a short string such as `Revised`, not `true`. To ask them again on "
        "purpose, pass allow_reask.", 409)


def add_round(review_id: str, decisions: Optional[str] = None, prototype: Optional[str] = None,
              design: Optional[str] = None, allow_live_demo: Optional[bool] = None, project: Optional[str] = None,
              no_design: Optional[bool] = None, off_brand: Optional[str] = None, allow_reask: Optional[bool] = None,
              **look: Any) -> Dict[str, Any]:
    """Add the next round. Design: `design`, else the review's own, else the profile bound to `project`, else refused
    when saved profiles exist (unless `no_design`, or the review was created with it). See create().
    A round that asks again what the last round settled is refused unless `allow_reask`."""
    unknown = set(look) - set(settings.REVIEW_KEYS)
    if unknown:
        raise ReviewError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    project, no_design, reason = _brand_args(design, project, no_design, off_brand)
    spec = _spec(decisions, prototype, design, allow_live_demo)
    ctx = owner.resolve_soft()
    with owner.bound(ctx):
        existing = store.load(review_id)
        check_wake_toolsets(existing["mode"], existing.get("wake", True) is not False)
        if settings.get("wake_required") and existing.get("wake", True) is not False:
            ctx.require_wake()
        new_look = None
        if any(v is not None for v in look.values()):
            base = existing.get("settings")
            try:
                new_look = settings.review_settings(look, base=base or settings.review_settings())
            except settings.SettingsError as e:
                raise _settings_error(e)
        spec, extra = _brand_round(spec, project, no_design, reason, review=existing)
        if not allow_reask and existing.get("rounds"):
            again = reasked(existing["rounds"][-1], spec)
            if again:
                raise _reask_error(again, existing["rounds"][-1]["n"])
        data = store.add_round(review_id, spec, look=new_look)
        _supersede_pin_fixes(review_id, data["rounds"][-1]["n"])
        data = store.load(review_id)
        res = {"review_id": review_id, "round": data["rounds"][-1]["n"], "url": store.review_url(data),
               "mode": data["mode"], "owner": ctx.as_dict(), **extra,
               **_interactive_hint(spec, new_look or existing.get("settings"))}
        _add_note(res, sign_in_note(data["mode"]))
        return res


NO_HTML_REFUSAL = ("a pin fix edits a block that has HTML (a mock, a decision option, a piece or an artifact); "
                   "this pin is on {ref}")


def _supersede_pin_fixes(review_id: str, n: int) -> None:
    """A new round supersedes every open pin fix on the previous one, and terminalises its wake."""
    with store.locked(review_id):
        data = store.load(review_id)
        prev = None
        for r in data["rounds"]:
            if r["n"] == n - 1:
                prev = r
                break
        if prev is None:
            return
        changed = False
        now = store.now_iso()
        for q in (prev.get("pin_requests") or {}).values():
            if q.get("status") in service.PIN_TERMINAL:
                continue
            q.update(status="done", detail=f"superseded by round {n}", done_at=now)
            changed = True
        state = prev.get("pin_fix") or {}
        wake = state.get("wake") or {}
        if wake.get("status") == "pending_notify":
            wake.update(status="failed", sending_until=0.0, next_attempt=0.0,
                        last_error=f"superseded by round {n}")
            state["wake"] = wake
            prev["pin_fix"] = state
            changed = True
        if changed:
            store.save(review_id, data)


@_owned
def request_pin_fix(review_id: str, round_n: Any, pin: Any, token: Any, idem: Any = None,
                    do_notify: bool = True, requester: Optional[str] = None) -> Dict[str, Any]:
    """Store a reviewer pin fix for the tool surfaces; bound to a CLI/tool it stores and sends nothing."""
    return service.request_pin_fix(review_id, round_n, pin, token, idem=idem, do_notify=do_notify,
                                   requester=requester)


def _patch_int(value: Any, name: str) -> int:
    """An int or a decimal string; a bool is never a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ReviewError(f"{name} must be an integer", 400)
    if isinstance(value, str):
        if not value.isdecimal():
            raise ReviewError(f"{name} must be an integer", 400)
        return int(value)
    return value


def _request_matches(request: Dict[str, Any], block: str, option: Optional[str]) -> bool:
    """A request may be closed by a patch of its own block: a block-level request by any option patch of that block,
    an option request only by its own option."""
    return request.get("block") == block and (request.get("option") is None or request.get("option") == option)


@_owned
def patch_block(review_id: str, block: str, html: str, option: Optional[str] = None,
                round_n: Optional[int] = None, if_rev: Optional[int] = None,
                request_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Replace one block's HTML in the newest round, in place.

    Only HTML changes: a block's id, type, title and option ids stay. An artifact page goes to blocks/<id>.html;
    every other block's html is re-stamped and decisions.json is rewritten. The replaced content is kept under
    rounds/<n>/history/ (never overwritten). Cited pin fix requests become done; without request_ids, every open
    request on that block (whose option matches) is closed.
    """
    with store.locked(review_id):
        data = store.load(review_id)
        if data["status"] != "open":
            raise ReviewError("this review is closed — a block can no longer be patched", 409)
        current = data["rounds"][-1]
        n = current["n"]
        if round_n is None:
            raise ReviewError(
                "patch_block needs the round you read: pass round=<n> (mock_review_feedback gives it)", 400)
        want_round = _patch_int(round_n, "round")
        if want_round != n:
            raise ReviewError(
                f"round {want_round} is not the current round ({n}): a newer round was published — "
                f"read round {n} again and merge", 409)
        if if_rev is None:
            raise ReviewError("patch_block needs if_rev: the block revision from the round you read", 400)
        want = _patch_int(if_rev, "if_rev")
        path = store.safe_variant_file(review_id, str(n), "a", "decisions.json")
        try:
            dec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ReviewError(f"round {n} decisions.json cannot be read: {e}", 409)
        blocks = dec.get("blocks") if isinstance(dec, dict) else None
        if not isinstance(blocks, list):
            raise ReviewError(f"round {n} has no decisions blocks", 409)
        target = next((b for b in blocks if b.get("id") == block), None)
        if target is None:
            raise ReviewError(f"block {block!r} is not in round {n}", 404)
        btype = target.get("type")
        options = target.get("options") if btype == "decision" else None
        option = option if isinstance(option, str) and option else None
        if options:
            oids = [o.get("id") for o in options]
            if option is None:
                raise ReviewError("name the option to patch (" + ", ".join(oids) + ")", 409)
            if option not in oids:
                raise ReviewError(f"block {block} has no option {option!r} (" + ", ".join(oids) + ")", 409)
        elif option is not None:
            raise ReviewError(f"block {block} has no options; patch it without an option", 409)
        if not isinstance(html, str) or not html.strip():
            raise ReviewError("html must be a non-empty string", 400)
        if len(html.encode("utf-8")) > store.MAX_ARTIFACT_HTML:
            raise ReviewError(f"html is larger than {store.MAX_ARTIFACT_HTML // (1024 * 1024)} MB", 400)
        revs = current.get("blocks_rev")
        if not isinstance(revs, dict):
            revs = current["blocks_rev"] = {}
        old_rev = int(revs.get(block, 0) or 0)
        if want != old_rev:
            raise ReviewError(
                f"this block changed since you read it (revision {want}, now {old_rev}): "
                "read the round again and merge", 409)
        if btype == "artifact":
            if not target.get("file"):
                raise ReviewError(NO_HTML_REFUSAL.format(ref=block), 409)
            old_file = store.safe_variant_file(review_id, str(n), "a", str(target["file"]))
            old_html = old_file.read_text(encoding="utf-8")
        elif btype in ("mock", "piece") and isinstance(target.get("html"), str):
            old_file, old_html = None, target["html"]
        elif btype == "decision":
            old_file = None
            old_html = next((o.get("html") or "" for o in options if o.get("id") == option), "")
        else:
            raise ReviewError(NO_HTML_REFUSAL.format(ref=block), 409)
        requests = current.get("pin_requests") or {}
        cited: Optional[List[str]] = None
        if request_ids is not None:
            if not isinstance(request_ids, list):
                raise ReviewError("request_ids must be a list of pin fix ids", 400)
            cited = [str(x) for x in request_ids]
            unknown = [i for i in cited if i not in requests]
            if unknown:
                raise ReviewError("unknown pin fix request(s): " + ", ".join(unknown), 409)
            mismatched = [i for i in cited if not _request_matches(requests[i], block, option)]
            if mismatched:
                raise ReviewError("pin fix request(s) " + ", ".join(mismatched)
                                  + f" belong to another block or option: this patch changes {block}", 409)
        new_rev = old_rev + 1
        hist_dir = store.review_dir(review_id) / "rounds" / str(n) / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        name = f"{old_rev}-{block}" + (f"-{option}" if option else "")
        hist = hist_dir / f"{name}.html"
        suffix = 2
        while hist.exists():                      # history is append-only: never overwrite a page
            hist = hist_dir / f"{name}-{suffix}.html"
            suffix += 1
        store.atomic_write(hist, old_html)
        if btype == "artifact":
            store.atomic_write(old_file, store.artifact_page(html))
        else:
            new_html = targets.stamp_html(html)
            if btype == "decision":
                for o in options:
                    if o.get("id") == option:
                        o["html"] = new_html
            else:
                target["html"] = new_html
            store.atomic_write(path, json.dumps(dec, ensure_ascii=False), lock=True)
        revs[block] = new_rev
        now = store.now_iso()
        detail = f"patched in round {n} (revision {new_rev})"
        done: List[str] = []
        skipped: List[str] = []
        if cited is not None:
            for i in cited:
                q = requests[i]
                if q.get("status") in service.PIN_TERMINAL:
                    skipped.append(i)
                    continue
                q.update(status="done", done_at=now, detail=detail)
                done.append(i)
        else:
            for q in requests.values():
                if q.get("status") not in ("woken", "queued") or not _request_matches(q, block, option):
                    continue
                q.update(status="done", done_at=now, detail=detail)
                done.append(q["id"])
        history = current.get("history")
        if not isinstance(history, list):
            history = current["history"] = []
        history.append({"at": now, "action": "patch_block", "block": block, "option": option, "rev": new_rev,
                        "request_ids": done, "prev_file": hist.relative_to(store.review_dir(review_id)).as_posix()})
        store.save(review_id, data)
        result = {"ok": True, "review_id": review_id, "round": n, "block": block, "option": option,
                  "rev": new_rev, "done": done, "url": store.review_url(data)}
        if skipped:
            result["skipped"] = skipped
        return result


def _record_read(review_id: str, round_n: int) -> None:
    """Note the first read of the newest round's current submission: it proves a session woke and has the feedback,
    so the stalled ping waits stale_after_read_minutes from here instead of stale_after_minutes from Send."""
    with store.locked(review_id):
        data = store.load(review_id)
        r = data["rounds"][-1]
        sub = r.get("submission")
        if data["status"] != "open" or r["n"] != round_n or not sub or not r.get("feedback") \
                or sub.get("read_at") or store.guest_submission(r):
            return
        sub["read_at"] = store.now_iso()
        store.save(review_id, data)


@_owned
def get_feedback(review_id: str, round_n: Optional[int] = None) -> Dict[str, Any]:
    data = store.load(review_id)
    r = store.get_round(data, round_n)
    sub = r.get("submission")
    guest = store.guest_submission(r)
    if not guest and data["status"] == "open" and sub and r.get("feedback") and not sub.get("read_at") \
            and r["n"] == data["rounds"][-1]["n"]:
        try:
            _record_read(review_id, r["n"])
            data = store.load(review_id)
            r = store.get_round(data, r["n"])
        except (ReviewError, ValueError, OSError) as e:   # the read itself must never fail on this
            log.warning("could not record the feedback read for %s round %s: %s", review_id, r["n"], e)
    pin_requests = sorted((r.get("pin_requests") or {}).values(),
                          key=lambda q: q.get("requested_at") or "")
    action = "send" if guest else (r.get("feedback") or {}).get("action", "send")
    res = {"review_id": data["id"], "title": data["title"], "round": r["n"], "status": data["status"],
           "kind": r.get("kind", "variants"), "decisions": store.round_index(r),
           "submission": r.get("submission"), "feedback": None if guest else r.get("feedback"),
           "feedback_path": (None if guest else
                             service.feedback_path(data["id"], r["n"]) if r.get("feedback") else None),
           "markdown": feedback.render_markdown(data, r),
           "action": action,
           "pin_requests": pin_requests}
    # The build instruction only while it is still owed: an open review whose newest round is this one. A guest's
    # stored record is owner-only data, so it never drives a next step for the agent.
    if not guest and res["action"] == "build" and data["status"] == "open" and r["n"] == data["rounds"][-1]["n"]:
        label = (r.get("gate") or {}).get("label") or "Build"
        res["next_step"] = (f"The reviewer approved the whole round and pressed {label}. Build it now in this session. "
                            "When done, call mock_review_add_round with the result (what you built, with notes_answered "
                            "for every note) so the reviewer sees it on the same link. Do not ask for confirmation "
                            "again.")
    elif (not guest and res["action"] == "send" and res["kind"] == "decisions" and r.get("feedback")
          and data["status"] == "open" and r["n"] == data["rounds"][-1]["n"]):
        res["next_round"] = SEND_NEXT_ROUND
    waiting = [q for q in pin_requests if q.get("status") in ("queued", "woken")]
    if waiting:
        ids = ", ".join(f"{q.get('id')} (block {q.get('block')}, rev {q.get('rev', 0)})" for q in waiting)
        pin_step = (f"{len(waiting)} pin fix request(s) are waiting: patch the block in place with "
                    f"mock_review_patch_block(review_id=..., block=..., option=..., round={r['n']}, "
                    "if_rev=<rev you read>) — do not "
                    f"add a round. Pass their ids as request_ids so each shows Done: {ids}.")
        res["next_step"] = (res["next_step"] + " " + pin_step) if res.get("next_step") else pin_step
    return res


@_owned
def ack(review_id: str, round_n: Optional[int] = None) -> Dict[str, Any]:
    """The session has handled a round's submission (answered in chat, or nothing more is coming): no stalled ping
    for it. Once per submission; an amend is a new submission and needs its own ack. A build (Build) expects
    round N+1, so the session builds and adds that round instead; an ack there only silences the ping."""
    with store.locked(review_id):
        data = store.load(review_id)
        r = store.get_round(data, round_n)
        sub = r.get("submission")
        if not sub or not r.get("feedback"):
            raise ReviewError(f"round {r['n']} of {review_id} has no submitted feedback to acknowledge", 409)
        sub.setdefault("acked_at", store.now_iso())
        state = sub.get("stalled_notice") or {}
        if state.get("status") not in service.TERMINAL_NOTICE:
            state.update(status="skipped", reason="acknowledged")
            sub["stalled_notice"] = state
        store.save(review_id, data)
    return {"review_id": review_id, "round": r["n"], "seq": sub.get("seq"), "acked_at": sub["acked_at"],
            "stalled_notice": state["status"]}


def _row(rid: str, d: Dict[str, Any], url: str) -> Dict[str, Any]:
    cur = d["rounds"][-1]
    return {"review_id": rid, "title": d["title"], "mode": d["mode"], "status": d["status"],
            "rounds": len(d["rounds"]), "origin": d.get("origin", {}),
            "thread_id": (d.get("origin") or {}).get("thread_id"),
            "current_round_feedback": (cur.get("submission") or {}).get("status", "waiting"),
            "url": url,
            "updated_at": d.get("updated_at")}


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
        rows.append(_row(rid, d, store.review_url(d)))
    return rows


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@_owned
def desk_list() -> List[Dict[str, Any]]:
    """Every review the store holds, closed and older ones included, newest activity first: the rows of the Desktop Mocks
    tab. The rows of list_reviews(include_closed=True), with `url` named `board_url` (desk_url), plus `round` (the current
    round's number) and `last_activity` (newest of the review's creation, a round, a submission or its last save)."""
    rows, public = [], None
    for rid in store.list_ids():
        try:
            d = store.load(rid)
            if public is None and d["mode"] != "local":
                public = config.public_link_base()    # once, not per review: it may ask the tailscale CLI
            row = _row(rid, d, desk_url(d, public))
        except (ReviewError, ValueError, OSError, KeyError, IndexError, TypeError):
            continue
        row["board_url"] = row.pop("url")
        row["round"] = d["rounds"][-1].get("n", row["rounds"])
        row["last_activity"] = max(last_activity(d), _ts(d.get("updated_at")))
        rows.append(row)
    rows.sort(key=lambda r: (-r["last_activity"], r["review_id"]))
    for r in rows:
        r["last_activity"] = _iso(r["last_activity"]) if r["last_activity"] else None
    return rows


def desk_url(data: Dict[str, Any], public_base: Optional[str] = None) -> str:
    """The address the review's board answers on, for the Desktop's Open in browser: the review's own link (store.review_url:
    this computer's address for a local review, the tailnet or public base otherwise), and the loopback board when the
    install has no such base configured. The Desktop may run on another computer than this one; there the loopback
    address reaches nothing, which is why a configured tailnet or public host is preferred."""
    return store.review_url(data, public_base) or f"{config.local_base()}/r/{data['id']}"


# The longest data: URL (in characters) the viewer hands to the Desktop's frame. Measured in the packaged Desktop (a
# sandboxed iframe, `src` = data:text/html;base64,...): 2,096,500 characters render, and 2,097,100 kills the renderer
# process, so this sits below both. A longer snapshot is not truncated: desk_view says `too_large` and the tab offers
# Open in browser. The frame cannot load a plugin_api route instead (every /api/plugins/ route needs the app's session
# token, which a frame cannot send). See docs/desktop.md.
DESK_DATA_URL_LIMIT = 2_000_000


@_owned
def desk_view(review_id: str, round_n: Optional[int] = None) -> Dict[str, Any]:
    """One review's round (default: the current one) as the Desktop's read-only viewer shows it: the header facts and
    the export document (export.build_export_html, comments off) as a `data:` URL. The document is rebuilt through the
    export allowlist, so it holds no script, handler or external URL, and carries its own CSP meta. Nothing is written.
    Above DESK_DATA_URL_LIMIT `data_url` is None and `too_large` is true."""
    data = store.load(review_id)
    rnd = store.get_round(data, round_n)
    doc = export.build_export_html(data, rnd["n"], False).encode("utf-8")
    url = "data:text/html;charset=utf-8;base64," + base64.b64encode(doc).decode("ascii")
    too_large = len(url) > DESK_DATA_URL_LIMIT
    return {"review_id": review_id, "title": data.get("title"), "status": data.get("status"), "round": rnd["n"],
            "rounds": len(data["rounds"]), "board_url": desk_url(data), "bytes": len(doc),
            "too_large": too_large, "data_url": None if too_large else url}


@_owned
def review_link(review_id: str) -> Dict[str, Any]:
    d = store.load(review_id)
    return {"review_id": review_id, "title": d["title"], "status": d["status"], "mode": d["mode"],
            "url": store.review_url(d)}


@_owned
def close(review_id: str) -> Dict[str, Any]:
    """Close a review: later rounds and submissions are refused, and any pending notice is dropped."""
    store.load(review_id)
    return _close(review_id)


def _drop_pending_notices(data: Dict[str, Any]) -> None:
    """A closed review never pings: every stalled ping not yet sent (including the newest round's, never attempted)
    and every unsent ack becomes dropped, so nothing reads as still owed."""
    if data.get("wake") is False:
        return
    for r in data["rounds"]:
        sub = r.get("submission")
        if not sub or not r.get("feedback"):
            continue
        for key in ("stalled_notice", "notice"):
            state = sub.get(key)
            if state is None and not (key == "stalled_notice" and r is data["rounds"][-1]):
                continue
            state = state or {}
            if state.get("status") not in service.TERMINAL_NOTICE:
                state.update(status="dropped", reason="review closed")
                state.setdefault("last_error", None)
                sub[key] = state


def _close(review_id: str) -> Dict[str, Any]:
    data = store.load(review_id)
    res: Dict[str, Any] = {"review_id": review_id, "mode": data["mode"]}
    with store.locked(review_id):
        data = store.load(review_id)
        data["status"] = "closed"
        data["closed_at"] = store.now_iso()
        _drop_pending_notices(data)
        store.save(review_id, data)
    res["status"] = "closed"
    return res


@_owned
def delete_review(review_id: str) -> Dict[str, Any]:
    """Delete a review for good: close it first when it is open (so no stalled ping or wake-up outlives it), clear its
    rows from the access database (users.purge_review), then remove its directory. Housekeeping's prune and the
    desktop and CLI delete all come through here. An unknown id is a 404 ReviewError."""
    data = store.load(review_id)
    was_open = data.get("status") == "open"
    if was_open:
        _close(review_id)
    d = store.review_dir(review_id)
    with store.locked(review_id):
        rows = users.purge_review(review_id)
        shutil.rmtree(d, ignore_errors=True)
    # Windows keeps the open .lock file (and its directory) until the lock closes.
    if d.exists():
        shutil.rmtree(d)
    return {"review_id": review_id, "title": data.get("title"), "deleted": True, "was_open": was_open, "rows": rows}


# ---------------------------------------------------------------- housekeeping (the server's worker runs this)

def _ts(iso: Any) -> float:
    try:
        return float(calendar.timegm(time.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def last_activity(data: Dict[str, Any]) -> float:
    """Newest of: review created, a round added, feedback submitted."""
    times = [_ts(data.get("created_at"))]
    for r in data.get("rounds") or []:
        times.append(_ts(r.get("created_at")))
        times.append(_ts((r.get("submission") or {}).get("submitted_at")))
    return max(times)


def housekeeping(now: Optional[float] = None) -> Dict[str, Any]:
    """auto_close_idle_days: close idle open reviews. keep_closed_days: delete closed reviews that closed longer ago
    than that. 0 = off for either."""
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
                delete_review(rid)
                out["pruned"].append(rid)
                log.info("pruned %s (closed more than %s days ago)", rid, keep_days)
        except Exception as e:  # one bad review must not stop the pass
            out["errors"].append({"review_id": rid, "error": getattr(e, "message", None) or str(e)})
            log.error("housekeeping of %s failed: %s", rid, e)
    return out
