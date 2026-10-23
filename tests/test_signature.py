import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import SECRET, verify_v2_copy
from mockreview import notify

HERMES = os.environ.get("HERMES_AGENT_SRC")


def _adapter_validate():
    """Hermes's own WebhookAdapter._validate_signature, or skip if the gateway isn't importable."""
    if HERMES:
        sys.path.insert(0, str(Path(HERMES).expanduser()))
    try:
        from gateway.platforms.webhook import WebhookAdapter
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Hermes webhook adapter unavailable ({type(e).__name__}); install hermes-agent or set HERMES_AGENT_SRC")

    class Headers(dict):
        def get(self, k, d=None):
            for kk, v in self.items():
                if kk.lower() == k.lower():
                    return v
            return d

    def validate(headers, body, secret):
        req = SimpleNamespace(headers=Headers(headers), match_info={"route_name": "mock-review"})
        fake = SimpleNamespace(_v1_signature_warned=set(), _validate_svix_signature=lambda **kw: False)
        return WebhookAdapter._validate_signature(fake, req, body, secret)

    return validate


BODY = json.dumps({"event_type": "review.submitted", "review_id": "x-1", "round": 1}).encode()


def test_sign_matches_exact_copy():
    h = notify.sign(BODY, SECRET)
    assert set(h) == {"X-Webhook-Timestamp", "X-Webhook-Signature-V2"}
    assert verify_v2_copy(h, BODY, SECRET)
    assert not verify_v2_copy(h, BODY + b" ", SECRET)
    assert not verify_v2_copy(h, BODY, SECRET + "x")


def test_sign_verified_by_hermes_adapter():
    validate = _adapter_validate()
    h = notify.sign(BODY, SECRET)
    assert validate(h, BODY, SECRET) is True
    assert validate(h, BODY + b"x", SECRET) is False
    assert validate(h, BODY, "other") is False
    old = notify.sign(BODY, SECRET, ts=int(time.time()) - 400)  # outside the 300 s replay window
    assert validate(old, BODY, SECRET) is False
    # header names are case-insensitive on the wire (urllib sends "X-webhook-signature-v2")
    assert validate({k.lower(): v for k, v in h.items()}, BODY, SECRET) is True


def test_post_over_http_is_verifiable(env):
    ok, msg = notify.post(env["receiver"].url, {"a": 1}, SECRET, "mockreview-test-1")
    assert ok, msg
    r = env["receiver"].requests[0]
    assert r["ok"]
    validate = _adapter_validate()
    assert validate(r["headers"], r["body"], SECRET) is True


def test_load_secret_errors(secret_values):
    secret_values.clear()
    secret, err = notify.load_secret()
    assert secret is None and "MOCKREVIEW_WEBHOOK_SECRET is not set" in err
    secret_values["MOCKREVIEW_WEBHOOK_SECRET"] = "   "
    assert notify.load_secret()[0] is None
