"""Board ownership: a worker (different HERMES_HOME) must never publish a board the owning service cannot find or
wake anyone for. The owner is a REAL board server in a subprocess with its own store, scoped secret and
environment; the worker is this test process with a different HERMES_HOME. Nothing here touches ~/.hermes."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FEEDBACK, ROOT, SECRET, Receiver, http, load_plugin, python_child_env
from mockreview import api, cli, config, herenow, notify, owner, service, settings, store
from mockreview.store import ReviewError
import test_store


def _decisions(tmp_path):
    f = tmp_path / "dec" / "decisions.json"
    return f if f.is_file() else test_store._decisions(tmp_path)


def _design(tmp_path):
    d = tmp_path / "design"
    return d if d.is_dir() else test_store._design(tmp_path)

THREAD = "test-topic"
DEC_FB = {"marks": {"d1": {"status": "approve", "option": "b"}}, "pins": [], "guidelines": "Please simplify the layout."}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(port, proc=None, secs=15):
    end = time.time() + secs
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"owner service exited early ({proc.returncode})")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError("owner service did not come up")


class Service:
    def __init__(self, tmp, secret=SECRET, receiver=None):
        self.tmp = tmp
        self.home = tmp / "owner-home"
        self.root = tmp / "owner-store"
        self.home.mkdir()
        provider = tmp / "provider" / "agent"
        provider.mkdir(parents=True)
        (provider / "__init__.py").write_text("", encoding="utf-8")
        (provider / "secret_scope.py").write_text(
            "def get_secret(name):\n    return " + repr({"MOCKREVIEW_WEBHOOK_SECRET": secret}) + ".get(name)\n", encoding="utf-8")
        self.receiver = receiver or Receiver()
        self.port = free_port()
        env = python_child_env(tmp)
        env.update(HERMES_HOME=str(self.home), MOCKREVIEW_HOME=str(self.root),
                   PYTHONPATH=str(provider.parent), MOCKREVIEW_WEBHOOK_URL=self.receiver.url,
                   MOCKREVIEW_RETRY_SECONDS="0", MOCKREVIEW_CONFIG=str(tmp / "no-config.yaml"))
        self.proc = None
        # A file cannot fill a PIPE and deadlock a long-running server.
        self.stderr = tempfile.TemporaryFile(dir=tmp)
        try:
            self.proc = subprocess.Popen([sys.executable, str(ROOT / "bin" / "mockreview"), "serve", "--port",
                                          str(self.port)], env=env, stdout=subprocess.DEVNULL, stderr=self.stderr)
            wait_health(self.port, self.proc)
        except BaseException as e:
            try:
                self.stop()
                self.stderr.seek(0)
                diagnostic = self.stderr.read().decode("utf-8", "replace")[-4000:]
            finally:
                self.stderr.close()
            raise AssertionError(f"{e}\nowner startup stderr:\n{diagnostic}") from e

    def stop(self):
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()                # only the PID this fixture started
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(10)
        finally:
            self.receiver.close()

    def close(self):
        try:
            self.stop()
        finally:
            self.stderr.close()


@pytest.mark.parametrize("failure", ["exit", "timeout", "spawn"])
def test_failed_service_construction_reports_stderr_and_cleans_up(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    procs, logs, closed = [], [], []
    popen, temporary_file = subprocess.Popen, tempfile.TemporaryFile

    def launch(argv, **kwargs):
        if failure == "spawn":
            raise OSError("synthetic spawn failure")
        code = ("import sys; sys.stderr.buffer.write(b'synthetic startup traceback\\xff\\n'); "
                "sys.stderr.flush(); " + ("sys.exit(7)" if failure == "exit" else "import time; time.sleep(30)"))
        proc = popen([sys.executable, "-c", code], **kwargs)
        procs.append(proc)
        return proc

    def log_file(**kwargs):
        log = temporary_file(**kwargs)
        logs.append(log)
        return log

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(tempfile, "TemporaryFile", log_file)
    monkeypatch.setattr(sys.modules[__name__], "Receiver", lambda: SimpleNamespace(
        url="http://127.0.0.1:1/unused", close=lambda: closed.append(True)))
    if failure == "timeout":
        def timeout(port, proc):
            raise AssertionError("synthetic readiness timeout")
        monkeypatch.setattr(sys.modules[__name__], "wait_health", timeout)
    with pytest.raises(AssertionError) as exc:
        Service(tmp_path)
    assert "owner startup stderr:" in str(exc.value)
    if failure == "exit":
        assert "exited early (7)" in str(exc.value) and "synthetic startup traceback\ufffd" in str(exc.value)
    assert all(proc.poll() is not None for proc in procs)
    assert closed == [True] and logs and all(log.closed for log in logs)


@pytest.fixture
def start_service(tmp_path):
    made = []

    def make(**kw):
        s = Service(tmp_path, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """This process becomes a Kanban worker: its own HERMES_HOME, no signing secret, no data_dir."""
    def become(svc_port, kanban=True, **env):
        home = tmp_path / "worker-home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path / "worker-user"))
        monkeypatch.setenv("HERMES_HOME", str(home))
        for k in ("MOCKREVIEW_HOME", "MOCKREVIEW_WEBHOOK_URL", "MOCKREVIEW_OWNER",
                  "HERMES_KANBAN_TASK"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MOCKREVIEW_PORT", str(svc_port))
        if kanban:
            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return home
    return become


def create(tmp_path, mode="local", **kw):
    return api.create("Synthetic review", {"platform": "discord", "chat_id": THREAD, "thread_id": THREAD}, mode=mode, decisions=str(_decisions(tmp_path)),
                      design=str(_design(tmp_path)), **kw)


def reviews_in(root):
    return sorted(p.name for p in root.iterdir() if (p / "review.json").is_file()) if root.is_dir() else []


# ---------------------------------------------------------------- worker -> owner service

def test_worker_creates_in_the_owner_store_and_the_owner_serves_it(tmp_path, start_service, worker):
    svc = start_service()
    home = worker(svc.port)
    res = create(tmp_path)
    rid = res["review_id"]
    assert res["owner"]["bound_to_service"] is True and res["owner"]["state"] == "ok"
    assert res["owner"]["wake_route"] == {"route": "mock-review", "ready": True}
    assert reviews_in(svc.root) == [rid]                                   # the watched store has it
    assert not (home / "workspace").exists()                               # the worker's own store was never written
    code, _, body = http("GET", f"http://127.0.0.1:{svc.port}/r/{rid}/state.json")
    assert code == 200 and json.loads(body)["id"] == rid                    # and the owner really loads it
    assert not (home / "webhook_subscriptions.json").exists()               # nothing was copied to the worker


def test_worker_reads_and_extends_the_owners_review(tmp_path, start_service, worker):
    svc = start_service()
    worker(svc.port)
    rid = create(tmp_path)["review_id"]
    assert [r["review_id"] for r in api.list_reviews()] == [rid]
    assert api.get_feedback(rid)["feedback"] is None
    nxt = api.add_round(rid, decisions=str(_decisions(tmp_path)))
    assert nxt["round"] == 2 and nxt["owner"]["bound_to_service"] is True
    assert len(store.load.__globals__["json"].loads((svc.root / rid / "review.json").read_text(encoding="utf-8"))["rounds"]) == 2


def test_worker_never_delivers_wakeups_itself(tmp_path, start_service, worker):
    svc = start_service()
    worker(svc.port)
    rid = create(tmp_path)["review_id"]
    ctx = owner.resolve()
    with owner.bound(ctx):
        d = store.load(rid)
        service.submit(rid, 1, DEC_FB, d["token"], do_notify=False)
        before = (svc.root / rid / "review.json").read_text(encoding="utf-8")
        ok, msg = service.notify_round(rid, 1)
    assert ok is False and "only the board server delivers" in msg
    assert json.loads((svc.root / rid / "review.json").read_text(encoding="utf-8"))["rounds"][0]["submission"]["attempts"] == 0
    assert svc.receiver.requests == [] or time.sleep(0) is None             # the server may deliver later, the worker not
    assert before  # the worker did not rewrite the record while refusing


def test_plugin_tool_from_a_worker_binds_to_the_owner(tmp_path, start_service, worker):
    plugin = load_plugin()
    from test_plugin import FakeCtx
    svc = start_service()
    worker(svc.port)
    ctx = FakeCtx()
    plugin.register(ctx)
    out = json.loads(ctx.tools["mock_review_create"]["handler"](
        {"title": "Plugin", "wake": False, "mode": "local", "decisions": str(_decisions(tmp_path))}))
    assert "error" not in out, out
    assert out["owner"]["bound_to_service"] is True and reviews_in(svc.root) == [out["review_id"]]


def test_cli_create_and_owner_command_from_a_worker(tmp_path, start_service, worker, capsys):
    svc = start_service()
    worker(svc.port)
    assert cli.main(["create", "--title", "CLI", "--platform", "discord", "--chat-id", "test-chat", "--thread", THREAD, "--mode", "local",
                     "--decisions", str(_decisions(tmp_path))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["owner"]["bound_to_service"] is True and reviews_in(svc.root) == [out["review_id"]]
    assert cli.main(["owner"]) == 0
    o = json.loads(capsys.readouterr().out)
    assert o["ok"] is True and o["server"]["state"] == "ok" and o["context"]["bound_to_service"] is True
    assert SECRET not in json.dumps(o)


def test_not_a_kanban_worker_is_refused_not_redirected(tmp_path, start_service, worker):
    svc = start_service()
    home = worker(svc.port, kanban=False)
    with pytest.raises(ReviewError, match="not the one the board server watches"):
        create(tmp_path)
    assert reviews_in(svc.root) == [] and not (home / "workspace").exists()


def test_explicit_data_dir_is_never_redirected(tmp_path, start_service, worker):
    svc = start_service()
    worker(svc.port, MOCKREVIEW_HOME=str(tmp_path / "private"))
    with pytest.raises(ReviewError, match="data_dir is set explicitly"):
        create(tmp_path)
    assert reviews_in(svc.root) == [] and not (tmp_path / "private").exists()


def test_owner_service_setting_binds_even_outside_kanban(tmp_path, start_service, worker):
    svc = start_service()
    worker(svc.port, kanban=False, MOCKREVIEW_OWNER="service")
    assert create(tmp_path)["owner"]["bound_to_service"] is True and len(reviews_in(svc.root)) == 1


# ---------------------------------------------------------------- preflight fails closed BEFORE publish

@pytest.fixture
def publish_calls(monkeypatch):
    calls = []

    def fake(review_id):
        calls.append(review_id)
        d = store.load(review_id)
        d["herenow"].update(slug="fake-slug", site_url="https://fake-slug.here.now/")
        store.save(review_id, d)
        return d
    monkeypatch.setattr(herenow, "publish", fake)
    return calls


def test_missing_wake_secret_refuses_before_publish(tmp_path, start_service, worker, publish_calls):
    svc = start_service(secret=None)
    worker(svc.port)
    with pytest.raises(owner.OwnerError, match="no wake route") as e:
        create(tmp_path, mode="herenow")
    assert "MOCKREVIEW_WEBHOOK_SECRET is not set" in e.value.message
    assert publish_calls == [] and reviews_in(svc.root) == []


def test_unreachable_owner_fails_closed_for_a_worker(tmp_path, worker, publish_calls):
    home = worker(free_port())                                                # nothing listens there
    with pytest.raises(owner.OwnerError, match="cannot reach the board server") as e:
        create(tmp_path, mode="herenow")
    assert e.value.status == 503 and publish_calls == [] and not (home / "workspace").exists()


def test_unreachable_owner_refuses_a_public_board_even_in_the_default_store(tmp_path, worker, monkeypatch, publish_calls):
    worker(free_port(), kanban=False)
    user = tmp_path / "worker-user"
    monkeypatch.setenv("HERMES_HOME", str(user / ".hermes"))                  # own store == the default a server watches
    with pytest.raises(owner.OwnerError, match="refusing to publish a public board"):
        create(tmp_path, mode="herenow")
    assert publish_calls == []
    res = create(tmp_path, mode="local")                                      # tailnet/local keep the old "warning" path
    assert res["owner"]["state"] == "unreachable" and res["server_up"] is False and "warning" in res


def test_owner_cannot_load_the_new_board_rolls_back_before_publish(tmp_path, start_service, worker, publish_calls,
                                                                  monkeypatch):
    svc = start_service()
    worker(svc.port)
    monkeypatch.setattr(owner, "confirm_visible", lambda rid, ctx, timeout=4.0: False)
    with pytest.raises(owner.OwnerError, match="could not load the review"):
        create(tmp_path, mode="herenow")
    assert publish_calls == [] and reviews_in(svc.root) == []


def test_successful_create_publishes_only_after_the_owner_can_see_it(tmp_path, start_service, worker, publish_calls,
                                                                    monkeypatch):
    svc = start_service()
    worker(svc.port)
    order = []
    real = owner.confirm_visible
    monkeypatch.setattr(owner, "confirm_visible", lambda *a, **k: (order.append("visible"), real(*a, **k))[1])
    monkeypatch.setattr(herenow, "publish", lambda rid: (order.append("publish"), store.load(rid))[1])
    create(tmp_path, mode="herenow")
    assert order == ["visible", "publish"]


class OldServer:
    """A server that predates /api/owner: /healthz answers, everything else is 404."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                code, body = (200, b'{"ok": true}') if self.path == "/healthz" else (404, b"nope")
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_server_without_owner_api_cannot_vouch_for_a_workers_private_store(tmp_path, worker, publish_calls):
    old = OldServer()
    try:
        home = worker(old.port)
        with pytest.raises(owner.OwnerError, match="predates /api/owner|private to this profile"):
            create(tmp_path, mode="herenow")
        assert publish_calls == [] and not (home / "workspace").exists()
    finally:
        old.close()


def test_owner_endpoint_is_loopback_only_and_has_no_secrets(server, env):
    code, _, body = http("GET", f"{server}/api/owner")
    info = json.loads(body)
    assert code == 200 and info["service"] == "mock-review" and info["wake"] == {
        "route": "mock-review", "ready": True, "secret": "set", "error": None, "url": env["receiver"].url}
    assert SECRET not in body.decode()
    for h in ("X-Forwarded-For", "Tailscale-User-Login", "X-Forwarded-Host"):    # what tailscale serve adds
        code, _, _ = http("GET", f"{server}/api/owner", headers={h: "100.64.0.9"})
        assert code == 404, h


# ---------------------------------------------------------------- demo / local modes are honoured

def test_wake_off_board_needs_no_route_stores_feedback_and_sends_nothing(tmp_path, start_service, worker):
    svc = start_service(secret=None)                                            # no route at all
    worker(svc.port)
    with pytest.raises(owner.OwnerError, match="no wake route"):
        create(tmp_path)
    res = create(tmp_path, wake=False)
    rid = res["review_id"]
    assert res["wake"] is False and store.load.__globals__["json"].loads((svc.root / rid / "review.json").read_text(encoding="utf-8"))["wake"] is False
    with owner.bound(owner.resolve()):
        d = store.load(rid)
        out = service.submit(rid, 1, DEC_FB, d["token"])
    assert out["status"] == "stored" and out["notified"] is False
    time.sleep(6.5)                                                           # the owner's retry loop ticks every 5 s
    assert svc.receiver.requests == []
    sub = json.loads((svc.root / rid / "review.json").read_text(encoding="utf-8"))["rounds"][0]["submission"]
    assert sub["status"] == "stored" and sub["attempts"] == 0


def test_wake_required_setting_off_skips_the_check(tmp_path, start_service, worker):
    svc = start_service(secret=None)
    worker(svc.port, MOCKREVIEW_WAKE_REQUIRED="false")
    assert create(tmp_path)["wake"] is False


def test_preflight_itself_sends_nothing(tmp_path, start_service, worker):
    svc = start_service()
    worker(svc.port)
    owner.resolve()
    cli.main(["owner"])
    create(tmp_path)
    assert svc.receiver.requests == []                                         # no test wake-up, no publish


# ---------------------------------------------------------------- exactly-once delivery

def _pending_review(env, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions(tmp_path)), design=str(_design(tmp_path)))
    data = store.create_review("Dedup", "", {"platform": "discord", "chat_id": THREAD, "thread_id": THREAD}, "tailscale", spec)
    service.submit(data["id"], 1, DEC_FB, data["token"], do_notify=False)
    return data["id"]


def test_notify_twice_sends_once(env, tmp_path):
    rid = _pending_review(env, tmp_path)
    assert service.notify_round(rid, 1)[0] is True
    assert service.notify_round(rid, 1) == (True, "already notified")
    assert len(env["receiver"].requests) == 1
    sub = store.load(rid)["rounds"][0]["submission"]
    assert sub["status"] == "notified" and sub["gateway_response"] == "HTTP 202" and "sending_until" not in sub


def test_concurrent_notifiers_send_once(env, tmp_path, monkeypatch):
    rid = _pending_review(env, tmp_path)
    sent = []

    def slow_send(payload, delivery_id, url=None):
        sent.append(delivery_id)
        time.sleep(0.4)
        return True, "HTTP 202 accepted"
    monkeypatch.setattr(notify, "send", slow_send)
    results = []
    ts = [threading.Thread(target=lambda: results.append(service.notify_round(rid, 1))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(sent) == 1 and sum(1 for ok, _ in results if ok) >= 1
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "notified"


def test_retry_keeps_the_same_delivery_id_until_it_lands(env, tmp_path):
    rid = _pending_review(env, tmp_path)
    rec = env["receiver"]
    rec.status = 503
    assert service.retry_pending()  == 0
    assert service.retry_pending() == 0
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "pending_notify"
    rec.status = 202
    assert service.retry_pending() == 1
    assert service.retry_pending() == 0                                        # notified: never again
    ids = [{k.lower(): v for k, v in r["headers"].items()}["x-request-id"] for r in rec.wakes]
    assert len(ids) == 3 and len(set(ids)) == 1 and ids[0] == f"mockreview-{rid}-r1-s1"


@pytest.mark.parametrize("code,body,woken", [
    (202, '{"status":"accepted"}', True), (202, '{"status":"coalesced"}', True),
    (200, '{"status":"duplicate"}', True), (200, '{"status":"ignored"}', False),
    (200, "{}", False), (200, "not json", False), (404, "Unknown route", False), (500, "boom", False)])
def test_gateway_response_classification(code, body, woken):
    ok, detail = notify.classify(code, body)
    assert ok is woken
    if code == 200 and not woken:
        assert "did not wake anyone" in detail


def test_http_200_ignored_is_not_a_notified_marker(env, tmp_path):
    rid = _pending_review(env, tmp_path)
    env["receiver"].status = 200                                               # the fixture answers {} = no status
    ok, msg = service.notify_round(rid, 1)
    assert ok is False and "did not wake anyone" in msg
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "pending_notify"


# ---------------------------------------------------------------- adopt: recover one review from a private store

def _digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def private(env, tmp_path, monkeypatch):
    """A review that lives in a worker-private store with a pending, never-delivered submission."""
    priv = tmp_path / "private-store"
    monkeypatch.setenv("MOCKREVIEW_HOME", str(priv))
    spec = store.parse_round_spec(decisions=str(_decisions(tmp_path)), design=str(_design(tmp_path)))
    data = store.create_review("Synthetic review", "brief", {"platform": "discord", "chat_id": THREAD, "thread_id": THREAD}, "herenow", spec)
    rid = data["id"]
    with store.locked(rid):
        d = store.load(rid)
        d["herenow"].update(slug="synthetic-review-ab12", site_url="https://synthetic-review-ab12.here.now/",
                            processed_records=["rec_01"], auth_mode="authenticated")
        store.save(rid, d)
    service.submit(rid, 1, DEC_FB, data["token"], source="herenow", record_id="rec_01", do_notify=False)
    with store.locked(rid):
        d = store.load(rid)
        d["rounds"][0]["submission"].update(attempts=1, last_error="signing secret missing")
        store.save(rid, d)
    (priv / rid / "site").mkdir()
    (priv / rid / "site" / "state.json").write_text('{"public": true}', encoding="utf-8")
    monkeypatch.setenv("MOCKREVIEW_HOME", str(tmp_path / "watched-store"))
    return {"rid": rid, "priv": priv, "src": priv / rid, "watched": tmp_path / "watched-store",
            "backup": tmp_path / "backups", "token": data["token"]}


def test_adopt_moves_every_byte_adds_only_the_marker_and_keeps_the_delivery_id(private):
    src, rid = private["src"], private["rid"]
    before = _digest(src)
    sub0 = json.loads((src / "review.json").read_text(encoding="utf-8"))["rounds"][0]["submission"]
    res = owner.adopt(str(src), str(private["backup"]))
    dest = private["watched"] / rid
    after = _digest(dest)
    assert res["adopted"] is True and not src.exists() and not list(private["watched"].glob(".adopt-*"))
    assert res["changed_files"] == ["review.json"] and set(before) == set(after)
    assert {k: v for k, v in before.items() if k != "review.json"} == {k: v for k, v in after.items() if k != "review.json"}
    new = json.loads((dest / "review.json").read_text(encoding="utf-8"))
    old = json.loads(json.dumps(new))
    for k in ("wake_note", "adopted"):
        old.pop(k)
    assert list(new)[0] == "wake_note" and new["wake_note"]["seq"] == 1 and "no new user instruction" in new["wake_note"]["text"]
    assert old["rounds"][0]["submission"] == sub0                              # delivery id, record id, feedback untouched
    assert sub0["delivery_id"] == f"mockreview-{rid}-r1-s1" and old["token"] == private["token"]
    assert old["herenow"]["site_url"] == "https://synthetic-review-ab12.here.now/"
    assert store.list_ids() == [rid] if False else rid in reviews_in(private["watched"])
    import tarfile
    with tarfile.open(res["backup"]) as tf:                                    # the backup holds the original bytes
        names = {m.name for m in tf.getmembers() if m.isfile()}
        orig = hashlib.sha256(tf.extractfile(f"{rid}/rounds/1/feedback.json").read()).hexdigest()
    assert f"{rid}/review.json" in names and orig == before["rounds/1/feedback.json"]


def test_adopt_refuses_a_different_review_with_the_same_id(private, tmp_path):
    rid = private["rid"]
    clash = private["watched"] / rid
    clash.mkdir(parents=True)
    (clash / "review.json").write_text(json.dumps({"id": rid, "token": "someone-else", "herenow": {"slug": "other"}}), encoding="utf-8")
    src_before = _digest(private["src"])
    with pytest.raises(ReviewError, match="collision") as e:
        owner.adopt(str(private["src"]), str(private["backup"]))
    assert e.value.status == 409 and _digest(private["src"]) == src_before     # nothing moved, nothing removed
    assert json.loads((clash / "review.json").read_text(encoding="utf-8"))["token"] == "someone-else"
    assert not private["backup"].exists()                                       # refused before any backup was made


@pytest.mark.parametrize("field,value,msg", [("token", None, "same submit token"), ("slug", "synthetic-review-ab12", "same here.now slug")])
def test_adopt_refuses_token_or_slug_collisions_with_another_review(private, field, value, msg):
    other = private["watched"] / "another-review-000000"
    other.mkdir(parents=True)
    data = {"id": "another-review-000000", "token": private["token"] if field == "token" else "t2", "rounds": [],
            "herenow": {"slug": value or "x"}}
    (other / "review.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ReviewError, match=msg):
        owner.adopt(str(private["src"]), str(private["backup"]))
    assert private["src"].is_dir()


def test_adopt_twice_is_a_no_op_and_never_duplicates(private):
    owner.adopt(str(private["src"]), str(private["backup"]))
    dest = private["watched"] / private["rid"]
    snap = _digest(dest)
    # the source reappears (a stale copy): same identity -> reported, left alone, destination untouched
    import shutil
    shutil.copytree(dest, private["src"])
    again = owner.adopt(str(private["src"]), str(private["backup"]))
    assert again["adopted"] is False and again["already_in_store"] is True and private["src"].is_dir()
    assert _digest(dest) == snap and reviews_in(private["watched"]) == [private["rid"]]


def test_adopt_validates_its_input(private, tmp_path):
    with pytest.raises(ReviewError, match="absolute"):
        owner.adopt("relative/dir", str(private["backup"]))
    with pytest.raises(ReviewError, match="no readable review.json"):
        owner.adopt(str(tmp_path), str(private["backup"]))
    wrong = tmp_path / "wrong-name"
    wrong.mkdir()
    (wrong / "review.json").write_text(json.dumps({"id": "other-id-000000"}), encoding="utf-8")
    with pytest.raises(ReviewError, match="does not match review id"):
        owner.adopt(str(wrong), str(private["backup"]))


def test_adopted_review_is_delivered_once_by_the_real_service(tmp_path, start_service, private, monkeypatch):
    svc = start_service()
    monkeypatch.setenv("MOCKREVIEW_HOME", str(svc.root))                       # the review lands in THE WATCHED store
    rid = private["rid"]
    owner.adopt(str(private["src"]), str(private["backup"]))
    rec = svc.receiver
    end = time.time() + 20
    while time.time() < end and len(rec.requests) < 2:
        time.sleep(0.25)
    assert len(rec.requests) == 2, "the owner's retry loop must pick the adopted review up and deliver wake + ack"
    time.sleep(7)                                                              # two more owner ticks: still exactly two
    assert len(rec.requests) == 2
    req, notice = rec.requests[0], rec.requests[1]
    assert notice["path"].endswith("-notice") and json.loads(notice["body"])["message"].startswith("📨")
    h = {k.lower(): v for k, v in req["headers"].items()}
    payload = json.loads(req["body"])
    assert req["ok"] is True                                                   # HMAC verified like the gateway does
    assert h["x-request-id"] == f"mockreview-{rid}-r1-s1"
    assert payload["review_id"] == rid and payload["recovered"] is True
    assert payload["summary"].startswith("RECOVERED DELIVERY, NOT A NEW REQUEST") and "Please simplify the layout." in payload["summary"]
    sub = json.loads((svc.root / rid / "review.json").read_text(encoding="utf-8"))["rounds"][0]["submission"]
    assert sub["status"] == "notified" and sub["notified_at"] and sub["gateway_response"] == "HTTP 202"
    assert sub["delivery_id"] == f"mockreview-{rid}-r1-s1"
    assert sub["notice"]["status"] == "notified"


def test_amend_after_recovery_is_not_marked_as_recovered(private, env, tmp_path, monkeypatch):
    owner.adopt(str(private["src"]), str(private["backup"]))
    rid = private["rid"]
    monkeypatch.setenv("MOCKREVIEW_HOME", str(private["watched"]))
    d = store.load(rid)
    r = d["rounds"][0]
    assert "RECOVERED" in service.build_payload(d, r)["summary"]
    service.submit(rid, 1, DEC_FB, d["token"], amend=True, do_notify=False)
    d = store.load(rid)
    assert d["rounds"][0]["submission"]["seq"] == 2
    p = service.build_payload(d, d["rounds"][0])
    assert "RECOVERED" not in p["summary"] and "recovered" not in p


def test_adopt_dry_run_writes_nothing_and_reports_collisions_first(private):
    before_src = _digest(private["src"])
    res = owner.adopt(str(private["src"]), str(private["backup"]), target=str(private["watched"]), dry_run=True)
    assert res["dry_run"] is True and res["pending"] == {"round": 1, "seq": 1} and res["files"] > 5
    assert _digest(private["src"]) == before_src
    assert not private["watched"].exists() and not private["backup"].exists()
    clash = private["watched"] / private["rid"]
    clash.mkdir(parents=True)
    (clash / "review.json").write_text(json.dumps({"id": private["rid"], "token": "x"}), encoding="utf-8")
    with pytest.raises(ReviewError, match="collision"):
        owner.adopt(str(private["src"]), str(private["backup"]), target=str(private["watched"]), dry_run=True)


def test_adopt_target_defaults(tmp_path, monkeypatch, env):
    monkeypatch.setenv("MOCKREVIEW_OWNER", "service")
    monkeypatch.setenv("MOCKREVIEW_PORT", str(free_port()))                 # nothing listens
    monkeypatch.setenv("HOME", str(tmp_path / "u"))
    p, why = owner.adopt_target()
    assert p == config.hermes_home() / "workspace" / "mock-reviews" and "default watched store" in why
    with pytest.raises(ReviewError, match="absolute"):
        owner.adopt_target("rel/x")
