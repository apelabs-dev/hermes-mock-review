"""here.now hosting: build the static board, publish, poll Site Data, delete.

The published site is the same board as the local one plus a Site Data manifest.
The browser inserts one record per submit into the `feedback` collection
(public insert, owner-only read). The local server polls those records with the
account API key, treats every record as untrusted, and runs it through the same
submit path as a local POST.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config, locks, service, store
from . import secrets
from .store import ReviewError

log = logging.getLogger("mockreview.herenow")

COLLECTION = "feedback"
MAX_PAYLOAD = 15000  # Site Data caps a record body at 16 KB
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")
BOARD_FILES = ("index.html", "board.css", "board.js", "favicon.svg")

MANIFEST = {
    "collections": {
        COLLECTION: {
            "fields": {
                "token": {"type": "string", "required": True, "maxLength": 64},
                "round": {"type": "integer", "required": True, "minimum": 1, "maximum": 999},
                "amend": {"type": "boolean", "default": False},
                "payload": {"type": "string", "required": True, "maxLength": MAX_PAYLOAD},
            },
            "access": {"read": "owner", "insert": "public", "update": "owner", "delete": "owner"},
            "rateLimit": "30/hour/ip",
        }
    }
}


class HereNowError(ReviewError):
    pass


def api_key() -> str:
    key = secrets.get_secret("HERENOW_API_KEY")
    if not key:
        raise HereNowError("HERENOW_API_KEY is not set in the active Hermes secret scope", 409)
    return key


def _api(method: str, path: str, timeout: float = 20.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        config.HERENOW_API + path,
        method=method,
        headers={"Authorization": f"Bearer {api_key()}", "X-HereNow-Client": config.HERENOW_CLIENT,
                 "Accept": "application/json", "User-Agent": "mock-review/1.0 (hermes)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024)
            return json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read(1000).decode("utf-8", "replace") if e.fp else ""
        raise HereNowError(f"here.now {method} {path} -> HTTP {e.code}: {body[:300]}", e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise HereNowError(f"here.now {method} {path} failed: {e}", 502)


def build_site(data: Dict[str, Any], dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in BOARD_FILES:
        shutil.copyfile(config.BOARD_DIR / name, dest / name)
    state = store.public_state(data, submit_kind="sitedata")
    (dest / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    src_rounds = store.review_dir(data["id"]) / "rounds"
    shutil.copytree(src_rounds, dest / "rounds", ignore=shutil.ignore_patterns("feedback.*", ".*"))
    (dest / ".herenow").mkdir()
    (dest / ".herenow" / "data.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    return dest


def publish(review_id: str) -> Dict[str, Any]:
    """Build and publish (create or update) the review's site via the here.now publish helper."""
    try:
        script = config.herenow_publish_script()
    except FileNotFoundError as e:
        raise HereNowError(str(e), 409)
    key = api_key()  # Must be nonempty before the helper can run.
    child_env = dict(os.environ, HERENOW_API_KEY=key)
    rdir = store.review_dir(review_id)
    with locks.exclusive(rdir / ".publish.lock"):
        data = store.load(review_id)
        hn = data.get("herenow") or {}
        site = build_site(data, rdir / "site")
        cmd = ["bash", str(script), str(site), "--client", "hermes", "--title", f"Mock review: {data['title']}"[:80]]
        if hn.get("slug"):
            cmd += ["--slug", hn["slug"]]
        try:
            proc = subprocess.run(cmd, cwd=str(rdir), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=child_env)
        except subprocess.TimeoutExpired:
            raise HereNowError("here.now publish timed out after 300 s", 504)
        results: Dict[str, str] = {}
        for line in (proc.stderr or "").splitlines():
            if line.startswith("publish_result.") and "=" in line:
                k, v = line[len("publish_result."):].split("=", 1)
                results[k] = v.strip()
        slug, url = results.get("slug", ""), results.get("site_url", "")
        if proc.returncode != 0 or not SLUG_RE.match(slug or "") or not url.startswith("https://"):
            tail = "\n".join(proc.stderr.strip().splitlines()[-4:])
            raise HereNowError(f"here.now publish failed (exit {proc.returncode}): {tail}", 502)
        if results.get("auth_mode") != "authenticated":
            log.warning("here.now publish was not authenticated — Site Data will return 403 account_required")
    with store.locked(review_id):
        data = store.load(review_id)
        data.setdefault("herenow", {}).update(slug=slug, site_url=url, published_at=store.now_iso(),
                                              auth_mode=results.get("auth_mode"))
        store.save(review_id, data)
    return data


def republish_quietly(review_id: str) -> None:
    try:
        publish(review_id)
    except Exception as e:  # the poller must keep running
        log.error("republish of %s failed: %s", review_id, e)


def fetch_records(slug: str) -> List[Dict[str, Any]]:
    if not SLUG_RE.match(slug or ""):
        raise HereNowError("bad slug", 400)
    out: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(20):
        q = {"limit": "100"}
        if cursor:
            q["cursor"] = cursor
        res = _api("GET", f"/publishes/{slug}/data/{COLLECTION}?" + urllib.parse.urlencode(q))
        recs = res.get("records") if isinstance(res, dict) else None
        out.extend(r for r in (recs or []) if isinstance(r, dict))
        cursor = res.get("nextCursor") if isinstance(res, dict) else None
        if not cursor:
            break
    return out


def delete_site(slug: str) -> bool:
    if not SLUG_RE.match(slug or ""):
        raise HereNowError("bad slug", 400)
    try:
        _api("DELETE", f"/publish/{slug}")
    except HereNowError as e:
        if e.status == 404:
            return False
        raise
    return True


def parse_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Pull only the known fields out of an untrusted Site Data record."""
    d = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    token, rnd, amend, payload = d.get("token"), d.get("round"), d.get("amend", False), d.get("payload")
    if not isinstance(token, str) or len(token) > 64:
        raise ReviewError("record has no valid token", 403)
    if not isinstance(rnd, int) or isinstance(rnd, bool):
        raise ReviewError("record has no valid round")
    if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD:
        raise ReviewError("record payload missing or too large")
    try:
        fb = json.loads(payload)
    except ValueError:
        raise ReviewError("record payload is not JSON")
    return {"token": token, "round": rnd, "amend": amend is True, "feedback": fb}


def poll_review(review_id: str, fetch: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
                republish: bool = True) -> List[Dict[str, Any]]:
    """Ingest new Site Data records for one open here.now review. Returns per-record outcomes."""
    fetch = fetch or fetch_records
    data = store.load(review_id)
    hn = data.get("herenow") or {}
    if data["mode"] != "herenow" or data["status"] != "open" or not hn.get("slug"):
        return []
    records = fetch(hn["slug"])
    seen = set(hn.get("processed_records") or [])
    fresh = [r for r in records if isinstance(r.get("id"), str) and len(r["id"]) <= 80 and r["id"] not in seen]
    fresh.sort(key=lambda r: str(r.get("createdAt") or ""))
    outcomes = []
    accepted_rounds = []
    for rec in fresh:
        rid = rec["id"]
        try:
            parsed = parse_record(rec)
            res = service.submit(review_id, parsed["round"], parsed["feedback"], parsed["token"],
                                 amend=parsed["amend"], source="herenow", record_id=rid, do_notify=False)
            outcome = {"record": rid, "result": "accepted", "round": res["round"], "amended": res["amended"]}
            accepted_rounds.append(res["round"])
        except ReviewError as e:
            outcome = {"record": rid, "result": "rejected", "reason": e.message}
            log.warning("here.now record %s for %s rejected: %s", rid, review_id, e.message)
        with store.locked(review_id):
            data = store.load(review_id)
            h = data.setdefault("herenow", {})
            h["processed_records"] = (h.get("processed_records") or [])[-4999:] + [rid]
            h["record_log"] = (h.get("record_log") or [])[-49:] + [{**outcome, "at": store.now_iso()}]
            store.save(review_id, data)
        outcomes.append(outcome)
    for n in dict.fromkeys(accepted_rounds):
        service.deliver_submission(review_id, n)
    with store.locked(review_id):
        data = store.load(review_id)
        data.setdefault("herenow", {})["last_poll"] = store.now_iso()
        store.save(review_id, data)
    if accepted_rounds and republish:
        republish_quietly(review_id)
    return outcomes


def open_reviews() -> List[str]:
    out = []
    for rid in store.list_ids():
        try:
            d = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        if d.get("mode") == "herenow" and d.get("status") == "open" and (d.get("herenow") or {}).get("slug"):
            out.append(rid)
    return out


def poll_all() -> int:
    n = 0
    for rid in open_reviews():
        try:
            n += sum(1 for o in poll_review(rid) if o["result"] == "accepted")
        except Exception as e:
            log.error("poll of %s failed: %s", rid, e)
    return n
