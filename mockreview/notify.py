"""Wake Hermes: HMAC-signed POST to its webhook adapter.

Signing matches gateway/platforms/webhook.py "Generic V2":
    X-Webhook-Timestamp:    <unix seconds>   (must be within 300 s of Hermes's clock)
    X-Webhook-Signature-V2: hex(HMAC-SHA256(secret, "<timestamp>." + raw_body))
X-Request-ID is the delivery id Hermes uses for its 1 h duplicate filter, so a
retry of the same submission is never processed twice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from . import config
from . import secrets

log = logging.getLogger("mockreview.notify")

EVENT = "review.submitted"


def sign(body: bytes, secret: str, ts: Optional[int] = None) -> Dict[str, str]:
    ts = int(time.time()) if ts is None else int(ts)
    sig = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Webhook-Timestamp": str(ts), "X-Webhook-Signature-V2": sig}


def load_secret() -> Tuple[Optional[str], str]:
    """Resolve this plugin's signing secret in the caller's active scope."""
    secret = secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET")
    return (secret, "") if secret else (None, "MOCKREVIEW_WEBHOOK_SECRET is not set in the active Hermes secret scope")


def classify(code: int, text: str) -> Tuple[bool, str]:
    """HTTP status + body of the gateway's webhook adapter -> (woken-or-already-woken, detail).

    The adapter answers 202 {"status": "accepted"|"coalesced"} for a new wake, 200 {"status": "duplicate"} when it
    already took this delivery id in the last hour, and 200 {"status": "ignored"} when the route's events filter
    dropped it. Only the first two mean Hermes was (or already is being) woken; "ignored" must not count.
    """
    status = ""
    try:
        obj = json.loads(text)
        status = str(obj.get("status", "")) if isinstance(obj, dict) else ""
    except ValueError:
        pass
    if code == 202 or (code == 200 and status in ("accepted", "coalesced", "duplicate")):
        return True, f"HTTP {code}" + (f" {status}" if status else "")
    if 200 <= code < 300:
        return False, f"HTTP {code} but the gateway did not wake anyone ({status or 'no status'}): {text[:200]}"
    return False, f"HTTP {code}: {text[:300]}"


def classify_delivered(code: int, text: str) -> Tuple[bool, str]:
    """HTTP status + body of a deliver-only notice route -> (delivered, detail).

    The adapter answers 200 {"status": "delivered"} when it rendered and delivered the notice, 200
    {"status": "duplicate"} when it already took this delivery id (Hermes records the id before it delivers, so a
    duplicate only means delivered when the earlier POST with that id got no answer; the caller changes the id after a
    gateway error), and 502 {"status": "error", ...} when the target
    adapter is missing or rejected it (retryable). It never answers 202: that is the agent route's code.
    """
    status = ""
    try:
        obj = json.loads(text)
        status = str(obj.get("status", "")) if isinstance(obj, dict) else ""
    except ValueError:
        pass
    if code == 200 and status in ("delivered", "duplicate"):
        return True, f"HTTP {code} {status}"
    return False, f"HTTP {code}: {text[:300]}"


def is_no_route(detail: str) -> bool:
    """True when a classify_delivered detail is the gateway's 404: the notice route does not exist."""
    return detail.startswith("HTTP 404")


def _request(url: str, payload: Dict[str, Any], secret: str, delivery_id: str,
             timeout: float) -> Tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "mock-review/1.0",
        "X-Request-ID": delivery_id,
        **sign(body, secret),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read(2000).decode("utf-8", "replace") if e.fp else "")


def post(url: str, payload: Dict[str, Any], secret: str, delivery_id: str, timeout: float = 10.0) -> Tuple[bool, str]:
    try:
        code, text = _request(url, payload, secret, delivery_id, timeout)
    except (urllib.error.URLError, OSError) as e:
        return False, f"cannot reach {url}: {getattr(e, 'reason', e)}"
    return classify(code, text)


def post_notice(url: str, payload: Dict[str, Any], secret: str, delivery_id: str,
                timeout: float = 10.0) -> Tuple[bool, str]:
    try:
        code, text = _request(url, payload, secret, delivery_id, timeout)
    except (urllib.error.URLError, OSError) as e:
        return False, f"cannot reach {url}: {getattr(e, 'reason', e)}"
    return classify_delivered(code, text)


def send(payload: Dict[str, Any], delivery_id: str, url: Optional[str] = None) -> Tuple[bool, str]:
    secret, err = load_secret()
    if not secret:
        log.error("cannot notify Hermes: %s", err)
        return False, err
    from .store import normalize_origin, ReviewError
    try:
        origin = normalize_origin(payload)
    except ReviewError as e:
        return False, e.message
    return post(url or config.webhook_url(origin["platform"]), payload, secret, delivery_id)


def send_notice(payload: Dict[str, Any], delivery_id: str, platform: str,
                url: Optional[str] = None) -> Tuple[bool, str]:
    """POST a deliver-only notice to the `<route>-<platform>-notice` route. Same secret, same signing."""
    secret, err = load_secret()
    if not secret:
        log.error("cannot deliver the notice: %s", err)
        return False, err
    return post_notice(url or config.notice_webhook_url(platform), payload, secret, delivery_id)
