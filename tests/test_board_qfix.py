"""A question block refreshes through its own section, and a markable artifact keeps its decide row.

Both were release blockers on 171de57. markSurface returned a question's inner qdecide row (no data-kind, no .status),
so refreshBlock threw a TypeError on load and on every answer. And renderBlock passed two children to the one-child
append helper, so a `markable: true` artifact silently lost its Approve / Change / Skip row. These are real-browser
tests: the page-error recorder is installed with Page.addScriptToEvaluateOnNewDocument before the document starts, so
the throw from the render's own setTimeout is caught rather than missed.
"""

import json
import time

import pytest

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

PAGE_API = ("<!doctype html><html><body><h1>API</h1><script>"
            "window.__setMark = function () { return window.mockreview.use('marks').set("
            "{status: 'change', note: 'x'}); };"
            "</script></body></html>")

BLOCKS = {"blocks": [
    {"type": "question", "id": "retention", "title": "Keep retention at 30 days?",
     "text": "Plain-language question text.",
     "recommendation": {"answer": "Yes, keep 30 days.", "why": "It matches the plan."}},
    {"type": "artifact", "id": "page-api", "title": "API page", "markable": True, "html": PAGE_API},
    {"type": "artifact", "id": "shot", "title": "Reference shot",
     "html": "<!doctype html><html><body><h1>ref</h1></body></html>"},   # NOT markable
    {"type": "decision", "id": "nav", "title": "Nav shape",
     "options": [{"id": "a", "label": "Tabs", "html": "<div>Tabs</div>"},
                 {"id": "b", "label": "Rail", "html": "<div>Rail</div>"}]},
]}

REC = ("window.__pageErrors=[];addEventListener('error',function(e){"
       "window.__pageErrors.push(String(e.message||e));});")

WAIT = "!!document.getElementById('dz-retention') && !!document.getElementById('dz-page-api')"


def _review(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(BLOCKS), encoding="utf-8")
    return store.create_review("Qfix", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)), wake=False)


def _open(chrome, server, data, width, height, mobile):
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile, wait=WAIT)
    b.review = data
    b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": REC}, session=b.sid)
    b.cdp.call("Page.reload", {}, session=b.sid)
    assert b.until(WAIT, timeout=60), "the board never loaded"
    time.sleep(0.4)   # the render's setTimeout(refreshBlock) and the tally's requestAnimationFrame
    return b


def _text(b, sel):
    return b.js("(function(e){return e ? e.textContent : null})(document.querySelector(%s))" % json.dumps(sel))


def _tally(b):
    """The tally counts that exist at this width: the full row on desktop, the compact pair on a phone."""
    got = {}
    for key in ("approve", "change", "skip", "open", "done", "total"):
        v = b.js("(function(e){return e ? parseInt(e.textContent, 10) : null})"
                 "(document.getElementById('c-%s'))" % key)
        if v is not None:
            got[key] = v
    return got


def _frame_session(b, suffix, timeout=10.0):
    rid = b.review["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        targets = b.cdp.call("Target.getTargets")["targetInfos"]
        frame = next((t for t in targets
                      if t["type"] == "iframe" and rid in t["url"] and t["url"].split("?")[0].endswith(suffix)), None)
        if frame:
            sid = b.cdp.call("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True})["sessionId"]
            b.cdp.call("Runtime.enable", session=sid)
            return sid
        b.cdp.pump(0.1)
    return None


def _wait_in_frame(b, sid, expr, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if b.cdp.evaluate(expr, session=sid):
            return True
        b.cdp.pump(0.1)
    return False


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_load_has_no_page_error_and_both_fixes_are_visible(env, server, tmp_path, chrome, width, height, mobile):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data, width, height, mobile)
    errors = json.loads(b.js("JSON.stringify(window.__pageErrors)"))
    assert errors == [], "a question block threw on load: %r" % (errors,)
    assert b.js("!!document.querySelector('#dz-retention .status')"), "the question section has no status label"
    assert _text(b, "#dz-retention .status") == "Open", "the question's status must read Open before an answer"
    row = json.loads(b.js("""JSON.stringify((function(){
      var row = document.querySelector('#dz-page-api .decide[data-id="page-api"]');
      if (!row) return null;
      return [].slice.call(row.querySelectorAll('.sbtn')).map(function (x) { return x.textContent.trim(); });
    })())"""))
    assert row == ["Approve", "Change", "Skip"], "the markable artifact has no Approve / Change / Skip row: %r" % (row,)
    assert b.js("document.querySelectorAll('#dz-page-api .decide[data-id=\"page-api\"]').length") == 1
    assert b.js("document.querySelectorAll('#dz-shot .decide, #dz-shot .sbtn').length") == 0, \
        "a non-markable artifact must keep no decide row"


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_answering_the_question_updates_its_status_and_the_tally(env, server, tmp_path, chrome, width, height, mobile):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data, width, height, mobile)
    b.press("#dz-retention .qbtn.q-recommended")
    assert b.until("(document.querySelector('#dz-retention .status') || {}).textContent === 'Answered'"), \
        "answering the question did not update its status"
    tally = _tally(b)
    if mobile:
        assert (tally.get("done"), tally.get("total")) == (1, 3), tally
    else:
        assert (tally.get("approve"), tally.get("open")) == (1, 2), tally
    errors = json.loads(b.js("JSON.stringify(window.__pageErrors)"))
    assert errors == [], "answering the question threw: %r" % (errors,)


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_the_markable_artifact_row_and_the_page_both_set_the_mark(env, server, tmp_path, chrome, width, height, mobile):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data, width, height, mobile)
    assert b.js("!!document.querySelector('#dz-page-api .decide[data-id=\"page-api\"]')"), \
        "the markable artifact has no decide row"
    # The page may set the mark while the reviewer has not: it lands first.
    sid = _frame_session(b, "blocks/page-api.html")
    assert sid, "the artifact page never appeared as a frame target"
    assert _wait_in_frame(b, sid, "typeof window.__setMark === 'function'"), \
        "the artifact page's own script never ran"
    b.cdp.call("Runtime.evaluate", {"expression":
               "window.__setMark().then(function(){window.__ok=1}, function(e){window.__err=String(e&&e.message||e)})",
               "returnByValue": True}, session=sid)
    assert _wait_in_frame(b, sid, "window.__ok === 1"), \
        "the page's use('marks').set never resolved: %r" % (b.cdp.evaluate("window.__err || ''", session=sid),)
    assert b.until("document.querySelector('#dz-page-api .s-change').getAttribute('aria-pressed') === 'true'"), \
        "the page's mark did not press Change on the row"
    assert b.until("(document.querySelector('#dz-page-api .status') || {}).textContent === 'Change'"), \
        "the page's mark did not update the artifact's status"
    tally = _tally(b)
    if mobile:
        assert tally.get("done") == 1, tally
    else:
        assert (tally.get("change"), tally.get("approve")) == (1, 0), tally
    # The reviewer's own button always wins, even over a mark the page already set.
    b.press('#dz-page-api .decide[data-id="page-api"] .s-approve')
    assert b.until("document.querySelector('#dz-page-api .s-approve').getAttribute('aria-pressed') === 'true'"), \
        "Approve did not press the artifact's own button"
    assert b.js("document.querySelector('#dz-page-api').classList.contains('marked')"), \
        "Approve did not mark the artifact section"
    assert b.until("(document.querySelector('#dz-page-api .status') || {}).textContent === 'Approved'"), \
        "Approve did not update the artifact's status"
    tally = _tally(b)
    if mobile:
        assert tally.get("done") == 1, tally
    else:
        assert (tally.get("approve"), tally.get("change")) == (1, 0), tally
    errors = json.loads(b.js("JSON.stringify(window.__pageErrors)"))
    assert errors == [], "the page's mark raised an error on the board: %r" % (errors,)
