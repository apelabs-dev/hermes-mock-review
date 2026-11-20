"""The generic headless-browser harness: handshake deadlines, secret-free child env, browser discovery, isolated
launch and cleanup, and the observer's network accounting. These are the safety regressions kept when the old
host-specific checker went away; they cover tests/browserkit.py. Only the Chrome temp-dir regressions start a real
browser, and they skip when none is installed.
"""

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

import browserkit as bk

pytestmark = pytest.mark.browser

posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups and shell scripts")

FAKE_BROWSER = """#!/bin/sh
for a in "$@"; do case "$a" in --user-data-dir=*) d="${a#--user-data-dir=}";; esac; done
echo "$@" > "$d/argv.txt"
sleep 300 &
echo $! > "$d/helper.pid"
printf '9222\\n/devtools/browser/x\\n' > "$d/DevToolsActivePort"
wait
"""

# Like FAKE_BROWSER, but it also does what Chrome on macOS does on every launch: make a com.google.Chrome.XXXXXX
# dir in the per-user temp dir holding SingletonSocket/SingletonCookie, and link <profile>/SingletonSocket at it.
SINGLETON_BROWSER = """#!/bin/sh
for a in "$@"; do case "$a" in --user-data-dir=*) d="${a#--user-data-dir=}";; esac; done
t=$(mktemp -d "$SINGLETON_TMP/com.google.Chrome.XXXXXX")
: > "$t/SingletonSocket"
: > "$t/SingletonCookie"
ln -s "$t/SingletonSocket" "$d/SingletonSocket"
printf '9222\\n/devtools/browser/x\\n' > "$d/DevToolsActivePort"
sleep 300
"""


def _darwin_temp_dir():
    """The per-user temp dir Chrome uses for its singleton dir on macOS, or None where Chrome does not make one."""
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() or None


# The singleton dirs a launch owns: com.google.Chrome.XXXXXX, holding SingletonSocket/SingletonCookie. Chrome also
# makes empty com.google.Chrome.<binary>_url_fetcher_.XXXXXX scratch dirs (component updates) that no launch can claim
# from its profile, so they are not asserted on here; they hold no singleton and a suite-wide sweep clears them.
SINGLETON_NAME = re.compile(r"com\.google\.Chrome\.[A-Za-z0-9]{6}$")


class _FakeSocket:
    """The send/recv half of a WebSocket for one canned DevTools answer (None: the deadline passes silently)."""

    def __init__(self, reply):
        self.reply = None if reply is None else json.dumps(reply)
        self.sent = []

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        reply, self.reply = self.reply, None
        return reply

    def close(self):
        pass


class _ScriptedSocket:
    """Answers every Cdp.call by its own id, in order; methods in `refuse` get a protocol error."""

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.sent = []
        self.replies = []

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg.get("method") in self.refuse:
            reply = {"id": msg["id"], "error": {"message": "refused"}}
        else:
            reply = {"id": msg["id"], "result": {}}
        self.replies.append(json.dumps(reply))

    def recv(self, timeout):
        return self.replies.pop(0) if self.replies else None

    def close(self):
        pass


def _new_singleton_dirs(before, temp):
    return {n for n in set(os.listdir(temp)) - before
            if SINGLETON_NAME.fullmatch(n) or os.path.exists(os.path.join(temp, n, "SingletonSocket"))}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _gone(pid: int, wait=5.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_handshake_that_never_completes_raises_at_the_deadline_and_closes(monkeypatch):
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()[0]), daemon=True).start()
    made = []
    real_close = bk.WebSocket.close
    monkeypatch.setattr(bk.WebSocket, "close", lambda self: (made.append(self), real_close(self))[1])
    started = time.monotonic()
    try:
        with pytest.raises(bk.BrowserCheckError, match="did not complete the connection in time"):
            bk.WebSocket("127.0.0.1", srv.getsockname()[1], "/devtools/browser/x", timeout=0.5)
    finally:
        srv.close()
        for c in held:
            c.close()
    assert time.monotonic() - started < 5 and made and made[0].sock.fileno() == -1


def test_child_env_drops_secret_looking_variables(monkeypatch):
    monkeypatch.setenv("SOME_PASSWORD", "p")
    monkeypatch.setenv("MY_TOKEN", "t")
    monkeypatch.setenv("SOME_API_KEY", "k")
    monkeypatch.setenv("KEEPME", "ok")
    env = bk._child_env()
    assert "KEEPME" in env and not {"SOME_PASSWORD", "MY_TOKEN", "SOME_API_KEY"} & set(env)


def test_find_browser_env_override_and_missing(monkeypatch, tmp_path):
    exe = tmp_path / "fakechrome"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv(bk.BROWSER_ENV, str(exe))
    assert bk.find_browser() == str(exe)
    monkeypatch.setenv(bk.BROWSER_ENV, str(tmp_path / "nope"))
    with pytest.raises(bk.BrowserUnavailable, match="not an executable"):
        bk.find_browser()
    monkeypatch.delenv(bk.BROWSER_ENV)
    monkeypatch.setattr(bk.shutil, "which", lambda name: None)
    monkeypatch.setattr(bk, "BROWSER_PATHS", ())
    with pytest.raises(bk.BrowserUnavailable, match="no Chrome, Chromium or Edge was found"):
        bk.find_browser()


def test_no_browser_is_an_error_not_a_pass(monkeypatch):
    monkeypatch.delenv(bk.BROWSER_ENV, raising=False)
    monkeypatch.setattr(bk.shutil, "which", lambda name: None)
    monkeypatch.setattr(bk, "BROWSER_PATHS", ())
    with pytest.raises(bk.BrowserUnavailable) as ei:
        bk.find_browser()
    assert isinstance(ei.value, bk.BrowserCheckError)


@posix_only
def test_chrome_launch_isolated_group_and_cleanup_even_on_error(tmp_path):
    exe = tmp_path / "chrome"
    exe.write_text(FAKE_BROWSER, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    c = bk.Chrome(executable=str(exe))
    with pytest.raises(RuntimeError):
        with c as (host, port, path):
            assert (host, port, path) == ("127.0.0.1", 9222, "/devtools/browser/x")
            pid, profile = c.proc.pid, c.profile
            assert os.getpgid(pid) == pid != os.getpgid(os.getpid())        # its own process group
            argv = Path(profile, "argv.txt").read_text(encoding="utf-8")
            assert "--headless=new" in argv and "--remote-debugging-port=0" in argv and f"--user-data-dir={profile}" in argv
            # without these Chrome's network can stall on the macOS keychain when HOME is not a real home
            assert "--use-mock-keychain" in argv and "--password-store=basic" in argv
            helper = int(Path(profile, "helper.pid").read_text(encoding="utf-8"))
            assert _alive(helper)
            raise RuntimeError("the check blew up mid-run")
    assert _gone(pid) and _gone(helper), "browser or its helper survived"
    assert not os.path.exists(profile)


@posix_only
def test_chrome_that_exits_early_or_never_opens_its_port_is_cleaned_up(tmp_path):
    early = tmp_path / "early"
    early.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    early.chmod(0o755)
    c = bk.Chrome(executable=str(early))
    with pytest.raises(bk.BrowserCheckError, match="exited before"):
        c.__enter__()
    assert c.profile is None and c.proc is None
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
    silent.chmod(0o755)
    c = bk.Chrome(executable=str(silent), startup=0.5)
    with pytest.raises(bk.BrowserCheckError, match="debugging port in time"):
        c.__enter__()
    assert c.profile is None and c.proc is None
    c = bk.Chrome(executable=str(tmp_path / "does-not-exist"))
    with pytest.raises(bk.BrowserUnavailable, match="could not be started"):
        c.__enter__()
    assert c.profile is None


def test_chrome_never_adds_security_relaxing_flags():
    src = Path(bk.__file__).read_text(encoding="utf-8")
    assert "--disable-web-security" not in src and "--disable-site-isolation" not in src
    # --disable-features is how site isolation gets switched off, so it is banned except for the one switch that stops
    # macOS Chrome cloning its app bundle at every launch (nothing security-related). The match is the whole flag.
    assert re.findall(r"disable-features\S*", src) == ['disable-features=MacAppCodeSignClone",']


@posix_only
def test_chrome_teardown_removes_the_singleton_dir_its_own_launch_made(tmp_path, monkeypatch):
    """macOS Chrome ignores TMPDIR and puts SingletonSocket/SingletonCookie in a com.google.Chrome.XXXXXX dir in the
    per-user temp dir, linked from <profile>/SingletonSocket; a killed Chrome never removes it. Teardown must remove
    the one dir its own launch linked, and nothing else."""
    exe = tmp_path / "chrome"
    exe.write_text(SINGLETON_BROWSER, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    temp = tmp_path / "T"                                   # stands in for the per-user temp dir
    temp.mkdir()
    decoy = temp / "com.google.Chrome.DECOY"                # another run's dir: never touched
    decoy.mkdir()
    monkeypatch.setenv("SINGLETON_TMP", str(temp))          # not secret-looking, so _child_env keeps it
    c = bk.Chrome(executable=str(exe))
    with c as (host, port, path):
        assert (host, port, path) == ("127.0.0.1", 9222, "/devtools/browser/x")
        link = os.path.join(c.profile, "SingletonSocket")
        assert os.path.islink(link), "the fixture did not link the singleton socket"
        made = os.path.dirname(os.readlink(link))
        assert made != c.profile and os.path.basename(made).startswith("com.google.Chrome.") and os.path.isdir(made)
    assert not os.path.exists(made), "the Chrome singleton temp dir this launch made survived teardown"
    assert decoy.is_dir(), "teardown removed a com.google.Chrome.* dir it did not create"


def test_a_real_chrome_launch_leaves_no_singleton_dir_behind():
    """The same leak with the real browser: ~7 launches a minute from the board tests, ~1k leftover dirs an hour."""
    temp = _darwin_temp_dir()
    if not temp:
        pytest.skip("only macOS Chrome uses a com.google.Chrome.* dir in the per-user temp dir")
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    before = set(os.listdir(temp))
    c = bk.Chrome(startup=60)
    with c as (host, port, path):
        link = os.path.join(c.profile, "SingletonSocket")
        assert os.path.islink(link), "Chrome did not link its singleton socket into the profile"
        made = os.path.dirname(os.readlink(link))
        assert os.path.basename(made).startswith("com.google.Chrome.") and os.path.isdir(made)
    # Count only the dir this launch made: the browser pool runs other modules against the same per-user temp dir, so
    # its other new entries are their live Chromes' singleton dirs, never this launch's leak.
    left = _new_singleton_dirs(before, temp) & {os.path.basename(made)}
    assert not left, f"{made} survived teardown"


def test_chrome_budget_defaults_to_three_and_never_exceeds_four(monkeypatch):
    monkeypatch.delenv(bk.CHROMES_ENV, raising=False)
    assert bk.chrome_budget() == 3
    for raw, want in (("1", 1), ("2", 2), ("4", 4), ("9", 4), ("0", 1), ("-2", 1), ("nonsense", 3), ("", 3)):
        monkeypatch.setenv(bk.CHROMES_ENV, raw)
        assert bk.chrome_budget() == want, raw


def test_a_run_holds_its_budget_and_the_machine_never_holds_more_than_four(monkeypatch, tmp_path):
    slots_dir = tmp_path / "slots"                     # its own registry, apart from anything else in the test tmpdir
    monkeypatch.setenv(bk.CHROME_SLOTS_ENV, str(slots_dir))
    monkeypatch.setenv(bk.CHROME_RUN_ENV, "budget-run")
    monkeypatch.setenv(bk.CHROMES_ENV, "1")
    first = bk._acquire_chrome_slot(timeout=1)
    try:
        with pytest.raises(bk.BrowserCheckError, match="no free Chrome slot"):
            bk._acquire_chrome_slot(timeout=0.2)
    finally:
        bk._release_chrome_slot(first)
    monkeypatch.setenv(bk.CHROMES_ENV, "4")
    slots = [bk._acquire_chrome_slot(timeout=1) for _ in range(4)]
    try:
        with pytest.raises(bk.BrowserCheckError, match="no free Chrome slot"):
            bk._acquire_chrome_slot(timeout=0.2)
    finally:
        for slot in slots:
            bk._release_chrome_slot(slot)
    assert os.listdir(slots_dir) == []         # every slot and the registry lock are gone


def test_a_slot_left_by_a_dead_process_is_reclaimed(monkeypatch, tmp_path):
    monkeypatch.setenv(bk.CHROME_SLOTS_ENV, str(tmp_path))
    monkeypatch.setenv(bk.CHROME_RUN_ENV, "after-crash")
    monkeypatch.setenv(bk.CHROMES_ENV, "1")
    stale = tmp_path / "0-dead-000000.slot"
    stale.write_text("0 crashed-run 0\n", encoding="utf-8")
    slot = bk._acquire_chrome_slot(timeout=1)
    try:
        assert not stale.exists()
        assert bk._pid_alive(os.getpid()) and not bk._pid_alive(0)
    finally:
        bk._release_chrome_slot(slot)


def test_network_failures_and_5xx_are_counted_like_4xx():
    obs = bk.Observer()
    obs.top_frame = "TOP"
    obs.reset()
    obs.handle({"method": "Network.requestWillBeSent", "params": {"requestId": "1", "request": {"url": "https://x.test/a.css"},
                                                                  "type": "Stylesheet"}})
    obs.handle({"method": "Network.responseReceived", "params": {"requestId": "1", "type": "Stylesheet",
                                                                 "response": {"url": "https://x.test/a.css", "status": 503}}})
    obs.handle({"method": "Network.requestWillBeSent", "params": {"requestId": "2", "request": {"url": "https://x.test/b.woff2"},
                                                                  "type": "Font"}})
    obs.handle({"method": "Network.loadingFailed", "params": {"requestId": "2", "errorText": "net::ERR_CONNECTION_RESET"}})
    obs.handle({"method": "Network.requestWillBeSent", "params": {"requestId": "3", "frameId": "TOP",
                                                                  "request": {"url": "https://x.test/favicon.ico"}}})
    obs.handle({"method": "Network.responseReceived", "params": {"requestId": "3", "response": {"url": "https://x.test/favicon.ico", "status": 404}}})
    obs.handle({"method": "Network.requestWillBeSent", "params": {"requestId": "4", "request": {"url": "data:image/png;base64,AAAA"}}})
    assert [(f["kind"], f["status"]) for f in obs.failures] == [("http", 503), ("network", 0)]
    assert obs.total == 3 and "ERR_CONNECTION_RESET" in obs.failures[1]["error"]


def test_nothing_counted_before_the_unlock():
    obs = bk.Observer()
    obs.handle({"method": "Network.requestWillBeSent", "params": {"requestId": "1", "request": {"url": "https://x.test/"}}})
    obs.handle({"method": "Network.responseReceived", "params": {"requestId": "1", "response": {"url": "https://x.test/", "status": 401}}})
    assert obs.failures == [] and obs.total == 0           # nothing before reset() is counted


def test_a_page_exception_names_the_throwing_expression_and_stays_bounded():
    details = {"text": "Uncaught",
               "exception": {"description": "TypeError: Cannot read properties of null (reading 'getBoundingClientRect')"
                                            "\n    at <anonymous>:1:91\n    at Array.forEach (<anonymous>)"}}
    cdp = bk.Cdp(_FakeSocket({"id": 1, "result": {"result": {}, "exceptionDetails": details}}), bk.Observer())
    with pytest.raises(bk.BrowserCheckError) as ei:
        cdp.evaluate("document.querySelector('.dzbar').getBoundingClientRect()")
    message = str(ei.value)
    assert isinstance(ei.value, bk.PageScriptError)      # a wait may treat this as "not yet"; a dead socket may not
    assert "Uncaught" in message and "TypeError" in message and "getBoundingClientRect" in message, message
    assert "\n" not in message and len(message) <= 360, message
    huge = bk.Cdp(_FakeSocket({"id": 1, "result": {"result": {}, "exceptionDetails": {
        "text": "Uncaught", "exception": {"description": "Error: " + "x" * 5000 + "\nmore"}}}}), bk.Observer())
    with pytest.raises(bk.BrowserCheckError) as ei:
        huge.evaluate("throw new Error('x')")
    assert len(str(ei.value)) <= 360 and "\n" not in str(ei.value)


def test_cdp_alive_is_false_when_the_debugging_port_stops_answering():
    assert bk.Cdp(_FakeSocket(None), bk.Observer()).alive(timeout=0.05) is False
    assert bk.Cdp(_FakeSocket({"id": 1, "result": {"product": "Chrome"}}), bk.Observer()).alive(timeout=0.05) is True


def test_flush_resumes_a_target_even_when_one_enable_is_refused():
    """An OOPIF is paused until Runtime.runIfWaitingForDebugger. One timed-out domain must not wedge it: the frame
    would never load and its execution context would never be reported again."""
    sock, obs = _ScriptedSocket(refuse={"Page.enable"}), bk.Observer()
    obs.attach.append("S1")
    bk.Cdp(sock, obs).flush()
    methods = [msg["method"] for msg in sock.sent]
    assert methods == ["Network.enable", "Runtime.enable", "Page.enable", "Runtime.runIfWaitingForDebugger"], methods


def test_reannounce_contexts_asks_every_session_and_is_throttled():
    """A frame whose context the event map lost is rebuilt by disabling and enabling Runtime (a plain re-enable only
    reports on the first enable), and a polling loop cannot flood the browser with it."""
    obs = bk.Observer()
    obs.frames["F"] = ("https://board.example.test/r/x/rounds/1/a/blocks/copy.html", "S1")
    obs.contexts["F"] = ("S1", 1)
    sock = _ScriptedSocket()
    cdp = bk.Cdp(sock, obs)
    assert cdp.reannounce_contexts() == ["S1"]
    assert [msg["method"] for msg in sock.sent] == ["Runtime.disable", "Runtime.enable"]
    assert cdp.reannounce_contexts() == []                    # the same session set inside the throttle window
    assert cdp.reannounce_contexts(min_interval=0) == ["S1"]


@posix_only
def test_a_browser_that_dies_during_setup_is_replaced_and_a_page_failure_is_not_retried():
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    attempts, procs = [], []

    def dies_once(live):
        attempts.append(live)
        procs.append(live.chrome.proc)
        if len(attempts) == 1:
            live.kill()
            raise bk.BrowserCheckError("the browser died during setup")
        return live.cdp.call("Browser.getVersion")["product"]

    assert bk.with_live_browser(dies_once, lambda live, ctx: ctx, startup=60)
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    assert procs[0].poll() is not None and attempts[0].ws.sock.fileno() == -1

    page_calls = []

    def bring_up(live):
        return live.cdp.call("Browser.getVersion")["product"]

    def page_failure(live, ctx):
        page_calls.append(live)
        raise bk.BrowserCheckError("a page script check failed in the browser")

    with pytest.raises(bk.BrowserCheckError, match="page script check failed"):
        bk.with_live_browser(bring_up, page_failure, startup=60)
    assert len(page_calls) == 1, "a live browser's page failure must not be retried"


@pytest.mark.parametrize("message,gone", [
    ("the browser did not answer Page.enable in time", True),
    ("the browser did not answer Runtime.evaluate in time", False),
    ("the browser closed its debugging connection", True),
    ("the browser exited before it opened its debugging port", True),
    ("the browser did not open its debugging port in time", True),
    ("the browser's debugging port did not complete the connection in time", True),
    ("the browser's debugging port did not accept the connection", True),
    ("the browser refused Page.enable: 'x'", False),
    ("no free Chrome slot: this run may hold 2 and the machine at most 4; waited 900s", False),
    ("the browser sent a message that is too large", False),
    ("a page script check failed in the browser: TypeError: x", False),
])
def test_only_the_transport_message_family_is_replaced(message, gone):
    error = (bk.PageScriptError(message) if message.startswith("a page script") else bk.BrowserCheckError(message))
    assert bk._transport_failure(error) is gone
    assert bk._transport_failure(bk.BrowserUnavailable("no Chrome, Chromium or Edge found")) is False
    assert bk._transport_failure(ConnectionResetError()) is True


@posix_only
def test_a_transport_timeout_on_a_live_browser_is_replaced_while_a_refusal_is_not():
    """Page.enable is answered per target: a wedged renderer keeps Browser.getVersion alive. That bring-up timeout must
    replace the browser, while a refusal and a second wedged launch must still raise."""
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    calls = []

    def timeout_once(live):
        calls.append(live)
        assert live.alive() is True, "the wedged launch must still answer Browser.getVersion"
        if len(calls) == 1:
            raise bk.BrowserCheckError("the browser did not answer Page.enable in time")
        return live.cdp.call("Browser.getVersion")["product"]

    assert bk.with_live_browser(timeout_once, lambda live, ctx: ctx, startup=60)
    assert len(calls) == 2 and calls[0] is not calls[1]

    refused = []

    def refusal(live):
        refused.append(live)
        assert live.alive() is True
        raise bk.BrowserCheckError("the browser refused Page.enable: x")

    with pytest.raises(bk.BrowserCheckError, match="refused Page.enable"):
        bk.with_live_browser(refusal, lambda live, ctx: ctx, startup=60)
    assert len(refused) == 1, "a CDP refusal from a live browser must not be retried"

    wedged = []

    def always_timeout(live):
        wedged.append(live)
        raise bk.BrowserCheckError("the browser did not answer Page.enable in time")

    with pytest.raises(bk.BrowserCheckError, match="did not answer Page.enable"):
        bk.with_live_browser(always_timeout, lambda live, ctx: ctx, startup=60)
    assert len(wedged) == 2, "a second wedged launch must still raise; never a third launch"


@posix_only
def test_a_bring_up_timeout_is_replaced_and_warns_once(capsys):
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    calls, bodies = [], []

    def bring_up(live):
        calls.append(live)
        if len(calls) == 1:
            raise bk.BrowserCheckError("the browser did not answer Page.enable in time")
        return "session-1"

    def body(live, ctx):
        bodies.append((live, ctx))
        return live.cdp.call("Browser.getVersion")["product"]

    assert bk.with_live_browser(bring_up, body, startup=60)
    assert len(calls) == 2 and calls[0] is not calls[1]
    assert len(bodies) == 1 and bodies[0][1] == "session-1"
    err = capsys.readouterr().err
    warned = [line for line in err.splitlines() if line.startswith("browserkit: replacing")]
    assert len(warned) == 1 and "(1/2)" in warned[0] and "did not answer Page.enable in time" in warned[0], err



@posix_only
def test_a_failed_alive_probe_replaces_without_running_bring_up_or_the_body(capsys, monkeypatch):
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    probes, bring_ups, bodies = [], [], []
    real_alive = bk.LiveBrowser.alive

    def alive(live):
        probes.append(live)
        return False if len(probes) == 1 else real_alive(live)

    monkeypatch.setattr(bk.LiveBrowser, "alive", alive)

    def bring_up(live):
        bring_ups.append(live)
        return "session-1"

    def body(live, ctx):
        bodies.append((live, ctx))
        return live.cdp.call("Browser.getVersion")["product"]

    assert bk.with_live_browser(bring_up, body, startup=60)
    assert len(probes) == 2, "the probe must run once on every launch"
    assert bring_ups == [probes[1]], "a launch whose probe failed must not run bring_up"
    assert len(bodies) == 1 and bodies[0][0] is probes[1] and bodies[0][1] == "session-1"
    warned = [line for line in capsys.readouterr().err.splitlines() if line.startswith("browserkit: replacing")]
    assert len(warned) == 1 and "(1/2)" in warned[0] and "did not answer before setup" in warned[0], warned


@posix_only
def test_a_body_timeout_is_never_replaced_and_does_not_warn(capsys):
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    launches, bodies = [], []

    def bring_up(live):
        launches.append(live)
        return "session-1"

    def body(live, ctx):
        bodies.append(live)
        raise bk.BrowserCheckError("the browser did not answer Runtime.evaluate in time")

    with pytest.raises(bk.BrowserCheckError, match="did not answer Runtime.evaluate in time"):
        bk.with_live_browser(bring_up, body, startup=60)
    assert len(launches) == 1 and len(bodies) == 1, "a body failure must never rerun the scenario"
    assert "browserkit: replacing" not in capsys.readouterr().err


@posix_only
def test_a_transport_failure_in_the_body_is_raised_and_never_replaced(capsys):
    """The body runs outside the bring-up guard. Moving it inside would catch this transport-class error and launch
    a second browser, rerunning a scenario that already ran."""
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    launches, bodies = [], []

    def bring_up(live):
        launches.append(live)
        return "session-1"

    def body(live, ctx):
        bodies.append(live)
        raise bk.BrowserCheckError("the browser did not answer Page.navigate in time")

    with pytest.raises(bk.BrowserCheckError, match="did not answer Page.navigate in time"):
        bk.with_live_browser(bring_up, body, startup=60)
    assert len(launches) == 1 and len(bodies) == 1, "a body failure must never launch a replacement"
    assert "browserkit: replacing" not in capsys.readouterr().err


@posix_only
def test_a_wedged_page_in_the_body_costs_one_launch_and_raises(capsys):
    """The reviewer's mid-run probe inverted: a hung Runtime.evaluate raises, it does not replace and rerun."""
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    launches, bodies = [], []

    def bring_up(live):
        launches.append(live)
        tid = live.cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = live.cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        live.cdp.call("Page.enable", session=sid)
        return sid

    def body(live, sid):
        bodies.append(live)
        live.cdp.call("Runtime.evaluate", {"expression": "while(true){}", "returnByValue": True},
                      session=sid, timeout=2)

    with pytest.raises(bk.BrowserCheckError, match="did not answer Runtime.evaluate in time"):
        bk.with_live_browser(bring_up, body, startup=60)
    assert len(launches) == 1 and len(bodies) == 1
    assert "browserkit: replacing" not in capsys.readouterr().err


@posix_only
def test_a_bring_up_refusal_is_not_replaced_and_two_wedged_launches_raise(capsys):
    try:
        bk.find_browser()
    except bk.BrowserUnavailable:
        pytest.skip("no Chrome, Chromium or Edge installed")
    refused = []

    def refusal(live):
        refused.append(live)
        assert live.alive() is True
        raise bk.BrowserCheckError("the browser refused Page.enable: x")

    with pytest.raises(bk.BrowserCheckError, match="refused Page.enable"):
        bk.with_live_browser(refusal, lambda live, ctx: ctx, startup=60)
    assert len(refused) == 1
    assert "browserkit: replacing" not in capsys.readouterr().err

    wedged = []

    def always_timeout(live):
        wedged.append(live)
        raise bk.BrowserCheckError("the browser did not answer Page.enable in time")

    with pytest.raises(bk.BrowserCheckError, match="did not answer Page.enable"):
        bk.with_live_browser(always_timeout, lambda live, ctx: ctx, startup=60)
    assert len(wedged) == 2, "a second wedged launch must still raise; never a third launch"


class _ReplaceableBrowser:
    def __init__(self):
        self.replacements = 0

    def replace(self):
        self.replacements += 1


def test_page_bring_up_replaces_a_transport_failure_and_returns_its_context(capsys):
    browser, calls = _ReplaceableBrowser(), []

    def bring_up():
        calls.append(1)
        if len(calls) == 1:
            raise bk.BrowserCheckError("the browser did not answer Page.navigate in time")
        return "session-1"

    assert bk.with_page_bring_up(browser, bring_up) == "session-1"
    assert browser.replacements == 1 and len(calls) == 2
    warned = [line for line in capsys.readouterr().err.splitlines() if line.startswith("browserkit: replacing")]
    assert len(warned) == 1 and "(1/2)" in warned[0] and "did not answer Page.navigate in time" in warned[0]


@pytest.mark.parametrize("message", [
    "the browser refused Page.navigate: 'x'",
    "a page script check failed in the browser: TypeError: x",
    "no free Chrome slot: this run may hold 2 and the machine at most 4; waited 900s",
])
def test_page_bring_up_does_not_replace_a_refusal_or_a_page_failure(message):
    browser = _ReplaceableBrowser()
    error = bk.PageScriptError(message) if message.startswith("a page script") else bk.BrowserCheckError(message)

    def bring_up():
        raise error

    with pytest.raises(bk.BrowserCheckError) as caught:
        bk.with_page_bring_up(browser, bring_up)
    assert caught.value is error
    assert browser.replacements == 0


def test_page_bring_up_replaces_at_most_once_and_a_second_failure_raises(capsys):
    browser = _ReplaceableBrowser()

    def bring_up():
        raise bk.BrowserCheckError("the browser did not answer Page.navigate in time")

    with pytest.raises(bk.BrowserCheckError, match="did not answer Page.navigate"):
        bk.with_page_bring_up(browser, bring_up)
    assert browser.replacements == 1
    warned = [line for line in capsys.readouterr().err.splitlines() if line.startswith("browserkit: replacing")]
    assert len(warned) == 1 and "(1/2)" in warned[0]


def test_page_bring_up_without_replace_raises_the_transport_error_as_is():
    class Plain:
        pass

    def bring_up():
        raise bk.BrowserCheckError("the browser did not answer Page.navigate in time")

    with pytest.raises(bk.BrowserCheckError, match="did not answer Page.navigate"):
        bk.with_page_bring_up(Plain(), bring_up)


def test_browser_unavailable_costs_one_attempt_and_never_one_more(monkeypatch):
    tries, ran = [], []

    class Missing(bk.Chrome):
        def __enter__(self):
            tries.append(self)
            raise bk.BrowserUnavailable("no Chrome, Chromium or Edge was found")

    monkeypatch.setattr(bk, "Chrome", Missing)
    with pytest.raises(bk.BrowserUnavailable, match="no Chrome"):
        bk.with_live_browser(lambda live: ran.append(live), lambda live, ctx: ran.append(live))
    assert len(tries) == 1 and ran == []


def _sent(obs, rid, url, frame, sid=None, typ="Image"):
    obs.handle({"method": "Network.requestWillBeSent", "sessionId": sid,
                "params": {"requestId": rid, "request": {"url": url}, "type": typ, "frameId": frame}})


def test_only_the_browsers_own_favicon_probe_is_exempt_the_prototypes_is_counted():
    obs = bk.Observer()
    obs.top_frame = "TOP"
    obs.reset()
    for rid, frame in (("1", "TOP"), ("2", "CHILD")):
        _sent(obs, rid, "https://x.test/rounds/1/a/favicon.ico" if frame == "CHILD" else "https://x.test/favicon.ico", frame)
        obs.handle({"method": "Network.responseReceived", "params": {
            "requestId": rid, "type": "Image", "response": {"url": "https://x.test/favicon.ico", "status": 401}}})
    _sent(obs, "3", "https://x.test/favicon.ico", "CHILD")
    obs.handle({"method": "Network.loadingFailed", "params": {"requestId": "3", "errorText": "net::ERR_FAILED"}})
    assert [(f["status"], f["path"]) for f in obs.failures] == [(401, "/favicon.ico"), (0, "/favicon.ico")]
    # and a checker that never learned its top frame exempts nothing
    blind = bk.Observer()
    blind.reset()
    _sent(blind, "1", "https://x.test/favicon.ico", "TOP")
    blind.handle({"method": "Network.responseReceived", "params": {
        "requestId": "1", "response": {"url": "https://x.test/favicon.ico", "status": 404}}})
    assert len(blind.failures) == 1


def test_a_request_that_ends_in_another_session_is_not_left_pending():
    """A sandboxed frame's document request starts in the page session and finishes in the frame's own session."""
    obs = bk.Observer()
    _sent(obs, "D1", "https://x.test/rounds/1/a/index.html", "F1", sid="S1", typ="Document")
    _sent(obs, "I1", "https://x.test/rounds/1/a/a.png", "F1", sid="S2")
    assert obs.pending() == ["/rounds/1/a/index.html", "/rounds/1/a/a.png"]
    obs.handle({"method": "Network.loadingFinished", "sessionId": "S2", "params": {"requestId": "D1"}})
    assert obs.pending() == ["/rounds/1/a/a.png"]
    obs.handle({"method": "Network.loadingFailed", "sessionId": "S1", "params": {"requestId": "I1", "errorText": "x"}})
    assert obs.pending() == []
    _sent(obs, "U1", "data:image/png;base64,AAAA", "F1")
    assert obs.pending() == []                              # inline data never leaves the browser
