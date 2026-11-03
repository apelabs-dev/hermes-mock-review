"""Browser verification of password-gated prototype boards.

The offline tests run the production checker (browsercheck.verify_board: WebSocket client, DevTools driver, observer,
judge) against fakebrowser.FakeBrowser, a DevTools server that behaves like Chrome for a SameSite=Lax gate: the board's
real sandbox attribute, an opaque-origin frame whose own requests carry no cookie, over real HTTP to a gated site that
answers 401 without the cookie. A cookie-bearing urllib fetch of the same URLs answers 200, which is exactly why curl is
not a verification. A real headless Chrome run exists too, behind MOCKREVIEW_TEST_REAL_BROWSER=1.
"""
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakebrowser import FakeBrowser, GatedSite, PROTO_SANDBOX
from mockreview import api, browsercheck as bc, bundle, cli, config, herenow, store
from mockreview.store import ReviewError

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
PASSWORD = "plover quiver zeppelin marmot"
SVG = "<svg xmlns='http://www.w3.org/2000/svg' width='4' height='4'/>"


def _write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def make_proto(tmp_path: Path, name="proto") -> str:
    root = tmp_path / name
    _write(root, "assets/main.css", "body{background:url(logo.svg);color:#123}")
    _write(root, "assets/logo.svg", SVG)
    _write(root, "assets/app.js", "window.ok = 'assets/logo.svg';")
    _write(root, "index.html", '<!doctype html><html><head><meta charset="utf-8">'
           '<link rel="stylesheet" href="assets/main.css"><script src="assets/app.js" defer></script></head>'
           '<body><h1>Proto</h1><img src="assets/logo.svg"></body></html>')
    return str(root / "index.html")


def make_site(tmp_path: Path, bundled: bool, password=PASSWORD, name="site") -> Path:
    """The files a published gated review serves, built by the production build_site."""
    spec = api._spec(None, make_proto(tmp_path, name + "-src"), None, True)
    if bundled:
        spec = bundle.bundle_spec(spec)
    data = store.create_review("Verify", "", ORIGIN, "herenow", spec, wake=False, password=True)
    return herenow.build_site(data, tmp_path / name)


@pytest.fixture
def gated(env, tmp_path):
    made = []

    def build(bundled=True, **kw):
        root = make_site(tmp_path, bundled, name=f"site{len(made)}")
        site = GatedSite(lambda: root, PASSWORD)
        made.append(site)
        return site, FakeBrowser(site, **kw)
    yield build
    for s in made:
        s.close()


@pytest.fixture(autouse=True)
def _live_demo(monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_OFFER_LIVE_DEMO", "true")


def verify(site, browser, **kw):
    return bc.verify_board(site.url, PASSWORD, 1, launcher=browser, timeout=kw.pop("timeout", 30), **kw)


# ---------------------------------------------------------------- the production checker against the gated fake

def test_unbundled_prototype_is_refused_though_a_cookie_fetch_sees_200(gated):
    site, browser = gated(bundled=False)
    with pytest.raises(bc.BrowserCheckError) as ei:
        verify(site, browser)
    msg = ei.value.message
    assert "401" in msg and "/rounds/1/a/assets/" in msg and "subresource request(s) failed" in msg
    failed = ei.value.report["failed"]
    assert len(failed) == 3 and {f["status"] for f in failed} == {401}
    assert {f["type"] for f in failed} == {"Stylesheet", "Script", "Image"}
    # what the browser did NOT have: the cookie, for exactly the requests that 401'd
    assert [c for p, c in site.subresource_log()] == [False, False, False]
    # curl/urllib with the cookie typed in by hand sees a perfectly healthy site for the very same URL
    for _, path in ((0, "/rounds/1/a/assets/main.css"), (0, "/rounds/1/a/assets/app.js"), (0, "/rounds/1/a/assets/logo.svg")):
        req = urllib.request.Request(site.url.rstrip("/") + path, headers={"Cookie": site.cookie})
        assert urllib.request.urlopen(req, timeout=10).status == 200
        with pytest.raises(urllib.error.HTTPError) as hi:
            urllib.request.urlopen(site.url.rstrip("/") + path, timeout=10)
        assert hi.value.code == 401


def test_bundled_prototype_passes_with_no_subresource_requests(gated):
    site, browser = gated(bundled=True)
    report = verify(site, browser)
    assert report["failed"] == [] and report["problems"] == []
    assert report["frame"]["sandbox"] == "allow-scripts" == PROTO_SANDBOX
    assert report["styles"]["rules"] > 0 and report["styles"]["linksFailed"] == 0
    assert site.subresource_log() == []                    # nothing left to ask the gate for
    assert report["requests"] >= 3                           # the unlock, the board, state.json, the frame document


def test_the_checker_unlocks_through_the_form_like_a_reviewer(gated):
    site, browser = gated(bundled=True)
    verify(site, browser)
    assert browser.typed == PASSWORD
    methods = [m for m, *_ in site.log]
    assert methods[:2] == ["GET", "POST"]                    # gate, then the password form post
    assert ("GET", "/", False) == site.log[0]                # first visit has no cookie and sees the gate
    assert "Input.insertText" in browser.commands and "Runtime.evaluate" in browser.commands


@pytest.mark.parametrize("sandbox,found", [("allow-scripts allow-same-origin", "allow-same-origin"),
                                           ("allow-scripts allow-popups", "allow-popups"),
                                           ("allow-forms", "allow-forms"), ("", "no sandbox")])
def test_a_frame_that_is_not_exactly_allow_scripts_is_refused(gated, sandbox, found):
    site, browser = gated(bundled=True, board_sandbox=sandbox)
    with pytest.raises(bc.BrowserCheckError, match=r"not sandbox=\"allow-scripts\" exactly") as ei:
        verify(site, browser)
    assert found in ei.value.message


def test_production_board_keeps_the_prototype_sandbox_to_allow_scripts():
    board_js = (config.BOARD_DIR / "board.js").read_text(encoding="utf-8")
    assert PROTO_SANDBOX == "allow-scripts"
    assert "allow-same-origin" not in board_js
    code = Path(bc.__file__).read_text(encoding="utf-8").split('"""', 2)[2]      # past the module docstring
    assert "allow-same-origin" not in code and "allow-same-origin" not in Path(bundle.__file__).read_text(encoding="utf-8")


def test_no_password_form_means_the_gate_is_not_there(gated):
    site, browser = gated(bundled=True)
    site.cookie = ""                                       # every request counts as unlocked
    with pytest.raises(bc.BrowserCheckError, match="no password form"):
        verify(site, browser)


def test_wrong_password_never_reaches_the_prototype_and_never_leaks(gated):
    site, browser = gated(bundled=True)
    with pytest.raises(bc.BrowserCheckError) as ei:
        bc.verify_board(site.url, "wrong battery staple words", 1, launcher=browser, timeout=2)
    assert "did not render the prototype frame" in ei.value.message
    assert "wrong battery" not in ei.value.message and "staple" not in ei.value.message


def test_a_page_that_never_finishes_loading_times_out(gated):
    site, browser = gated(bundled=True)
    browser._navigate = lambda url: None                   # no load event, ever
    with pytest.raises(bc.BrowserCheckError, match="did not finish loading in time"):
        verify(site, browser, timeout=1.5)


def test_a_missing_frame_fails_instead_of_passing(gated):
    site, browser = gated(bundled=True)
    real = browser._evaluate

    def evaluate(expr, ctx, session=None):
        if expr == bc.JS_FRAMES:
            return "[]", None
        return real(expr, ctx, session)
    browser._evaluate = evaluate
    with pytest.raises(bc.BrowserCheckError, match="did not render the prototype frame"):
        verify(site, browser, timeout=2)


def test_unstyled_frame_is_refused_even_with_all_requests_ok():
    report = {"frame": {"sandbox": "allow-scripts"}, "failed": [],
              "styles": {"links": 1, "linksFailed": 0, "styleEls": 0, "rules": 0}}
    assert "rendered unstyled" in " ".join(bc.judge(report))
    report["styles"] = {"links": 1, "linksFailed": 1, "styleEls": 0, "rules": 0}
    assert "never loaded" in " ".join(bc.judge(report))
    report["styles"] = {"links": 0, "linksFailed": 0, "styleEls": 1, "rules": 3}
    assert bc.judge(report) == []
    assert bc.judge({"frame": None, "failed": []}) == ["the board never showed the prototype frame"]


def test_network_failures_and_5xx_are_counted_like_4xx():
    obs = bc.Observer()
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
    assert "network error" in " ".join(bc.judge({"frame": {"sandbox": "allow-scripts"}, "failed": obs.failures,
                                                 "styles": {"links": 0, "styleEls": 0, "rules": 0}}))


def test_nothing_counted_before_the_unlock():
    obs = bc.Observer()
    obs.handle({"method": "Network.requestWillBeSent", "params": {"requestId": "1", "request": {"url": "https://x.test/"}}})
    obs.handle({"method": "Network.responseReceived", "params": {"requestId": "1", "response": {"url": "https://x.test/", "status": 401}}})
    assert obs.failures == [] and obs.total == 0           # the gate page itself is a 401 by design


def _sent(obs, rid, url, frame, sid=None, typ="Image"):
    obs.handle({"method": "Network.requestWillBeSent", "sessionId": sid,
                "params": {"requestId": rid, "request": {"url": url}, "type": typ, "frameId": frame}})


def test_only_the_browsers_own_favicon_probe_is_exempt_the_prototypes_is_counted():
    obs = bc.Observer()
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
    blind = bc.Observer()
    blind.reset()
    _sent(blind, "1", "https://x.test/favicon.ico", "TOP")
    blind.handle({"method": "Network.responseReceived", "params": {
        "requestId": "1", "response": {"url": "https://x.test/favicon.ico", "status": 404}}})
    assert len(blind.failures) == 1


def test_a_request_that_ends_in_another_session_is_not_left_pending():
    """A sandboxed frame's document request starts in the page session and finishes in the frame's own session."""
    obs = bc.Observer()
    _sent(obs, "D1", "https://x.test/rounds/1/a/index.html", "F1", sid="S1", typ="Document")
    _sent(obs, "I1", "https://x.test/rounds/1/a/a.png", "F1", sid="S2")
    assert obs.pending() == ["/rounds/1/a/index.html", "/rounds/1/a/a.png"]
    obs.handle({"method": "Network.loadingFinished", "sessionId": "S2", "params": {"requestId": "D1"}})
    assert obs.pending() == ["/rounds/1/a/a.png"]
    obs.handle({"method": "Network.loadingFailed", "sessionId": "S1", "params": {"requestId": "I1", "errorText": "x"}})
    assert obs.pending() == []
    _sent(obs, "U1", "data:image/png;base64,AAAA", "F1")
    assert obs.pending() == []                              # inline data never leaves the browser


def test_pending_requests_fail_the_judgment():
    report = {"frame": {"sandbox": "allow-scripts"}, "failed": [], "pending": ["/rounds/1/a/lazy.png"],
              "styles": {"links": 0, "styleEls": 1, "rules": 3}}
    assert "had not finished" in " ".join(bc.judge(report)) and "/rounds/1/a/lazy.png" in " ".join(bc.judge(report))
    assert bc.judge(dict(report, pending=[])) == []


def test_the_frame_is_found_and_inspected_through_its_own_session(gated):
    site, browser = gated(bundled=True)
    report = verify(site, browser)
    assert report["pending"] == [] and report["failed"] == []
    # the page session's frame tree does not list the out-of-process frame; the child session was resumed and inspected
    assert ("S2", "Runtime.runIfWaitingForDebugger") in browser.sessions
    assert {m for s, m in browser.sessions if s == "S2"} >= {"Network.enable", "Runtime.enable", "Page.enable",
                                                              "Runtime.evaluate"}


def test_a_request_unresolved_when_the_wait_ends_fails_the_check_instead_of_passing(gated):
    site, browser = gated(bundled=True)
    real = browser.emit

    def emit(method, session="S1", **params):
        if method == "Network.loadingFinished" and session == "S2":
            return                                         # the frame's document never reports that it finished
        real(method, session, **params)
    browser.emit = emit
    with pytest.raises(bc.BrowserCheckError, match=r"had not finished when the wait ended: /rounds/1/a/index.html") as ei:
        verify(site, browser, timeout=4)
    assert ei.value.report["pending"] == ["/rounds/1/a/index.html"]


def test_handshake_that_never_completes_raises_at_the_deadline_and_closes(monkeypatch):
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()[0]), daemon=True).start()
    made = []
    real_close = bc.WebSocket.close
    monkeypatch.setattr(bc.WebSocket, "close", lambda self: (made.append(self), real_close(self))[1])
    started = time.monotonic()
    try:
        with pytest.raises(bc.BrowserCheckError, match="did not complete the connection in time"):
            bc.WebSocket("127.0.0.1", srv.getsockname()[1], "/devtools/browser/x", timeout=0.5)
    finally:
        srv.close()
        for c in held:
            c.close()
    assert time.monotonic() - started < 5 and made and made[0].sock.fileno() == -1



# ---------------------------------------------------------------- secrets stay out

def test_password_stays_out_of_errors_reports_and_browser_argv(gated, tmp_path):
    site, browser = gated(bundled=False)
    with pytest.raises(bc.BrowserCheckError) as ei:
        verify(site, browser)
    blob = ei.value.message + json.dumps(ei.value.report, default=str)
    assert all(w not in blob for w in PASSWORD.split()) and "***" not in blob
    # typed into the form via Input.insertText, never a navigation URL or script text
    assert all(PASSWORD not in c for c in browser.commands)
    assert all(PASSWORD not in line[1] for line in site.log)


def test_scrub_hides_the_password_and_its_words_everywhere():
    assert bc._scrub(f"it said {PASSWORD} and plover", PASSWORD) == "it said *** and ***"

    class Boom:
        def __enter__(self):
            raise RuntimeError("could not use " + PASSWORD)

        def __exit__(self, *a):
            pass
    with pytest.raises(bc.BrowserCheckError) as ei:
        bc.verify_board("https://x.test/", PASSWORD, 1, launcher=Boom())
    assert PASSWORD not in ei.value.message and "plover" not in ei.value.message and "RuntimeError" in ei.value.message

    class Typed:
        def __enter__(self):
            raise bc.BrowserCheckError("saw " + PASSWORD)

        def __exit__(self, *a):
            pass
    with pytest.raises(bc.BrowserCheckError) as ei:
        bc.verify_board("https://x.test/", PASSWORD, 1, launcher=Typed())
    assert "plover" not in ei.value.message and ei.value.message == "saw ***"


def test_child_env_drops_secret_looking_variables(monkeypatch):
    monkeypatch.setenv("HERENOW_API_KEY", "k")
    monkeypatch.setenv("SOME_PASSWORD", "p")
    monkeypatch.setenv("MY_TOKEN", "t")
    monkeypatch.setenv("KEEPME", "ok")
    env = bc._child_env()
    assert "KEEPME" in env and not {"HERENOW_API_KEY", "SOME_PASSWORD", "MY_TOKEN"} & set(env)


# ---------------------------------------------------------------- finding and launching a browser

def test_find_browser_env_override_and_missing(monkeypatch, tmp_path):
    exe = tmp_path / "fakechrome"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv(bc.BROWSER_ENV, str(exe))
    assert bc.find_browser() == str(exe)
    monkeypatch.setenv(bc.BROWSER_ENV, str(tmp_path / "nope"))
    with pytest.raises(bc.BrowserUnavailable, match="not an executable"):
        bc.find_browser()
    monkeypatch.delenv(bc.BROWSER_ENV)
    monkeypatch.setattr(bc.shutil, "which", lambda name: None)
    monkeypatch.setattr(bc, "BROWSER_PATHS", ())
    with pytest.raises(bc.BrowserUnavailable, match="no Chrome, Chromium or Edge was found"):
        bc.find_browser()


def test_no_browser_is_an_error_not_a_pass(monkeypatch):
    monkeypatch.delenv(bc.BROWSER_ENV, raising=False)
    monkeypatch.setattr(bc.shutil, "which", lambda name: None)
    monkeypatch.setattr(bc, "BROWSER_PATHS", ())
    with pytest.raises(bc.BrowserUnavailable) as ei:
        bc.verify_board("https://x.test/", PASSWORD, 1)
    assert isinstance(ei.value, bc.BrowserCheckError)


posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups and shell scripts")

FAKE_BROWSER = """#!/bin/sh
for a in "$@"; do case "$a" in --user-data-dir=*) d="${a#--user-data-dir=}";; esac; done
echo "$@" > "$d/argv.txt"
sleep 300 &
echo $! > "$d/helper.pid"
printf '9222\\n/devtools/browser/x\\n' > "$d/DevToolsActivePort"
wait
"""


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


@posix_only
def test_chrome_launch_isolated_group_and_cleanup_even_on_error(tmp_path):
    exe = tmp_path / "chrome"
    exe.write_text(FAKE_BROWSER, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    c = bc.Chrome(executable=str(exe))
    with pytest.raises(RuntimeError):
        with c as (host, port, path):
            assert (host, port, path) == ("127.0.0.1", 9222, "/devtools/browser/x")
            pid, profile = c.proc.pid, c.profile
            assert os.getpgid(pid) == pid != os.getpgid(os.getpid())        # its own process group
            argv = Path(profile, "argv.txt").read_text(encoding="utf-8")
            assert "--headless=new" in argv and "--remote-debugging-port=0" in argv and f"--user-data-dir={profile}" in argv
            assert PASSWORD not in argv
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
    c = bc.Chrome(executable=str(early))
    with pytest.raises(bc.BrowserCheckError, match="exited before"):
        c.__enter__()
    assert c.profile is None and c.proc is None
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
    silent.chmod(0o755)
    c = bc.Chrome(executable=str(silent), startup=0.5)
    with pytest.raises(bc.BrowserCheckError, match="debugging port in time"):
        c.__enter__()
    assert c.profile is None and c.proc is None
    c = bc.Chrome(executable=str(tmp_path / "does-not-exist"))
    with pytest.raises(bc.BrowserUnavailable, match="could not be started"):
        c.__enter__()
    assert c.profile is None


def test_chrome_never_adds_allow_same_origin_or_the_password_to_argv():
    src = Path(bc.__file__).read_text(encoding="utf-8")
    assert "--disable-web-security" not in src and "--disable-site-isolation" not in src and "disable-features" not in src


# ---------------------------------------------------------------- publish semantics

@pytest.fixture
def hn(env, tmp_path, monkeypatch):
    """The publish helper and account API stubbed; the browser check is the production one over the fake browser."""
    calls = {"publish": 0, "sites": []}
    monkeypatch.setattr(config, "herenow_publish_script", lambda: tmp_path / "publish.sh")

    def run(argv, **kw):
        calls["publish"] += 1
        return SimpleNamespace(returncode=0, stderr="publish_result.slug=test-site-ab12\n"
                               "publish_result.site_url=https://test-site-ab12.here.now/\n"
                               "publish_result.auth_mode=authenticated\n")
    monkeypatch.setattr(herenow.subprocess, "run", run)
    monkeypatch.setattr(herenow, "_api", lambda *a, **kw: {})
    sites = []

    def chrome():
        rid = store.list_ids()[-1]
        data = store.load(rid)
        site = GatedSite(lambda: store.review_dir(rid) / "site", data["herenow"]["password"], slug="test-site-ab12")
        sites.append(site)
        calls["sites"].append(site)
        return FakeBrowser(site)
    monkeypatch.setattr(bc, "Chrome", chrome)
    yield calls
    for s in sites:
        s.close()


def _rid():
    return store.list_ids()[-1]


def _hn(rid):
    return store.load(rid)["herenow"]


def test_create_gated_prototype_publishes_verified_and_shares_the_link(hn, tmp_path):
    res = api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    assert res["url"] == "https://test-site-ab12.here.now/" and len(res["password"].split()) == 4
    h = _hn(res["review_id"])
    assert h["verify_status"] == "verified" and h["verified_round"] == 1 and h["verify_error"] is None
    assert hn["sites"][0].subresource_log() == []


def test_create_withholds_link_and_password_when_the_browser_check_fails(hn, tmp_path, monkeypatch):
    # an unbundled gated prototype: the state before this fix. Bundling off for this one test, the check must catch it.
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: spec)
    with pytest.raises(ReviewError) as ei:
        api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    msg = ei.value.message
    rid = _rid()
    assert f"review {rid} was created but publishing failed" in msg and "401" in msg
    assert f"mockreview publish {rid}" in msg and "withheld" in msg
    assert ei.value.status == 502
    stored = store.load(rid)
    assert stored["herenow"]["verify_status"] == "failed" and "401" in stored["herenow"]["verify_error"]
    assert store.review_url(stored) == ""
    assert herenow.password_result(stored)["password"] is None
    pw = stored["herenow"]["password"]
    assert pw not in msg and pw not in json.dumps(api.list_reviews()) and pw not in json.dumps(api.review_link(rid))
    assert api.review_link(rid)["url"] == ""
    assert all(w not in stored["herenow"]["verify_error"] for w in pw.split())


def test_retry_publish_after_failure_verifies_again_and_releases_the_link(hn, tmp_path, monkeypatch):
    real = bundle.bundle_spec
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: spec)
    with pytest.raises(ReviewError):
        api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    rid = _rid()
    # the author fixes the prototype by adding a bundled round, which publishes and verifies the new newest round
    monkeypatch.setattr(bundle, "bundle_spec", real)
    out = api.add_round(rid, prototype=make_proto(tmp_path, "fixed"))
    assert out["round"] == 2 and out["url"] == "https://test-site-ab12.here.now/" and out["password"]
    assert _hn(rid)["verify_status"] == "verified" and _hn(rid)["verified_round"] == 2


def test_add_round_failure_keeps_the_round_but_withholds_link_and_password(hn, tmp_path, monkeypatch):
    api.create("Gated", ORIGIN, mode="herenow", password=True, prototype=make_proto(tmp_path))
    rid = _rid()
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: spec)
    with pytest.raises(herenow.HereNowError) as ei:
        api.add_round(rid, prototype=make_proto(tmp_path, "round2"))
    assert "browser check of the password-gated prototype failed" in ei.value.message and ei.value.status == 502
    assert "401" in ei.value.message and f"mockreview publish {rid}" in ei.value.message
    data = store.load(rid)
    assert [r["n"] for r in data["rounds"]] == [1, 2]       # the round was added; only sharing is withheld
    assert data["herenow"]["verify_status"] == "failed"
    assert store.review_url(data) == "" and herenow.password_result(data)["password"] is None
    assert all(w not in ei.value.message for w in data["herenow"]["password"].split())


def test_browser_unavailable_fails_the_publish_with_409(hn, tmp_path, monkeypatch):
    def none():
        raise bc.BrowserUnavailable("no Chrome, Chromium or Edge was found")
    monkeypatch.setattr(bc, "Chrome", none)
    with pytest.raises(ReviewError) as ei:
        api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    assert ei.value.status == 409 and "no Chrome, Chromium or Edge was found" in ei.value.message
    assert "could not be verified, so it is treated as failed" in ei.value.message
    rid = _rid()
    assert _hn(rid)["verify_status"] == "failed" and store.review_url(store.load(rid)) == ""


def test_unexpected_checker_failure_is_still_a_failed_publish_not_a_pass(hn, tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "Chrome", lambda: (_ for _ in ()).throw(OSError("disk went away")))
    with pytest.raises(ReviewError, match="browser check of the password-gated prototype failed"):
        api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    assert _hn(_rid())["verify_status"] == "failed"


def test_pending_state_withholds_while_the_check_runs(hn, tmp_path, monkeypatch):
    seen = {}
    real = bc.verify_board

    def spy(site_url, password, n, variant="a", **kw):
        d = store.load(_rid())
        seen.update(status=d["herenow"]["verify_status"], url=store.review_url(d),
                    pw=herenow.password_result(d)["password"])
        return real(site_url, password, n, variant=variant, **kw)
    monkeypatch.setattr(bc, "verify_board", spy)
    api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    assert seen == {"status": "pending", "url": "", "pw": None}


@pytest.mark.parametrize("kind", ["password_off", "local", "decisions"])
def test_nothing_is_verified_unless_a_gated_prototype(hn, tmp_path, monkeypatch, kind):
    from test_store import _decisions

    def boom(*a, **kw):
        raise AssertionError("browser launched for a board that needs no check")
    monkeypatch.setattr(bc, "Chrome", boom)
    if kind == "password_off":
        res = api.create("Open", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=False, wake=False)
        assert res["url"] and "verify_status" not in _hn(res["review_id"])
    elif kind == "local":
        api.create("Local", ORIGIN, mode="local", prototype=make_proto(tmp_path), password=True, wake=False)
    else:
        res = api.create("Dec", ORIGIN, mode="herenow", decisions=str(_decisions(tmp_path)), password=True)
        assert res["url"] and "verify_status" not in _hn(res["review_id"])


def test_poller_republish_does_not_launch_a_browser(hn, tmp_path, monkeypatch):
    api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    rid = _rid()
    monkeypatch.setattr(bc, "Chrome", lambda: (_ for _ in ()).throw(AssertionError("poller launched a browser")))
    herenow.republish_quietly(rid)
    assert hn["publish"] == 2 and _hn(rid)["verify_status"] == "verified"


def test_retry_without_fixing_fails_the_same_way(hn, tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: spec)
    with pytest.raises(ReviewError):
        api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    rid = _rid()
    # the same unbundled files are still what is on disk: a retry must fail the same way, not pass
    with pytest.raises(herenow.HereNowError, match="browser check"):
        herenow.publish(rid)
    assert _hn(rid)["verify_status"] == "failed"


def test_enabling_the_password_on_an_existing_prototype_board_verifies_it(hn, tmp_path, monkeypatch):
    res = api.create("Open", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=False, wake=False)
    rid = res["review_id"]
    # a prototype round made while the gate was off has no bundle: turning the gate on must not hand out a broken link
    with pytest.raises(herenow.HereNowError, match="browser check of the password-gated prototype failed"):
        herenow.set_password(rid, True)
    data = store.load(rid)
    assert data["herenow"]["verify_status"] == "failed" and store.review_url(data) == ""
    assert herenow.password_result(data)["password"] is None


# ---------------------------------------------------------------- a failed check must not outlive its reason

def _failed_review(tmp_path, monkeypatch):
    real = bundle.bundle_spec
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: spec)
    with pytest.raises(ReviewError):
        api.create("Gated", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=True)
    monkeypatch.setattr(bundle, "bundle_spec", real)
    rid = _rid()
    assert _hn(rid)["verify_status"] == "failed" and store.review_url(store.load(rid)) == ""
    return rid


def test_turning_the_password_off_after_a_failed_check_releases_the_link(hn, tmp_path, monkeypatch):
    rid = _failed_review(tmp_path, monkeypatch)
    monkeypatch.setattr(bc, "Chrome", lambda: (_ for _ in ()).throw(AssertionError("no gate, nothing to verify")))
    data = herenow.set_password(rid, False)
    # the gate is gone, so the frame's assets are public: the link is the board's own, and no password exists to withhold
    assert store.review_url(data) == "https://test-site-ab12.here.now/"
    res = herenow.password_result(data)
    assert res == {"password": None, "password_enabled": False, "password_status": "applied"}
    assert api.review_link(rid)["url"] == "https://test-site-ab12.here.now/"


def test_turning_the_password_back_on_checks_again_and_withholds_again(hn, tmp_path, monkeypatch):
    rid = _failed_review(tmp_path, monkeypatch)
    herenow.set_password(rid, False)
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: spec)       # the stored round is still the unbundled one
    with pytest.raises(herenow.HereNowError, match="browser check of the password-gated prototype failed"):
        herenow.set_password(rid, True)
    assert _hn(rid)["verify_status"] == "failed" and store.review_url(store.load(rid)) == ""


def test_a_later_decisions_round_is_not_held_back_by_an_earlier_failed_prototype_check(hn, tmp_path, monkeypatch):
    from test_store import _decisions
    rid = _failed_review(tmp_path, monkeypatch)
    monkeypatch.setattr(bc, "Chrome", lambda: (_ for _ in ()).throw(AssertionError("a decisions round has no frame")))
    out = api.add_round(rid, decisions=str(_decisions(tmp_path)))
    assert out["round"] == 2 and out["url"] == "https://test-site-ab12.here.now/" and out["password"]
    data = store.load(rid)
    assert store.review_url(data) and herenow.password_result(data)["password"]


def test_a_failed_check_still_withholds_while_the_newest_round_is_a_gated_prototype(hn, tmp_path, monkeypatch):
    rid = _failed_review(tmp_path, monkeypatch)
    data = store.load(rid)
    assert store.verify_blocks(data) is True
    data["herenow"]["password_enabled"] = False
    assert store.verify_blocks(data) is False
    data["herenow"]["password_enabled"] = True
    data["rounds"][-1]["kind"] = "decisions"
    assert store.verify_blocks(data) is False


def test_password_off_boards_never_gain_a_verify_state(hn, tmp_path):
    res = api.create("Open", ORIGIN, mode="herenow", prototype=make_proto(tmp_path), password=False, wake=False)
    data = store.load(res["review_id"])
    assert "verify_status" not in data["herenow"] and store.review_url(data) == "https://test-site-ab12.here.now/"
    assert store.verify_blocks(data) is False


# ---------------------------------------------------------------- a real browser, when one is allowed

@pytest.mark.skipif(os.environ.get("MOCKREVIEW_TEST_REAL_BROWSER") != "1", reason="set MOCKREVIEW_TEST_REAL_BROWSER=1")
def test_real_chrome_refuses_unbundled_and_accepts_bundled(env, tmp_path):
    try:
        bc.find_browser()
    except bc.BrowserUnavailable:
        pytest.skip("no Chrome/Chromium/Edge installed")
    for bundled in (False, True):
        root = make_site(tmp_path, bundled, name=f"real{int(bundled)}")
        site = GatedSite(lambda r=root: r, PASSWORD)
        try:
            if bundled:
                report = bc.verify_board(site.url, PASSWORD, 1, timeout=90)
                assert report["failed"] == [] and report["frame"]["sandbox"] == "allow-scripts"
            else:
                with pytest.raises(bc.BrowserCheckError, match="401"):
                    bc.verify_board(site.url, PASSWORD, 1, timeout=90)
        finally:
            site.close()
