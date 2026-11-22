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

from mockreview import config as _config  # noqa: E402

SECRET = "test-secret-" + "x" * 20
PLUGIN_MODULE = "_mockreview_root_plugin"
_REAL_RUN_TAILSCALE = _config.run_tailscale


def _no_tailscale(args, timeout=5):
    """No test asks this machine's Tailscale CLI: its Serve and Funnel mappings are host state, not test state."""
    raise FileNotFoundError("tailscale")


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
    from mockreview import live, server as srv
    httpd = srv.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    # A live stream re-reads identity every 5 s (live.RECHECK_EVERY), and that read resolves the data root from the
    # environment as it is at that moment. End every subscription before this test's environment is restored (env
    # finalizes after this fixture), or the loop writes into the next test's store. daemon_threads makes shutdown()
    # leave handler threads running, so wait for them too, bounded.
    live.REGISTRY.reset()
    httpd.shutdown()
    httpd.server_close()
    deadline = time.monotonic() + 3.0
    while True:
        leftover = [th for th in threading.enumerate()
                    if th is not threading.current_thread() and th.is_alive()
                    and "process_request_thread" in th.name]
        remaining = deadline - time.monotonic()
        if not leftover or remaining <= 0:
            break
        leftover[0].join(remaining)


TEST_CSRF = "test-csrf-token"
CSRF_HEADERS = {"Cookie": "mr_csrf=" + TEST_CSRF, "X-CSRF-Token": TEST_CSRF}


def http(method, url, body=None, headers=None, csrf=True):
    """One request. A POST carries the double-submit CSRF pair (cookie + header) unless csrf=False or the test sets
    its own Cookie / X-CSRF-Token."""
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    if method == "POST" and csrf:
        hdrs.update(CSRF_HEADERS)
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
    """No test reads the real ~/.hermes/config.yaml or asks this machine's Tailscale: point the config layer at a
    temp file (absent unless a test writes it), replace the CLI with one that is not installed, and reset the
    in-process config source / reader switch afterwards."""
    from mockreview import api, config as config_mod, owner, settings, secrets
    secret_values = {"MOCKREVIEW_WEBHOOK_SECRET": SECRET}
    monkeypatch.setattr(secrets, "get_secret", lambda name: (secret_values.get(name) or "").strip())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MOCKREVIEW_CONFIG", str(tmp_path / "hermes-config.yaml"))
    for s in settings.SPEC:
        if s.env_name not in ("MOCKREVIEW_HOME", "MOCKREVIEW_PUBLIC_BASE", "MOCKREVIEW_WEBHOOK_URL",
                              "MOCKREVIEW_RETRY_SECONDS"):
            monkeypatch.delenv(s.env_name, raising=False)
    monkeypatch.setattr(config_mod, "run_tailscale", _no_tailscale)
    monkeypatch.setattr(api, "server_up", lambda: False)
    # No test shells out to the real tailscale binary: pin `auth.serve_fronts_board`'s cache to "Serve fronts nothing"
    # and make `public.serve_probe` answer "unknown", so a TTL expiring inside a long browser test still cannot reach
    # the CLI. A test that exercises the detection resets the cache and fakes the probe (or `config.run_tailscale`)
    # itself.
    from mockreview import auth as auth_mod, public as public_mod
    monkeypatch.setattr(auth_mod, "_serve_front", False)
    monkeypatch.setattr(public_mod, "serve_probe", lambda commands=None: None)
    # setup's port check must not meet the real board that may run on this machine's default port
    from mockreview import setup as setup_mod
    real_identity = setup_mod.board_identity
    monkeypatch.setattr(setup_mod, "board_identity", lambda port, timeout=2.0: (
        ("none", {}) if str(port) == str(config_mod.PORT) else real_identity(port, timeout)))
    monkeypatch.setenv("MOCKREVIEW_OWNER", "self")   # tests use their own temp store; test_owner.py exercises the rest
    # The same answers with or without Hermes importable: no calling chat (a test binds one through
    # platforms._session_env), and every well-formed platform name a deliver platform unless a test asks the registry.
    from mockreview import platforms, sessionwake
    for name in ("PLATFORM", "CHAT_ID", "THREAD_ID", "KEY"):
        monkeypatch.delenv("HERMES_SESSION_" + name, raising=False)
    monkeypatch.setattr(platforms, "_session_env", lambda: None)
    monkeypatch.setattr(platforms, "_hermes_knows", lambda platform: None)
    yield tmp_path / "hermes-config.yaml"
    sessionwake.detach()
    settings.set_config_source(None)
    settings.FORCE_MINI_PARSER = False
    settings._cache["key"] = None


@pytest.fixture
def real_tailscale(monkeypatch):
    """The genuine launcher for tests of Tailscale detection itself; they stub subprocess.run."""
    from mockreview import config as config_mod
    monkeypatch.setattr(config_mod, "run_tailscale", _REAL_RUN_TAILSCALE)


@pytest.fixture(autouse=True)
def close_access_db():
    """Each test has its own data root; close this thread's SQLite connections to it afterwards."""
    yield
    from mockreview import db
    db.close_all()


@pytest.fixture
def secret_values(monkeypatch):
    from mockreview import secrets
    values = {"MOCKREVIEW_WEBHOOK_SECRET": SECRET}
    monkeypatch.setattr(secrets, "get_secret", lambda name: (values.get(name) or "").strip())
    return values
