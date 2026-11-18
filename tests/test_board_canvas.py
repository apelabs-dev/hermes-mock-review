"""Real-Chrome regressions for pins on the board canvas: stable geometry, an observing lifecycle, one ref per surface,
and a pinnable bare board canvas. Each test fails on the round-1 head (8424ef6)."""
import json
import time
from pathlib import Path

import pytest

from conftest import http
from mockreview import service, store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
DRAFT = ("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}})"
         ".find(v=>v&&v.pins&&v.pins.length)")
LONG_COMMENT = ('This header note is intentionally long so the header section really grows when it is saved, '
                'which pushes every later section down the page. ') * 5

# Wraps ResizeObserver before board.js runs: every observe records the target tag and a callback count, and a read-time
# isConnected check separates live targets from the detached nodes a leaked sticky observer would keep watching.
STICKY_PROBE = r"""
(function () {
  var Native = window.ResizeObserver;
  if (!Native) return;
  var records = [];
  function tagOf(el) {
    if (!el) return '';
    if (el.id) return el.id;
    if (typeof el.className === 'string' && el.className) return el.className;
    return String(el.tagName || '').toLowerCase();
  }
  function StickyObserver(cb) {
    var rec = { tags: [], els: [], callbacks: 0, disposed: false };
    records.push(rec);
    var inner = new Native(function (entries, obs) { rec.callbacks++; cb(entries, obs); });
    this.observe = function (el) { rec.tags.push(tagOf(el)); rec.els.push(el); inner.observe(el); };
    this.unobserve = function (el) {
      for (var i = 0; i < rec.els.length; i++) {
        if (rec.els[i] !== el) continue;
        rec.els.splice(i, 1);
        rec.tags.splice(i, 1);
        break;
      }
      inner.unobserve(el);
    };
    this.disconnect = function () { rec.disposed = true; inner.disconnect(); };
  }
  window.ResizeObserver = StickyObserver;
  window.__stickyObservers = function () {
    var out = { tags: [], connected: 0, detached: 0, callbacks: 0, live: 0 };
    for (var i = 0; i < records.length; i++) {
      var rec = records[i];
      if (rec.disposed) continue;   // a disposed observer no longer watches anything
      var sticky = false;
      for (var j = 0; j < rec.els.length; j++) {
        if (rec.tags[j] !== 'dzbar' && rec.tags[j] !== 'dzfoot') continue;
        sticky = true;
        out.tags.push(rec.tags[j]);
        if (rec.els[j].isConnected) out.connected++; else out.detached++;
      }
      if (sticky) { out.live++; out.callbacks += rec.callbacks; }
    }
    return out;
  };
})();
"""


@pytest.fixture
def canvas(env, tmp_path, server, chrome):
    path = tmp_path / 'canvas.json'
    source = Path(store.__file__).resolve().parent.parent / 'examples/plan-demo/decisions.json'
    path.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    data = store.create_review('Canvas probes', 'Header prose', ORIGIN, 'local',
                               store.parse_round_spec(decisions=str(path)), wake=False)
    b = Board(chrome, f'{server}/r/{data["id"]}/', wait="!!document.querySelector('[data-mr-ref=defaults] p.itext')")
    b.review = data
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    return b


def coords(b, selector):
    return json.loads(b.js(
        "JSON.stringify((()=>{let e=document.querySelector(%s);e.scrollIntoView({block:'center'});"
        "let r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})())" % json.dumps(selector)))


def center(b, selector):
    return json.loads(b.js(
        "JSON.stringify((()=>{let r=document.querySelector(%s).getBoundingClientRect();"
        "return [r.left+r.width/2,r.top+r.height/2]})())" % json.dumps(selector)))


def draft(b):
    return json.loads(b.js(f'JSON.stringify({DRAFT})'))


def save(b, text='Please adjust this'):
    assert b.until("!!document.querySelector('.popover textarea')")
    b.js("document.querySelector('.popover textarea').value=%s" % json.dumps(text))
    b.press('.popover .btn-primary')
    assert b.until(f'!!({DRAFT})')
    time.sleep(.25)


def assert_centre(selector, target_selector, b, tolerance=3):
    marker, target = center(b, selector), center(b, target_selector)
    assert abs(marker[0] - target[0]) < tolerance, (marker, target)
    assert abs(marker[1] - target[1]) < tolerance, (marker, target)


def document_centre(b, selector):
    return json.loads(b.js(
        "JSON.stringify((()=>{let r=document.querySelector(%s).getBoundingClientRect();"
        "return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]})())" % json.dumps(selector)))


def assert_document_spot(b, selector, expected, tolerance=3):
    got = document_centre(b, selector)
    assert abs(got[0] - expected[0]) < tolerance and abs(got[1] - expected[1]) < tolerance, (got, expected)


def reload(b, wait):
    url = b.js('location.href')
    b.cdp.call('Page.navigate', {'url': url}, session=b.sid)
    assert b.until(wait, timeout=60), 'the board did not come back after reload'
    time.sleep(.4)


def add_header_comment(b, text=LONG_COMMENT):
    b.click(*coords(b, '.doc-head h1'))
    save(b, text[:590])
    assert b.until("!!document.querySelector('.doc-head .ipins li')", timeout=10)
    time.sleep(.5)


def assert_marker_visible_and_hittable(b, selector):
    got = json.loads(b.js(
        "JSON.stringify((()=>{let m=document.querySelector(%s);if(!m)return null;"
        "let top=parseFloat(m.style.top);m.scrollIntoView({block:'center'});"
        "let r=m.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
        "let hit=document.elementFromPoint(x,y),hidden=[];"
        "for(let e=m.parentElement;e&&e!==document.documentElement;e=e.parentElement){"
        "let s=getComputedStyle(e);"
        "if(s.overflowX!=='visible'||s.overflowY!=='visible')hidden.push(e.className||e.tagName);}"
        "return {top:top,w:r.width,h:r.height,"
        "hit:!!hit&&(hit===m||m.contains(hit)),hidden:hidden}})())" % json.dumps(selector)))
    assert got, selector
    assert got['top'] < 0, got            # the marker really sits above its section's top edge
    assert got['w'] >= 26 and got['h'] >= 26, got
    assert not got['hidden'], got         # no overflow-clipping ancestor between the pin and the page
    assert got['hit'], got                # the marker itself is what the point hits
    return got


def test_canvas_marker_geometry_survives_lists_edits_and_reload(canvas):
    b = canvas
    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))
    save(b)
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)

    b.click(*coords(b, '[data-mr-ref=defaults] .ihead h3'))   # a second pin grows the in-flow list
    save(b)
    assert b.js("document.querySelectorAll('[data-mr-ref=defaults] .mpin').length") == 2
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)   # the first marker never moved

    b.press('[data-mr-ref=defaults] .mpin')   # editing the first pin's text does not move it
    save(b, 'Edited note')
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)

    reload(b, "document.querySelectorAll('[data-mr-ref=defaults] .mpin').length===2")
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)


def test_canvas_follows_move_delete_and_restore(canvas):
    b = canvas
    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))
    save(b)
    moved = draft(b)['pins'][0]
    b.js("document.querySelector('[data-mr-ref=defaults] p.itext').style.marginTop='150px'")
    assert b.until(
        "(()=>{let m=document.querySelector('[data-mr-ref=defaults] .mpin'),t=document.querySelector('[data-mr-ref=defaults] p.itext');"
        "if(!m||!t)return false;let a=m.getBoundingClientRect(),z=t.getBoundingClientRect();"
        "return Math.abs((a.left+a.width/2)-(z.left+z.width/2))<3&&Math.abs((a.top+a.height/2)-(z.top+z.height/2))<3})()",
        timeout=10)
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)

    time.sleep(.5)   # deliberate: waits out the debounced draft save under test before reading its fractions
    before = draft(b)['pins'][0]
    b.js("window.__removed=document.querySelector('[data-mr-ref=defaults] p.itext'); window.__removed.remove()")
    assert b.until("!!document.querySelector('[data-mr-ref=defaults] .mpin.det')", timeout=10)
    time.sleep(.5)
    after = draft(b)['pins'][0]
    assert (after['x'], after['y']) == (before['x'], before['y'])   # last good fraction kept

    b.js("(()=>{let sec=document.querySelector('[data-mr-ref=defaults]');"
         "sec.insertBefore(window.__removed, sec.querySelector('.pin-layer'))})()")
    assert b.until("!document.querySelector('[data-mr-ref=defaults] .mpin.det')", timeout=10)
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)


def test_empty_anything_else_pin_stays_in_else(canvas):
    b = canvas
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let e=document.querySelector('.blk-else');e.scrollIntoView({block:'center'});"
        "let r=e.getBoundingClientRect();return [r.right-24,r.top+24]})())"))
    b.click(*xy)
    save(b)
    got = json.loads(b.js(f"JSON.stringify({{draft:{DRAFT},"
                           "headerPins:document.querySelector('.doc-head').querySelectorAll('.mpin').length,"
                           "elsePins:document.querySelector('.blk-else').querySelectorAll('.mpin').length})"))
    assert got['elsePins'] == 1 and got['headerPins'] == 0, got
    assert got['draft']['pins'][0]['block'] == 'else'

    reload(b, "document.querySelectorAll('.blk-else .mpin').length===1")
    got = json.loads(b.js(
        "JSON.stringify({headerPins:document.querySelector('.doc-head').querySelectorAll('.mpin').length,"
        "elsePins:document.querySelector('.blk-else').querySelectorAll('.mpin').length})"))
    assert got['elsePins'] == 1 and got['headerPins'] == 0, got


def test_board_canvas_click_opens_composer_and_controls_stay_live(canvas):
    b = canvas
    for selector in ('#dz-q1 button', '#dz-d1 .decide button'):
        b.click(*coords(b, selector))
        b.cdp.pump(0.2)
        assert not b.popover(), f'{selector} must stay a live control, not a pin'

    b.js('scrollTo(0,0)')
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let d=document.querySelector('.doc'),r=d.getBoundingClientRect();"
        "for(let y=Math.max(100,r.top);y<Math.min(innerHeight-100,r.bottom);y+=3)"
        "for(let x=r.left+2;x<r.right;x+=10)if(document.elementFromPoint(x,y)===d)return [x,y];return null})())"))
    assert xy, 'the probe must find genuine empty .doc space'
    b.click(*xy)
    assert b.until("!!document.querySelector('.popover textarea')", timeout=5), 'empty canvas must accept a pin'
    save(b)
    pin = [p for p in draft(b)['pins'] if p['block'] == 'doc'][0]
    assert pin['on'] == 'board'
    assert b.js("document.querySelectorAll('.doc > .pin-layer .mpin').length") == 1

    reload(b, "document.querySelectorAll('.doc > .pin-layer .mpin').length===1")
    pin = [p for p in draft(b)['pins'] if p['block'] == 'doc'][0]
    assert pin['on'] == 'board'


def test_detached_pin_keeps_document_spot_across_delete_reflow_and_restore(canvas):
    b = canvas
    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))
    save(b)
    pinned = document_centre(b, '[data-mr-ref=defaults] .mpin')

    b.js("window.__removed=document.querySelector('[data-mr-ref=defaults] p.itext'); window.__removed.remove()")
    assert b.until("!!document.querySelector('[data-mr-ref=defaults] .mpin.det')", timeout=10)
    time.sleep(.4)
    assert_document_spot(b, '[data-mr-ref=defaults] .mpin.det', pinned)   # the shrunken section does not drag it

    b.click(*coords(b, '[data-mr-ref=defaults] .ihead h3'))                # the in-flow list grows
    save(b)
    assert b.until("document.querySelectorAll('[data-mr-ref=defaults] .mpin').length===2")
    time.sleep(.4)
    assert_document_spot(b, '[data-mr-ref=defaults] .mpin.det', pinned)

    b.js("(()=>{let sec=document.querySelector('[data-mr-ref=defaults]');"
         "sec.insertBefore(window.__removed, sec.querySelector('.pin-layer'))})()")
    assert b.until("!document.querySelector('[data-mr-ref=defaults] .mpin.det')", timeout=10)
    assert_centre('[data-mr-ref=defaults] .mpin', '[data-mr-ref=defaults] p.itext', b)
    time.sleep(.5)
    target = draft(b)['pins'][0]['target']['doc']
    marker = document_centre(b, '[data-mr-ref=defaults] .mpin')
    assert abs(target['x'] - marker[0]) < 3 and abs(target['y'] - marker[1]) < 3, (target, marker)


def test_empty_canvas_pin_keeps_document_spot_across_list_growth_and_reload(canvas):
    b = canvas
    b.js('scrollTo(0,0)')
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let d=document.querySelector('.doc'),r=d.getBoundingClientRect();"
        "for(let y=Math.max(100,r.top);y<Math.min(innerHeight-100,r.bottom);y+=3)"
        "for(let x=r.left+2;x<r.right;x+=10)if(document.elementFromPoint(x,y)===d)return [x,y];return null})())"))
    assert xy, 'the probe must find genuine empty .doc space'
    b.click(*xy)
    save(b)
    spot = document_centre(b, '.doc > .pin-layer .mpin')

    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))                  # another section gains a pin list
    save(b)
    assert b.until("document.querySelectorAll('.doc > .pin-layer .mpin').length===1")
    time.sleep(.4)
    assert_document_spot(b, '.doc > .pin-layer .mpin', spot)

    reload(b, "document.querySelectorAll('.doc > .pin-layer .mpin').length===1")
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    time.sleep(.4)
    assert_document_spot(b, '.doc > .pin-layer .mpin', spot)
    pin = [p for p in draft(b)['pins'] if p['block'] == 'doc'][0]
    doc = pin.get('doc') or (pin.get('target') or {}).get('doc')
    assert doc, pin
    assert abs(doc['x'] - spot[0]) < 3 and abs(doc['y'] - spot[1]) < 3, (doc, spot)


def test_a_resubmitted_empty_canvas_pin_keeps_its_document_spot(canvas):
    b = canvas
    b.js('scrollTo(0,0)')
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let d=document.querySelector('.doc'),r=d.getBoundingClientRect();"
        "for(let y=Math.max(100,r.top);y<Math.min(innerHeight-100,r.bottom);y+=3)"
        "for(let x=r.left+2;x<r.right;x+=10)if(document.elementFromPoint(x,y)===d)return [x,y];return null})())"))
    assert xy, 'the probe must find genuine empty .doc space'
    b.click(*xy)
    save(b)
    spot = document_centre(b, '.doc > .pin-layer .mpin')

    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))                  # a list that grows the .doc box
    save(b)
    assert b.until(f"(()=>{{var d={DRAFT};return !!d && d.pins.length===2 && "
                   "!!document.querySelector('[data-mr-ref=defaults] .mpin')})()")

    pins = draft(b)['pins']
    service.submit(b.review['id'], 1, {'pins': pins}, b.review['token'], do_notify=False)
    stored = [p for p in store.load(b.review['id'])['rounds'][0]['feedback']['pins'] if p['block'] == 'doc'][0]
    assert abs(stored['doc']['x'] - spot[0]) < 3, (stored, spot)
    assert abs(stored['doc']['y'] - spot[1]) < 3, (stored, spot)

    reload(b, "!!document.querySelector('.doc > .pin-layer .mpin')")       # normalize() from server feedback
    assert_document_spot(b, '.doc > .pin-layer .mpin', spot)


def test_a_server_empty_canvas_thread_keeps_its_document_point(canvas, server):
    b = canvas
    b.js('scrollTo(0,0)')
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let d=document.querySelector('.doc'),r=d.getBoundingClientRect();"
        "for(let y=Math.max(100,r.top);y<Math.min(innerHeight-100,r.bottom);y+=3)"
        "for(let x=r.left+2;x<r.right;x+=10)if(document.elementFromPoint(x,y)===d)return [x,y];return null})())"))
    assert xy, 'the probe must find genuine empty .doc space'
    b.click(*xy)
    save(b)
    assert b.until("!!document.querySelector('.mpin[data-thread]')")
    local = [p for p in draft(b)['pins'] if p['block'] == 'doc'][0]
    assert local.get('doc') and 'target' not in local
    code, _, body = http('GET', f"{server}/r/{b.review['id']}/api/threads")
    assert code == 200
    anchor = json.loads(body)['threads'][0]['anchor']
    assert 'target' not in anchor and 'doc' in anchor
    assert round(float(local['doc']['x']), 2) == anchor['doc']['x']
    assert round(float(local['doc']['y']), 2) == anchor['doc']['y']


def test_a_server_only_empty_canvas_thread_stays_on_its_document_spot(canvas, server):
    b = canvas
    b.js('scrollTo(0,0)')
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let d=document.querySelector('.doc'),r=d.getBoundingClientRect();"
        "for(let y=Math.max(100,r.top);y<Math.min(innerHeight-100,r.bottom);y+=3)"
        "for(let x=r.left+2;x<r.right;x+=10)if(document.elementFromPoint(x,y)===d)return [x,y];return null})())"))
    assert xy, 'the probe must find genuine empty .doc space'
    b.click(*xy)
    save(b)
    assert b.until("!!document.querySelector('.mpin[data-thread]')")
    b.js('localStorage.clear()')
    reload(b, "!!document.querySelector('[data-mr-ref=defaults] p.itext')")
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    assert b.until("document.querySelectorAll('.doc > .pin-layer .mpin[data-thread]').length===1")
    spot = document_centre(b, '.doc > .pin-layer .mpin[data-thread]')

    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))   # the section list above grows the doc box
    save(b)
    assert b.until("document.querySelectorAll('[data-mr-ref=defaults] .mpin').length===1")
    time.sleep(0.4)
    assert_document_spot(b, '.doc > .pin-layer .mpin[data-thread]', spot)


def test_a_server_empty_section_thread_keeps_a_page_point_above_its_section(canvas, server):
    """A pin near a section's top must keep its page point when a section above grows past it.

    The server-only fallback used to clamp its page offsets at zero on both axes, so a pin whose stored page point
    ended up above its section's top edge jumped down to that edge (9a3b940).
    """
    b = canvas
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let s=document.querySelector('[data-mr-ref=defaults]');"
        "s.scrollIntoView({block:'center'});let r=s.getBoundingClientRect();"
        "for(let y=Math.max(4,r.top+2);y<Math.min(innerHeight-4,r.bottom);y+=2)"
        "for(let x=r.left+4;x<r.right-4;x+=40)if(document.elementFromPoint(x,y)===s)return [x,y];return null})())"))
    assert xy, 'the probe must find genuine empty section space just below its top edge'
    b.click(*xy)
    save(b)
    marker = '[data-mr-ref=defaults] > .pin-layer .mpin[data-thread]'
    assert b.until("!!document.querySelector(%s)" % json.dumps(marker))
    code, _, body = http('GET', f"{server}/r/{b.review['id']}/api/threads")
    assert code == 200 and len(json.loads(body)['threads']) == 1, body
    spot = document_centre(b, marker)
    b.js('localStorage.clear()')
    reload(b, "!!document.querySelector('[data-mr-ref=defaults]')")
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    assert b.until("document.querySelectorAll(%s).length===1" % json.dumps(marker))
    time.sleep(.4)
    assert_document_spot(b, marker, spot)

    add_header_comment(b)     # a section above grows past this pin's page point
    assert_document_spot(b, marker, spot)
    assert_marker_visible_and_hittable(b, marker)


def test_targetless_pin_keeps_document_spot_when_the_header_grows(canvas):
    b = canvas
    xy = json.loads(b.js(
        "JSON.stringify((()=>{let e=document.querySelector('[data-mr-ref=defaults]');e.scrollIntoView({block:'center'});"
        "let r=e.getBoundingClientRect();return [r.right-24,r.top+24]})())"))
    b.click(*xy)
    save(b)
    spot = document_centre(b, '[data-mr-ref=defaults] .mpin')
    pin = draft(b)['pins'][0]
    assert pin.get('doc'), pin
    assert abs(pin['doc']['x'] - spot[0]) < 3 and abs(pin['doc']['y'] - spot[1]) < 3, (pin, spot)

    add_header_comment(b)                                  # the preceding header section grows and moves defaults down
    assert_document_spot(b, '[data-mr-ref=defaults] .mpin', spot)
    assert_marker_visible_and_hittable(b, '[data-mr-ref=defaults] .mpin')

    reload(b, "document.querySelectorAll('[data-mr-ref=defaults] .mpin').length===1")
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    time.sleep(.4)
    assert_document_spot(b, '[data-mr-ref=defaults] .mpin', spot)   # reload re-enters the same fallback path


def test_sticky_header_and_footer_observers_dispose_across_rerenders(canvas):
    b = canvas
    b.cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': STICKY_PROBE}, session=b.sid)
    reload(b, "!!document.querySelector('#dzbar') && !!document.querySelector('#dzfoot') && !!window.__stickyObservers")
    stats = json.loads(b.js('JSON.stringify(window.__stickyObservers())'))
    assert sorted(stats['tags']) == ['dzbar', 'dzfoot'] and stats['connected'] == 2, stats

    for _ in range(4):
        b.press('.wseg button:not(.on)')
        time.sleep(0.7)

    stats = json.loads(b.js('JSON.stringify(window.__stickyObservers())'))
    print('STICKY_DETACHED', stats['detached'])
    assert stats['detached'] == 0, stats
    assert stats['connected'] == 2, stats

    for sel, prop in (('#dzbar', '--barh'), ('#dzfoot', '--footh')):
        got = json.loads(b.js(
            "JSON.stringify((()=>{var e=document.querySelector(%s);return {prop:document.documentElement.style"
            ".getPropertyValue(%s),h:e.offsetHeight}})())" % (json.dumps(sel), json.dumps(prop))))
        assert got['prop'] == '%dpx' % got['h'], (sel, got)   # the live observer kept the custom property current
        b.js("(()=>{var e=document.querySelector(%s);e.style.height=(e.offsetHeight+60)+'px'})()"
             % json.dumps(sel))
        assert b.until(
            "(()=>{var e=document.querySelector(%s);return document.documentElement.style.getPropertyValue(%s)"
            "===e.offsetHeight+'px'&&e.offsetHeight>%d})()" % (json.dumps(sel), json.dumps(prop), got['h']),
            timeout=10), (sel, got)                            # and still tracks a height change after the rerenders

    before = json.loads(b.js('JSON.stringify(window.__stickyObservers())'))['callbacks']
    time.sleep(1)   # deliberate: this second is the measured idle window (a leaked observer would tick in it)
    delta = json.loads(b.js('JSON.stringify(window.__stickyObservers())'))['callbacks'] - before
    print('STICKY_CALLBACK_DELTA', delta)
    assert delta < 2, delta


def test_detached_pin_keeps_document_spot_when_the_header_grows(canvas):
    b = canvas
    b.click(*coords(b, '[data-mr-ref=defaults] p.itext'))
    save(b)

    b.js("window.__removed=document.querySelector('[data-mr-ref=defaults] p.itext'); window.__removed.remove()")
    assert b.until("!!document.querySelector('[data-mr-ref=defaults] .mpin.det')", timeout=10)
    time.sleep(.4)
    spot = document_centre(b, '[data-mr-ref=defaults] .mpin.det')

    add_header_comment(b)                                  # the preceding header section grows and moves defaults down
    assert_document_spot(b, '[data-mr-ref=defaults] .mpin.det', spot)
    assert_marker_visible_and_hittable(b, '[data-mr-ref=defaults] .mpin.det')
