import hashlib
import hmac
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SECRET = "test-secret-" + "x" * 20
PLUGIN_MODULE = "_mockreview_root_plugin"


def python_child_env(tmp_path):
    """Keep OS runtime requirements, but isolate homes and discard application/Python configuration."""
    runtime = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
               "TEMP", "TMP", "TMPDIR", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() in runtime}
    env.update(HOME=str(tmp_path / "user"), USERPROFILE=str(tmp_path / "user"),
               APPDATA=str(tmp_path / "appdata"), LOCALAPPDATA=str(tmp_path / "localappdata"),
               PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return env


def load_plugin():
    """The plugin entry point, the root __init__.py, loaded from its file the way Hermes loads an installed plugin.
    Loaded once per session and cached in sys.modules, like an import."""
    module = sys.modules.get(PLUGIN_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(PLUGIN_MODULE, ROOT / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[PLUGIN_MODULE] = module
        spec.loader.exec_module(module)
    return module


def verify_v2_copy(headers, body: bytes, secret: str) -> bool:
    """Exact copy of the generic-V2 branch of gateway/platforms/webhook.py::_validate_signature."""
    h = {k.lower(): v for k, v in headers.items()}
    v2_sig = h.get("x-webhook-signature-v2", "")
    if not v2_sig:
        return False
    v2_timestamp = h.get("x-webhook-timestamp", "")
    if not v2_timestamp:
        return False
    try:
        ts = int(v2_timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    signed_content = v2_timestamp.encode() + b"." + body
    expected_v2 = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v2_sig.encode(), expected_v2.encode())


class Receiver:
    def __init__(self):
        self.requests = []
        self.status = 202            # wake (agent) route: 202 accepted
        self.notice_status = 200     # notice (deliver_only) route: 200 delivered
        # Like Hermes: a delivery id is recorded BEFORE the notice is delivered, so an id that already got an answer
        # (a 502 included) is answered 200 duplicate for the next hour, whether or not it was ever delivered.
        self.seen_notice_ids = set()
        self.delivered_notices = []  # the notice requests that were really delivered (200 delivered)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                hdrs = dict(self.headers.items())
                outer.requests.append({"path": self.path, "headers": hdrs, "body": body,
                                       "ok": verify_v2_copy(hdrs, body, SECRET)})
                if self.path.endswith("-notice"):
                    rid = {k.lower(): v for k, v in hdrs.items()}.get("x-request-id", "")
                    if rid and rid in outer.seen_notice_ids:
                        code, resp = 200, b'{"status": "duplicate"}'
                    else:
                        outer.seen_notice_ids.add(rid)
                        code = outer.notice_status
                        resp = (b'{"status": "delivered"}'
                                if code == 200 else b'{"status": "error", "error": "Delivery failed"}')
                        if code == 200:
                            outer.delivered_notices.append(outer.requests[-1])
                else:
                    code = outer.status
                    resp = b"{}"
                self.send_response(code)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/webhooks/mock-review"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def wakes(self):
        return [r for r in self.requests if not r["path"].endswith("-notice")]

    @property
    def notices(self):
        return [r for r in self.requests if r["path"].endswith("-notice")]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    rec = Receiver()
    monkeypatch.setenv("MOCKREVIEW_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_URL", rec.url)
    monkeypatch.setenv("MOCKREVIEW_RETRY_SECONDS", "0")
    yield {"tmp": tmp_path, "receiver": rec}
    rec.close()


@pytest.fixture
def variants(tmp_path):
    out = []
    for i, name in enumerate(["alpha", "beta", "gamma"]):
        d = tmp_path / "src" / name
        d.mkdir(parents=True)
        (d / "index.html").write_text(f"<!doctype html><html><body><h1>{name}</h1></body></html>", encoding="utf-8")
        if i == 0:
            (d / "assets").mkdir()
            (d / "assets" / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        out.append(f"{name.title()}|stance {name}|{d / 'index.html'}")
    return out


@pytest.fixture
def review(env, variants):
    from mockreview import store
    specs = [store.parse_variant_spec(v) for v in variants]
    return store.create_review("Test review", "brief", {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}, "tailscale", specs)


@pytest.fixture
def server(env):
    from mockreview import server as srv
    httpd = srv.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()


def http(method, url, body=None, headers=None):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


FEEDBACK = {
    "choice": "b",
    "choice_note": "Clearest",
    "guidelines": "Keep the table.",
    "variants": {
        "a": {"note": "Nice type", "pins": []},
        "b": {"note": "", "pins": [{"frame": "desktop", "x": 0.62, "y": 0.18, "page_h": 800,
                                     "text": "make this button smaller"}]},
        "c": {"note": "", "pins": []},
    },
}


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """No test reads the real ~/.hermes/config.yaml: point the config layer at a temp file (absent unless a test
    writes it) and reset the in-process config source / reader switch afterwards."""
    from mockreview import api, config as config_mod, owner, settings, secrets
    secret_values = {"MOCKREVIEW_WEBHOOK_SECRET": SECRET, "HERENOW_API_KEY": "test-public-key"}
    monkeypatch.setattr(secrets, "get_secret", lambda name: (secret_values.get(name) or "").strip())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MOCKREVIEW_CONFIG", str(tmp_path / "hermes-config.yaml"))
    for s in settings.SPEC:
        if s.env_name not in ("MOCKREVIEW_HOME", "MOCKREVIEW_PUBLIC_BASE", "MOCKREVIEW_WEBHOOK_URL",
                              "MOCKREVIEW_RETRY_SECONDS"):
            monkeypatch.delenv(s.env_name, raising=False)
    monkeypatch.setattr(api, "server_up", lambda: False)
    # setup's port check must not meet the real board that may run on this machine's default port
    from mockreview import setup as setup_mod
    real_identity = setup_mod.board_identity
    monkeypatch.setattr(setup_mod, "board_identity", lambda port, timeout=2.0: (
        ("none", {}) if str(port) == str(config_mod.PORT) else real_identity(port, timeout)))
    monkeypatch.setenv("MOCKREVIEW_OWNER", "self")   # tests use their own temp store; test_owner.py exercises the rest
    yield tmp_path / "hermes-config.yaml"
    settings.set_config_source(None)
    settings.FORCE_MINI_PARSER = False
    settings._cache["key"] = None


@pytest.fixture
def secret_values(monkeypatch):
    from mockreview import secrets
    values = {"MOCKREVIEW_WEBHOOK_SECRET": SECRET, "HERENOW_API_KEY": "test-public-key"}
    monkeypatch.setattr(secrets, "get_secret", lambda name: (values.get(name) or "").strip())
    return values


@pytest.fixture(autouse=True)
def block_real_herenow(monkeypatch):
    """Every here.now HTTP interaction must have an explicit test double."""
    original = urllib.request.urlopen
    def guarded(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url.startswith("https://here.now/") or ".here.now/" in url:
            raise AssertionError("real here.now HTTP is forbidden in tests")
        return original(request, *args, **kwargs)
    monkeypatch.setattr(urllib.request, "urlopen", guarded)
