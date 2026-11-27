"""An artifact frame sized from its own page follows the page down when the page shrinks.

`display: "long"` and an auto-height artifact frame are as tall as the page's own content. Until this fix the probe
measured `document.documentElement.scrollHeight`, which is never smaller than the frame's own viewport, so a frame could
grow and then never shrink: thousands of pixels of empty page stayed above the footer. These are real-browser tests for
the measurement in both directions, the two caps, and a page that sizes itself to the viewport.

One block per test where a resize is triggered: an offscreen cross-origin frame has its rendering (and so its
ResizeObserver callback) throttled by the browser, which is a harness limit, not the board's behaviour.
"""

import json
import time

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
ART_MIN, ART_MAX, LONG_MAX = 80, 4000, 30000

# 1000 px sections. The page can lose sections (`cut`) or append one (`add`) on a message from the test, and it reports
# its own content measurements back, so an assertion can tell the frame height from the page's own height.
PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#101820;color:#eef0f6;font:16px sans-serif}
section{height:1000px;box-sizing:border-box;padding:24px;border-bottom:1px solid #2a3340}
__EXTRA__
</style></head><body>__BODY__<script>
var MRB = __BLOCK__;
function mrReport() {
  parent.postMessage({mrMeasure: {block: MRB, ih: innerHeight,
    root: document.documentElement.getBoundingClientRect().height,
    dsh: document.documentElement.scrollHeight, bsh: document.body.scrollHeight,
    sections: document.querySelectorAll('section').length}}, '*');
}
function mrCut(keep) {
  var s = document.querySelectorAll('section');
  for (var i = keep; i < s.length; i++) s[i].remove();
  mrReport();
}
function mrAdd(px) {
  var s = document.createElement('section'); s.style.height = px + 'px'; document.body.appendChild(s); mrReport();
}
addEventListener('message', function (e) {
  var d = e.data || {};
  if (typeof d.cut === 'number') mrCut(d.cut);
  if (d.add) mrAdd(d.add);
  if (d.measure) mrReport();
});
</script></body></html>"""


def _page(block, sections=8, rem=0, extra="", tail=""):
    body = "".join("<section>section %d</section>" % i for i in range(sections))
    if rem:
        body += '<section style="height:%dpx">rem</section>' % rem
    body += tail
    return PAGE.replace("__EXTRA__", extra).replace("__BODY__", body).replace("__BLOCK__", json.dumps(block))


def _review(tmp_path, blocks):
    d = tmp_path / "dec"
    d.mkdir()
    (d / "decisions.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
    return store.create_review("Shrink", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(d / "decisions.json")), wake=False)


def _open(chrome, server, data, *blocks):
    wait = " && ".join("!!document.querySelector('#dz-%s iframe.art-frame')" % b for b in blocks)
    board = Board(chrome, "%s/r/%s/" % (server, data["id"]), wait=wait)
    board.review = data
    board.js("window.__mr = {}; addEventListener('message', function (e) {"
             " if (e.data && e.data.mrMeasure) window.__mr[e.data.mrMeasure.block] = e.data.mrMeasure; });")
    return board


def _h(board, block):
    return board.js("(function(){var f=document.querySelector('#dz-%s iframe');"
                    "return f ? f.getBoundingClientRect().height : -1})()" % block)


def _tell(board, block, message):
    assert board.js("(function(){var f=document.querySelector('#dz-%s iframe');"
                    "if (!f || !f.contentWindow) return false;"
                    "f.contentWindow.postMessage(%s, '*'); return true})()" % (block, json.dumps(message))), block


def _measure(board, block, timeout=5.0):
    board.js("if (window.__mr) delete window.__mr[%s]" % json.dumps(block))
    _tell(board, block, {"measure": 1})
    assert board.until("!!(window.__mr && window.__mr[%s])" % json.dumps(block), timeout=timeout), \
        "the page sent no measurement"
    return json.loads(board.js("JSON.stringify(window.__mr[%s])" % json.dumps(block)))


def _near(board, block, px, timeout):
    return board.until("(function(){var f=document.querySelector('#dz-%s iframe');"
                       "return !!f && Math.abs(f.getBoundingClientRect().height - %d) <= 2})()" % (block, px),
                       timeout=timeout)


def _settle(board, seconds):
    """Actually wait: Cdp.pump(n) returns on the first message, not after n seconds."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        board.cdp.pump(max(0.05, min(0.2, end - time.monotonic())))


def test_a_long_page_follows_its_page_down(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Long page",
                               "html": _page("cv", 8), "display": "long"}])
    b = _open(chrome, server, data, "cv")
    assert b.until("(function(){var f=document.querySelector('#dz-cv iframe');"
                   "return !!f && f.getBoundingClientRect().height > 7000})()", timeout=20), _h(b, "cv")
    assert _measure(b, "cv")["sections"] == 8
    _tell(b, "cv", {"cut": 2})                          # all but two sections go: 2000 px of content
    assert _near(b, "cv", 2000, timeout=5.0), "the long page stayed at %s" % _h(b, "cv")
    m = _measure(b, "cv")
    assert m["sections"] == 2 and m["bsh"] == 2000, m
    assert abs(m["root"] - 2000) <= 2, m                # the page's own box is 2000, not the old 8000 viewport
    assert m["ih"] <= 2002, m                           # the frame is now the page's height


def test_an_auto_height_frame_follows_its_page_down(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "fr", "title": "Frame",
                               "html": _page("fr", 3, rem=500)}])
    b = _open(chrome, server, data, "fr")
    assert _near(b, "fr", 3500, timeout=20), "the frame did not grow to its page: %s" % _h(b, "fr")
    m = _measure(b, "fr")
    assert m["root"] == 3500 and m["sections"] == 4, m
    _tell(b, "fr", {"cut": 2})                          # 2000 px of content left
    assert _near(b, "fr", 2000, timeout=5.0), "the frame stayed at %s" % _h(b, "fr")
    m = _measure(b, "fr")
    assert m["sections"] == 2 and m["bsh"] == 2000, m


def test_a_long_page_growth_still_follows_the_page(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Long page",
                               "html": _page("cv", 2), "display": "long"}])
    b = _open(chrome, server, data, "cv")
    assert _near(b, "cv", 2000, timeout=20), _h(b, "cv")
    _tell(b, "cv", {"add": 3000})
    assert _near(b, "cv", 5000, timeout=5.0), "the long page did not follow the page up: %s" % _h(b, "cv")
    m = _measure(b, "cv")
    assert m["sections"] == 3 and abs(m["root"] - 5000) <= 2, m


def test_a_frame_growth_stops_at_the_frame_cap(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "fr", "title": "Frame",
                               "html": _page("fr", 2)}])
    b = _open(chrome, server, data, "fr")
    assert _near(b, "fr", 2000, timeout=20), _h(b, "fr")
    _tell(b, "fr", {"add": 3000})
    assert _near(b, "fr", ART_MAX, timeout=5.0), "the frame left its cap: %s" % _h(b, "fr")
    m = _measure(b, "fr")
    assert m["sections"] == 3 and abs(m["root"] - 5000) <= 2, m


def test_a_long_page_stops_at_the_long_page_cap(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Huge",
                               "html": _page("cv", 42), "display": "long"}])
    b = _open(chrome, server, data, "cv")
    assert b.until("(function(){var f=document.querySelector('#dz-cv iframe');"
                   "return !!f && Math.abs(f.getBoundingClientRect().height - %d) <= 2})()" % LONG_MAX,
                   timeout=25), _h(b, "cv")
    m = _measure(b, "cv")
    assert m["root"] >= 42000 and m["dsh"] >= 42000, m


def test_a_page_that_sizes_itself_to_the_viewport_does_not_collapse_or_oscillate(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "hp", "title": "Full",
                               "html": _page("hp", 2, extra="html,body{height:100%}")}])
    b = _open(chrome, server, data, "hp")
    assert _near(b, "hp", 2000, timeout=20), "the frame did not grow to its content: %s" % _h(b, "hp")
    _settle(b, 1.5)
    start = _h(b, "hp")
    assert ART_MIN <= start <= 2002, start
    _tell(b, "hp", {"cut": 0})                          # html/body are 100% of the frame: the page is still 2000 tall
    _settle(b, 1.0)
    heights = []
    for _ in range(13):                                 # 3 s at 250 ms
        heights.append(_h(b, "hp"))
        _settle(b, 0.25)
    assert all(abs(h - start) <= 2 for h in heights), "a 100% page collapsed or moved: %s" % heights
    assert len({round(h) for h in heights}) == 1, heights
    m = _measure(b, "hp")
    assert m["sections"] == 0 and m["root"] >= 1998, m


def test_body_margins_are_part_of_the_page_height(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "fr", "title": "Margins",
                               "html": _page("fr", 2, extra="body{margin:8px}")}])
    b = _open(chrome, server, data, "fr")
    assert _near(b, "fr", 2016, timeout=20), "the body margins were left out: %s" % _h(b, "fr")
    m = _measure(b, "fr")
    assert m["root"] == 2016 and m["bsh"] == 2000, m


def test_an_absolute_footer_counts_and_a_fixed_one_does_not(env, server, tmp_path, chrome):
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Floats", "display": "long",
                               "html": _page("cv", 2, tail="<div style='position:absolute;top:6000px;height:100px'>abs</div>"
                                                        "<div style='position:fixed;top:9000px;height:100px'>fixed</div>")}])
    b = _open(chrome, server, data, "cv")
    assert _near(b, "cv", 6100, timeout=20), "the absolute footer was not the page height: %s" % _h(b, "cv")
    m = _measure(b, "cv")
    assert m["root"] == 2000, m                         # the height came from the absolute child, not the body


def test_the_frame_cap_and_page_mode_are_unchanged(env, server, tmp_path, chrome):
    data = _review(tmp_path, [
        {"type": "artifact", "id": "fr", "title": "Frame", "html": _page("fr", 6)},
        {"type": "artifact", "id": "pg", "title": "Page", "html": _page("pg", 6), "display": "page"},
    ])
    b = _open(chrome, server, data, "fr", "pg")
    assert _near(b, "fr", ART_MAX, timeout=20), "the frame cap moved: %s" % _h(b, "fr")
    b.js("document.querySelector('#dz-pg').scrollIntoView()")
    _settle(b, 1.0)
    g = json.loads(b.js("""JSON.stringify((function(){
      function box(sel){var e=document.querySelector(sel);if(!e)return null;var r=e.getBoundingClientRect();
        return {h:r.height};}
      return {inner:innerHeight, bar:box('.dzbar'), foot:box('#dzfoot'),
              page:box('#dz-pg iframe'), frame:box('#dz-fr iframe')};})())"""))
    assert g["bar"] and g["foot"], g
    expected = g["inner"] - g["bar"]["h"] - g["foot"]["h"]
    assert abs(g["page"]["h"] - expected) <= 2, g          # one viewport tall, whatever the page says
    assert g["page"]["h"] < 3000 and g["frame"]["h"] == ART_MAX, g
