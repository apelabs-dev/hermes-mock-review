"""E2 acceptance: actual Chrome, opaque frames, recovery, native controls and hostile target data.

DOM move/resize/deleted/missing/empty case table adapted behaviorally from
https://github.com/maciekzygmunt/excaliself/blob/96b922c/src/features/comments/anchor.test.ts
(MIT; no expressive source copied). Rotation/linear canvas cases do not apply to DOM anchors.
"""
import base64
import copy
import json
import os
import time
from pathlib import Path

import pytest

from mockreview import feedback, store, targets
from conftest import http
from test_live_round_tool import Board, chrome  # real CDP Chrome fixture, never a fake DOM

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
TARGET = {"path": '[data-mr-part="summary"]', "id": "mr-2", "part": "summary", "sig": {"tag": "p", "h": "abc"},
          "x": 0.5, "y": 0.5, "pin": {"x": 0, "y": 0}, "doc": {"x": 100, "y": 200}, "label": "Summary"}
ROOT = "document.querySelector('.mock-host').shadowRoot"
DRAFT = "Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}}).find(v=>v&&v.pins&&v.pins.length)"


@pytest.fixture
def anchor_review(env, tmp_path, server):
    markup = ('<section><h2>Order summary</h2><p data-mr-part="summary" id="copy" '
              'style="width:260px;height:60px;margin:20px">Shipping <strong>arrives tomorrow</strong> safely</p>'
              '<form id="form"><input id="check" type="checkbox"><input id="field" required>'
              '<button id="submit">Submit</button><select id="select"><option>One</option><option>Two</option></select>'
              '<textarea id="area"></textarea></form><details><summary>Details</summary>More</details>'
              f'<a href="{server}/healthz"><span id="link">Open health</span></a>'
              '<div data-mr-live><span id="live">Live region</span></div>'
              '<div id="editable" contenteditable>Editable text</div></section>')
    path = tmp_path / 'anchors.json'
    path.write_text(json.dumps({"blocks": [{"type": "mock", "id": "m1", "html": markup}]}), encoding="utf-8")
    data = store.create_review('Anchors', '', ORIGIN, 'local', store.parse_round_spec(decisions=str(path)), wake=False)
    return data['id']


def board(chrome, server, rid):
    b = Board(chrome, f'{server}/r/{rid}/', wait="!!document.querySelector('.mock-host')?.shadowRoot?.querySelector('#copy')")
    b.js("import('./anchors.js').then(m=>window.__anchors=m)")
    assert b.until('!!window.__anchors')
    return b


def on(b):
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")


def point(b, sel):
    return json.loads(b.js(f"(()=>{{let e={ROOT}.querySelector({json.dumps(sel)});Element.prototype.scrollIntoView.call(e,{{block:'center'}});let r=Element.prototype.getBoundingClientRect.call(e);return JSON.stringify([r.left+r.width/2,r.top+r.height/2])}})()"))


def save(b):
    b.js("document.querySelector('.popover textarea').value='Please adjust this'")
    b.press('.popover .btn-primary')
    assert b.until(f'!!({DRAFT})')


def shot(b, name):
    dest = os.environ.get('MR_E2_EVIDENCE')
    if dest:
        (Path(dest) / 'browser-version.json').write_text(json.dumps(b.cdp.call('Browser.getVersion'), indent=2), encoding="utf-8")
        result = b.cdp.call('Page.captureScreenshot', {'format': 'png'}, session=b.sid)
        (Path(dest) / name).write_bytes(base64.b64decode(result['data']))


def test_pin_target_is_validated_and_rendered(env, tmp_path):
    index = [{"id": "m1", "options": [], "kind": "piece"}]
    t = copy.deepcopy(TARGET)
    t.update(x=-20, y=99, label='x' * 200, part='p' * 70, doc={"x": float('inf'), "y": 9999999})
    t['quote'] = {"exact": 'a`quoted` phrase', "prefix": 'p' * 80, "suffix": 's' * 80}
    t['position'] = {"start": 0, "end": 16}
    raw = {"pins": [{"block": "m1", "x": .2, "y": .3, "text": '<img onerror=bad>', "target": t}]}
    fb = feedback.sanitize_decisions(raw, index)
    target = fb['pins'][0]['target']
    assert target['x'] == 0 and target['y'] == 1 and len(target['label']) == 120 and len(target['part']) == 40
    assert target['doc'] == {"x": 0, "y": 1000000} and len(target['quote']['prefix']) == 32
    assert target['position']['type'] == 'TextPositionSelector'
    rendered = targets.target_markdown(target)
    assert 'part: ' in rendered and 'label: ' in rendered and 'path: ' in rendered and 'quote: aquoted phrase' in rendered
    assert '`' not in rendered
    for bad in ('[', 'p:has(*)', 'p,p', 'x' * 301, 'p;alert(1)', 'a\nbutton', '0', '#0', 'p >', 'a' * 299 + '(', {'x': 1}):
        broken = dict(TARGET, path=bad)
        raw['pins'][0]['target'] = broken
        assert 'target' not in feedback.sanitize_decisions(raw, index)['pins'][0]
    assert targets.clean_target(dict(TARGET, sig={"tag": [], "h": {}})) is None
    assert targets.clean_target(dict(TARGET, quote={"exact": 'q' * 161})).get('quote') is None
    for path in ('#copy', 'section > p:nth-child(2)', '[data-mr-part="summary"]', 'div.card > p'):
        assert targets.safe_path(path) == path
    old = {"pins": [{"block": "m1", "x": .2, "y": .3, "text": 'old'}]}
    pins = feedback.sanitize_decisions(old, index)['pins']
    assert [{k: v for k, v in p.items() if k != 'id'} for p in pins] == [dict(old['pins'][0], option=None, w='desktop')]
    assert pins[0]['id'].startswith('p')
    assert old['pins'][0] == {"block": "m1", "x": .2, "y": .3, "text": 'old'}


def test_round_install_stamps_once_without_changing_source(anchor_review, tmp_path):
    original = (tmp_path / 'anchors.json').read_text(encoding="utf-8")
    saved = json.loads((store.review_dir(anchor_review) / 'rounds/1/a/decisions.json').read_text(encoding="utf-8"))
    html = saved['blocks'][0]['html']
    assert 'data-mr-id=' in html and 'data-mr-id=' not in original
    assert targets.stamp_html(html) == html
    assert 'data-mr-id="explicit"' in targets.stamp_html('<p data-mr-id="explicit">hi</p>')


def test_describe_target_prefers_part_and_caps_lengths(server, anchor_review, chrome):
    b = board(chrome, server, anchor_review)
    result = json.loads(b.js(f"JSON.stringify(window.mockreview.comments.anchorFor({ROOT}.querySelector('#copy')))"))
    assert result['path'] == '[data-mr-part="summary"]' and result['part'] == 'summary'
    assert result['label'].startswith('Order summary > p')
    assert b.js(f"window.mockreview.comments.anchorFor({ROOT}.querySelector('h2')).label") == 'Order summary'
    b.js(f"{ROOT}.querySelector('#copy').textContent='x'.repeat(400)")
    result = json.loads(b.js(f"JSON.stringify(window.mockreview.comments.anchorFor({ROOT}.querySelector('#copy')))"))
    assert len(result['label']) <= 120 and 'x' * 41 not in result['label']
    assert b.js('window.mockreview.comments.anchorFor(null)') is None
    assert b.js(f"(()=>{{let root={ROOT}.querySelector('.app');let a=document.createElement('p'),c=document.createElement('p');a.textContent=c.textContent='Repeated';root.append(a,c);window.__anchors.stamp(root);let t=window.__anchors.anchorFor(a,root),old=a.dataset.mrId;a.remove();let d=document.createElement('p');d.textContent='Repeated';root.append(d);window.__anchors.stamp(root);let ok=d.dataset.mrId!==old && window.__anchors.resolve(root,t)===null;c.remove();d.remove();return ok}})()") is True
    assert b.js('JSON.stringify(window.mockreview.comments.customAnchors())') == '[]'
    assert b.js('window.mockreview.comments.openComposer({element:document.body})') is None
    assert b.js('window.mockreview.comments.openComposer(null)') is None
    assert b.js(f"(()=>{{let e={ROOT}.querySelector('#copy');let t=window.mockreview.comments.anchorFor(e);e.textContent='same same';t.quote={{exact:'same',prefix:'',suffix:''}};return window.__anchors.rangeFor(e,t)}})()") is None
    assert b.js(f"window.__anchors.cleanTarget({json.dumps(dict(TARGET, path='['))})") is None


def test_pin_mode_tap_records_target_and_links_still_open(server, anchor_review, chrome):
    b = board(chrome, server, anchor_review)
    on(b)
    # Composed descendants of native controls remain live, including submit/selection events.
    b.js(f"{ROOT}.querySelector('#form').addEventListener('submit',e=>{{e.preventDefault();window.__submitted=true}})")
    b.click(*point(b, '#check'))
    assert b.js(f"{ROOT}.querySelector('#check').checked") and not b.popover()
    b.click(*point(b, '#field'))
    b.key('c', 'KeyC', 67, text='c')
    assert b.js(f"{ROOT}.querySelector('#field').value") == 'c' and b.aria() == 'true'
    b.click(*point(b, '#submit'))
    assert b.until('window.__submitted === true') and not b.popover()
    # Without the test's listener (a clone carries none), the board itself stops a mock form navigating.
    href = b.js('location.href')
    assert b.js(f"(()=>{{let f={ROOT}.querySelector('#form').cloneNode(true);{ROOT}.querySelector('#form').after(f);let ev=new Event('submit',{{bubbles:true,cancelable:true,composed:true}});f.dispatchEvent(ev);f.remove();return ev.defaultPrevented}})()") is True
    assert b.js('location.href') == href
    b.click(*point(b, 'summary'))
    assert b.js(f"{ROOT}.querySelector('details').open") and not b.popover()
    for sel in ('#live', '#editable'):
        b.click(*point(b, sel)); assert not b.popover()
    before = {t['targetId'] for t in chrome.call('Target.getTargets')['targetInfos']}
    b.click(*point(b, '#link'))
    assert b.until("!document.querySelector('.popover')")
    targets_now = chrome.call('Target.getTargets')['targetInfos']
    opened = [t for t in targets_now if t['targetId'] not in before]
    assert opened, 'real native link click must open a tab'
    for t in opened:
        chrome.call('Target.closeTarget', {'targetId': t['targetId']})
    b.click(*point(b, '#copy'))
    assert b.until("document.querySelector('.popover .pop-where').textContent.startsWith('on Order summary')")
    save(b)
    draft = json.loads(b.js(f'JSON.stringify({DRAFT})'))
    assert draft['pins'][0]['target']['part'] == 'summary'
    # The same bounded target survives real submission and markdown generation.
    payload = {k: draft[k] for k in ('marks', 'pins', 'guidelines')}
    data = store.load(anchor_review)
    code, _, response = http('POST', f'{server}/r/{anchor_review}/api/submit',
                             {'round': 1, 'token': data['token'], 'feedback': payload})
    assert code == 200
    saved = store.load(anchor_review)['rounds'][0]['feedback']
    assert saved['pins'][0]['target']['part'] == 'summary'
    markdown = (store.review_dir(anchor_review) / 'rounds/1/feedback.md').read_text(encoding="utf-8")
    assert 'part: summary' in markdown and 'path: [data-mr-part="summary"]' in markdown



def test_element_move_resize_delete_restore_and_empty_space(server, anchor_review, chrome):
    b = board(chrome, server, anchor_review); on(b)
    xy = point(b, '#copy')
    chrome.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': xy[0], 'y': xy[1]}, session=b.sid)
    assert b.until("!document.querySelector('.mr-hover').hidden")
    shot(b, 'hover-on.png')
    b.click(*xy); assert b.until("!!document.querySelector('.popover')"); save(b)
    initial = json.loads(b.js("JSON.stringify((()=>{let r=document.querySelector('.mpin').getBoundingClientRect();return [r.left,r.top]})())"))
    b.js(f"Object.assign({ROOT}.querySelector('#copy').style,{{marginLeft:'90px',height:'120px'}})")
    assert b.until(f"Math.abs(document.querySelector('.mpin').getBoundingClientRect().left-{initial[0]}) > 20")
    moved = b.js("document.querySelector('.mpin').getBoundingClientRect().top + scrollY")
    assert b.js("!document.querySelector('.mpin').classList.contains('det')")
    b.js(f"window.__removed={ROOT}.querySelector('#copy');window.__parent=window.__removed.parentNode;window.__removed.remove()")
    assert b.until("!!document.querySelector('.mpin.det')")
    assert abs(b.js("document.querySelector('.mpin').getBoundingClientRect().top + scrollY") - moved) < 2
    b.click_sel('.mpin')
    assert b.until("document.querySelector('.popover').textContent.includes('Element removed')")
    shot(b, 'detached-amber.png'); b.press('.popover .tp-close')
    b.js('window.__parent.appendChild(window.__removed)')
    assert b.until("!document.querySelector('.mpin').classList.contains('det')")
    # Ambiguous explicit identities must detach rather than pick the first element.
    b.js('window.__parent.appendChild(window.__removed.cloneNode(true))')
    assert b.until("!!document.querySelector('.mpin.det')")
    b.js('window.__parent.lastChild.remove()')
    assert b.until("!document.querySelector('.mpin').classList.contains('det')")
    # Empty-space anchor has no target and does not move when nearby content moves.
    b.js("window.__emptyRoot=document.createElement('div');document.body.appendChild(window.__emptyRoot);window.__emptyPin={x:.7,y:.1};window.__emptyDraws=0;window.__emptySurface=window.__anchors.surface(window.__emptyRoot,null,{enabled:()=>false,box:()=>window.__emptyRoot.getBoundingClientRect(),pins:()=>[window.__emptyPin],draw:()=>{window.__emptyDraws++}});window.__emptyRoot.appendChild(document.createElement('p'))")
    assert b.until("window.__emptyDraws>0 && window.__emptyPin.x===.7 && window.__emptyPin.y===.1 && !window.__emptyPin.detached")
    time.sleep(.15)
    draws = b.js('window.__emptyDraws')
    time.sleep(.15)
    assert b.js('window.__emptyDraws') == draws, 'observers must settle instead of redrawing forever'
    b.js('window.__emptySurface.dispose();window.__emptyRoot.remove()')
    assert b.js(f"window.__anchors.anchorFor({ROOT}.querySelector('.app'), {ROOT}.querySelector('.app'))") is None
    b.press('#comment-toggle')
    assert b.until("document.querySelector('.mr-hover').hidden")
    shot(b, 'hover-off.png')
    # Reload really recovers the saved stable identity and keeps the captured label.
    b.cdp.call('Page.reload', session=b.sid)
    assert b.until("!!document.querySelector('.mpin')")
    assert not b.js("document.querySelector('.mpin').classList.contains('det')")
    # A stored v1.2.0 coordinate-only draft loads without migration or metadata writes. Seed it from a
    # document-start script: an anchor re-measure can still have a 250 ms saveDraft queued, and that write landing
    # between a localStorage.setItem and the reload would restore the previous draft over the seeded one.
    old = json.loads(b.js("JSON.stringify((()=>{let entry=Object.entries(localStorage).find(([k,v])=>{try{return JSON.parse(v).pins?.length}catch(_){return false}});let d=JSON.parse(entry[1]);d.pins=[{block:'m1',option:null,w:'desktop',x:.25,y:.3,text:'v1.2.0 note'}];return [entry[0],JSON.stringify(d)]})())"))
    seeded = b.cdp.call('Page.addScriptToEvaluateOnNewDocument',
                        {'source': 'localStorage.setItem(%s,%s)' % (json.dumps(old[0]), json.dumps(old[1]))},
                        session=b.sid)
    try:
        b.cdp.call('Page.reload', session=b.sid)
        assert b.until("document.querySelector('.mpin')?.title==='v1.2.0 note'")
        assert b.js('localStorage.getItem(%s)' % json.dumps(old[0])) == old[1]
    finally:
        b.cdp.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': seeded['identifier']}, session=b.sid)


@pytest.mark.parametrize('fallback', [False, True])
def test_range_persists_reanchors_and_highlights(server, anchor_review, chrome, fallback):
    b = board(chrome, server, anchor_review)
    if fallback:
        # Next surfaces detect the unavailable API, exercising the actual wrap implementation.
        b.cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': 'Object.defineProperty(CSS,"highlights",{value:undefined})'}, session=b.sid)
        b.cdp.call('Page.reload', session=b.sid)
        assert b.until(f"!!document.querySelector('.mock-host')?.shadowRoot?.querySelector('#copy')")
    on(b)
    # The page API accepts a Range spanning nested text nodes.
    b.js(f"window.__range=document.createRange();let e={ROOT}.querySelector('#copy');window.__range.setStart(e.firstChild,0);window.__range.setEnd(e.querySelector('strong').firstChild,7)")
    result = b.js('window.mockreview.comments.openComposer({range:window.__range})')
    assert result and result['quote']['exact'] == 'Shipping arrives'
    assert b.until("!!document.querySelector('.popover')"); save(b)
    highlighted = "!!document.querySelector('.mock-host')?.shadowRoot?.querySelector('mark[data-mr-highlight]')" if fallback else "(CSS.highlights.get('mr-comments')?.size || 0) > 0"
    assert b.until(highlighted)
    shot(b, 'range-fallback.png' if fallback else 'range-native.png')
    captured = json.loads(b.js(f'JSON.stringify({DRAFT})'))['pins'][0]['target']
    assert captured['position']['start'] == 0 and captured['position']['end'] == 16
    # Insert content before the scoped element: position in the document changes, identity remains.
    b.js(f"{ROOT}.querySelector('#copy').before(document.createElement('p'))")
    assert b.until(highlighted)
    b.cdp.call('Page.reload', session=b.sid)
    assert b.until(highlighted) and not b.js("document.querySelector('.mpin').classList.contains('det')")
    b.js(f"window.__text={ROOT}.querySelector('#copy').innerHTML;{ROOT}.querySelector('#copy').textContent='Replacement'")
    assert b.until("!!document.querySelector('.mpin.det')")
    b.js(f"{ROOT}.querySelector('#copy').innerHTML=window.__text")
    assert b.until("!document.querySelector('.mpin').classList.contains('det')")


def test_real_text_drag_opens_range_composer(server, anchor_review, chrome):
    b = board(chrome, server, anchor_review); on(b)
    coords = json.loads(b.js(f"(()=>{{let e={ROOT}.querySelector('#copy');e.scrollIntoView({{block:'center'}});let r=document.createRange();r.setStart(e.firstChild,0);r.setEnd(e.firstChild,8);let b=r.getBoundingClientRect();return JSON.stringify([b.left+1,b.top+b.height/2,b.right-1])}})()"))
    x, y, end = coords
    chrome.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y}, session=b.sid)
    chrome.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'buttons': 1, 'clickCount': 1}, session=b.sid)
    for step in range(1, 7):
        chrome.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x + (end - x) * step / 6, 'y': y, 'button':'left', 'buttons': 1}, session=b.sid)
        chrome.pump(0.04)
    selection = b.js(f"JSON.stringify({{text:{ROOT}.getSelection().toString(),collapsed:{ROOT}.getSelection().isCollapsed,outer:getSelection().toString()}})")
    assert json.loads(selection)['text'], selection
    chrome.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': end, 'y': y, 'button': 'left', 'clickCount': 1}, session=b.sid)
    assert b.until("!!document.querySelector('.popover')")
    save(b)
    assert json.loads(b.js(f'JSON.stringify({DRAFT})'))['pins'][0]['target']['quote']['exact'].startswith('Shipping')


def describe_target(b):
    """Ask the prototype fixture for an anchor, re-asking until the bundled probe has installed window.mockreview.

    store.inject_probe puts the probe at the END of a prototype page, so the page's own script can answer a
    `describe` posted while the document is still parsing before the probe has run; the fixture's
    ``window.mockreview?.`` guard (docs/decisions-format.md tells pages to write it that way) then sends
    ``target: undefined``. A message is the only way into the opaque frame, so the wait re-posts instead of
    sleeping, and an answer that actually carries a target is the proof the probe is there. It shares the normal
    ``until`` budget with every other wait in this file.
    """
    return b.until("(function(){try{document.querySelector('.proto iframe').contentWindow"
                   ".postMessage({fixtureCommand:'describe'},'*')}catch(e){}"
                   "return window.__fixture && window.__fixture.fixture==='described'"
                   " && window.__fixture.target !== undefined})()")


def test_opaque_bundled_probe_anchors_and_api(server, env, tmp_path, chrome):
    path = tmp_path / 'prototype.html'
    path.write_text('''<!doctype html><html><head><style>body{margin:0;min-height:600px}#copy{position:absolute;left:40px;top:80px;width:240px;height:60px}</style></head><body>
    <h2>Prototype</h2><p data-mr-part="copy" id="copy">Review this selected text</p>
    <input id="check" type="checkbox" style="position:absolute;left:10px;top:10px">
    <form id="form"><input id="field"><button type="submit" id="submit">Submit</button></form>
    <script>
    let removed=null;addEventListener('error',e=>parent.postMessage({fixtureError:e.message},'*'));
    document.querySelector('#submit').addEventListener('click',e=>parent.postMessage({fixture:'form-button',prevented:e.defaultPrevented},'*'));
    document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();parent.postMessage({fixture:'submitted'},'*')});
    addEventListener('message',e=>{
      let cmd=e.data.fixtureCommand, el=document.querySelector('#copy');
      if(cmd==='move') Object.assign(el.style,{left:'140px',width:'320px'});
      if(cmd==='remove'){removed=el;el.remove()}
      if(cmd==='restore') document.body.appendChild(removed);
      if(cmd==='range'){let r=document.createRange();r.setStart(el.firstChild,0);r.setEnd(el.firstChild,11);window.mockreview?.comments.openComposer({range:r})}
      if(cmd==='describe') parent.postMessage({fixture:'described',target:window.mockreview?.comments.anchorFor(el),copyLeft:el?.getBoundingClientRect().left,checked:document.querySelector('#check').checked,submitPoint:(()=>{let r=document.querySelector('#submit').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()},'*');
    });
    </script></body></html>''', encoding="utf-8")
    spec = store.parse_round_spec(prototype=str(path))
    data = store.create_review('Prototype anchors', '', ORIGIN, 'local', spec, wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait="!!document.querySelector('.proto iframe')")
    b.js("window.__positions=[];addEventListener('message',e=>{if(e.source!==document.querySelector('.proto iframe').contentWindow)return;if(e.data.fixtureError)window.__error=e.data.fixtureError;if(e.data.type==='positions')window.__positions=e.data.pins;if(e.data.fixture)window.__fixture=e.data})")
    def command(cmd):
        b.js("document.querySelector('.proto iframe').contentWindow.postMessage({fixtureCommand:%s},'*')" % json.dumps(cmd))
    assert describe_target(b), b.js('JSON.stringify({error:window.__error,fixture:window.__fixture})')
    assert b.js('window.__fixture.target.part') == 'copy'
    assert b.js("(()=>{try{return !!document.querySelector('.proto iframe').contentWindow.document}catch(_){return false}})()") is False
    on(b)
    frame = json.loads(b.js("JSON.stringify((()=>{let r=document.querySelector('.proto iframe').getBoundingClientRect();return [r.left,r.top]})())"))
    b.click(frame[0] + 16, frame[1] + 16)
    command('describe'); assert b.until('window.__fixture.checked===true')
    submit_point = b.js('window.__fixture.submitPoint')
    b.click(frame[0] + submit_point[0], frame[1] + submit_point[1])
    assert b.until("window.__fixture?.fixture==='form-button'")
    assert b.js('window.__fixture.prevented') is False  # sandbox still blocks form navigation, as before E2
    b.click(frame[0] + 100, frame[1] + 110)
    assert b.until("document.querySelector('.popover .pop-where')?.textContent.startsWith('on Prototype')")
    save(b)
    assert json.loads(b.js(f'JSON.stringify({DRAFT})'))['pins'][0]['target']['part'] == 'copy'
    assert b.until('window.__positions[0]?.i===0 && window.__positions[0]?.x > 0')
    initial = b.js('window.__positions[0].x')
    command('move');command('describe')
    assert b.until(f'window.__positions[0]?.x > {initial} + .03'), b.js('JSON.stringify({error:window.__error,fixture:window.__fixture,positions:window.__positions})')
    command('remove'); assert b.until('window.__positions[0]?.detached === true')
    command('restore'); assert b.until('window.__positions[0]?.detached === false')
    command('range'); assert b.until("!!document.querySelector('.popover')"); save(b)
    assert b.until(f'({DRAFT}).pins.length===2')
    assert json.loads(b.js(f'JSON.stringify({DRAFT})'))['pins'][1]['target']['quote']['exact'] == 'Review this'
    # A forged message from the parent window must not be accepted as a probe event.
    b.js("window.postMessage({__mockreview:1,type:'click',target:{path:'['}},'*')")
    assert not b.popover()


ARTIFACT = '''<!doctype html><html><head><style>body{margin:0;min-height:600px}#copy{position:absolute;left:40px;top:80px;width:240px;height:60px}</style></head><body>
<h2>Artifact</h2><p data-mr-part="copy" id="copy">Review this selected text</p>
<script>
addEventListener('error',e=>parent.postMessage({fixtureError:e.message},'*'));
addEventListener('mousemove',e=>parent.postMessage({fixture:'hover',target:e.target?.id||'',x:e.clientX,y:e.clientY},'*'));
addEventListener('message',e=>{
  if(e.data.fixtureCommand!=='describe') return;
  let el=document.querySelector('#copy');
  parent.postMessage({fixture:'described',target:window.mockreview?.comments.anchorFor(el),
    commentMode:document.documentElement.classList.contains('mr-comment'),
    point:(()=>{let r=el.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()},'*');
});
</script></body></html>'''

HIJACK = '''<!doctype html><html><head><style>body{margin:0;min-height:600px}#copy{position:absolute;left:40px;top:80px;width:240px;height:60px}</style></head><body>
<img name="body" alt="" width="1" height="1" style="position:absolute;left:0;top:0">
<h2>Hijack</h2><p data-mr-part="copy" id="copy">Review this hijacked copy</p>
<script>
Element.prototype.getAttribute = function () { return 'HIJACKED'; };
Object.defineProperty(Node.prototype, 'textContent', {get(){ return 'HIJACKED-TEXT'; }});
addEventListener('error', e => parent.postMessage({fixtureError: e.message}, '*'));
addEventListener('message', e => {
  if (e.data.fixtureCommand === 'state') parent.postMessage({fixture: 'state',
    comment: document.documentElement.classList.contains('mr-comment')}, '*');
  if (e.data.fixtureCommand === 'click') {
    let el = document.querySelector('#copy'), r = el.getBoundingClientRect();
    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: r.left + 5, clientY: r.top + 5}));
  }
  if (e.data.fixtureCommand === 'resolve') parent.postMessage({fixture: 'resolved',
    ok: !!document.querySelector(e.data.path)}, '*');
});
</script></body></html>'''


CLOBBER = ('<section><h2>Clobber</h2>'
           '<p data-mr-part="copy" id="copy" style="width:260px;height:40px;margin:20px">Before the form</p>'
           '<form id="clobber" tabindex="0" style="display:block;box-sizing:border-box;width:260px;height:160px;margin:20px;padding:8px">'
           '<input name="matches" style="display:block;height:16px"><input id="getAttribute" style="display:block;height:16px">'
           '<input name="contains" style="display:block;height:16px"><input name="closest" style="display:none">'
           '<input name="getBoundingClientRect" style="display:none"><input name="username" style="display:none"></form>'
           '<p data-mr-part="after" id="after" style="width:260px;height:40px;margin:20px">After the form</p></section>')


@pytest.fixture
def clobber_review(env, tmp_path):
    path = tmp_path / 'clobber.json'
    path.write_text(json.dumps({"blocks": [{"type": "mock", "id": "m1", "html": CLOBBER}]}), encoding="utf-8")
    data = store.create_review('Clobber', '', ORIGIN, 'local', store.parse_round_spec(decisions=str(path)), wake=False)
    return data['id']


def test_a_mock_form_that_shadows_dom_methods_loads_and_records_targets(server, clobber_review, chrome):
    b = Board(chrome, f'{server}/r/{clobber_review}/',
              wait="!!document.querySelector('.mock-host')?.shadowRoot?.querySelector('#clobber')")
    b.js("window.__errors=[];addEventListener('error',e=>window.__errors.push(e.message))")
    b.js("import('./anchors.js').then(m=>window.__anchors=m)")
    assert b.until('!!window.__anchors')
    assert not b.js("document.querySelector('.boot.error')")          # no "This round could not be loaded"
    on(b)
    # The form centre is empty form area, not one of its (live) controls. Old code threw here:
    # `el.closest is not a function`, because <input name="closest"> shadows form.closest.
    b.click(*point(b, '#clobber'))
    assert b.until("!!document.querySelector('.popover')")
    save(b)
    first = json.loads(b.js(f'JSON.stringify(({DRAFT}).pins[0].target)'))
    assert first['path'] == '#clobber' and first['sig']['tag'] == 'form'
    b.click(*point(b, '[data-mr-part="after"]'))
    assert b.until("!!document.querySelector('.popover')")
    save(b)
    assert b.until(f'({DRAFT}).pins.length === 2')       # saveDraft is debounced
    pins = json.loads(b.js(f'JSON.stringify(({DRAFT}).pins)'))
    assert pins[1]['target']['path'] != first['path']
    # Keyboard path: a focused non-text form is not "typing", so C must toggle comment mode off,
    # not throw in the document key handler (`typing()` used to call form.closest directly).
    b.js(f"{ROOT}.querySelector('#clobber').focus()")
    assert b.js(f"{ROOT}.activeElement.id") == 'clobber'
    b.key('c', 'KeyC', 67, text='c')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'false'")
    assert b.js('window.__errors') == []


CLOBBER_PAGE = '''<!doctype html><html><head><style>body{margin:0;min-height:600px}#clobber{margin:20px;width:260px;height:160px}</style></head><body>
<h2>Clobber</h2><p data-mr-part="copy" id="copy">Before the form</p>
<form id="clobber"><input name="matches"><input id="getAttribute"><input name="contains"></form>
<p data-mr-part="after" id="after">After the form</p>
<script>
addEventListener('error',e=>parent.postMessage({fixtureError:e.message},'*'));
addEventListener('message',e=>{
  if(e.data.fixtureCommand!=='describe') return;
  let form=document.querySelector('#clobber'), after=document.querySelector('[data-mr-part="after"]');
  parent.postMessage({fixture:'described',form:window.mockreview.comments.anchorFor(form),after:window.mockreview.comments.anchorFor(after)},'*');
});
</script></body></html>'''


def test_probe_page_with_a_clobbering_form_answers_hello_and_anchors(server, env, tmp_path, chrome):
    path = tmp_path / 'clobber.html'
    path.write_text(CLOBBER_PAGE, encoding="utf-8")
    data = store.create_review('Clobber probe', '', ORIGIN, 'local', store.parse_round_spec(prototype=str(path)), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait="!!document.querySelector('.proto iframe')")
    b.js("window.__hello=0;window.__fixture=null;addEventListener('message',e=>{"
         "if(e.source!==document.querySelector('.proto iframe').contentWindow)return;"
         "if(e.data&&e.data.type==='hello')window.__hello++;"
         "if(e.data&&e.data.fixture==='described')window.__fixture=e.data})")
    b.js("document.querySelector('.proto iframe').contentWindow.postMessage({__mockreview:1,cmd:'ping'},'*')")
    assert b.until('window.__hello > 0'), b.js('window.__error')
    b.js("document.querySelector('.proto iframe').contentWindow.postMessage({fixtureCommand:'describe'},'*')")
    assert b.until("window.__fixture?.fixture==='described'"), b.js('window.__error')
    assert b.js('window.__fixture.form.path') and b.js('window.__fixture.after.path')
    assert b.js('window.__fixture.form.path') != b.js('window.__fixture.after.path')


REGISTER = '''<!doctype html><html><head><style>body{margin:0;min-height:600px}#copy{position:absolute;left:40px;top:80px;width:240px;height:60px}</style></head><body>
<h2>Artifact</h2><p data-mr-part="copy" id="copy">Review this selected text</p>
<script>
addEventListener('error',e=>parent.postMessage({fixtureError:e.message},'*'));
window.mockreview.comments.registerAnchors(function(){
  let t=window.mockreview.comments.anchorFor(document.querySelector('#copy'));
  return t?[t]:[];
});
addEventListener('message',e=>{
  if(e.data.fixtureCommand!=='custom') return;
  parent.postMessage({fixture:'custom',anchors:window.mockreview.comments.customAnchors(),frozen:Object.isFrozen(window.mockreview)},'*');
});
</script></body></html>'''


def test_a_page_registers_custom_anchors_through_the_frozen_hook(server, env, tmp_path, chrome):
    path = tmp_path / 'register.json'
    path.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "a1", "title": "Artifact", "markable": True,
                                            "html": REGISTER}]}), encoding='utf-8')
    data = store.create_review('Registration', '', ORIGIN, 'local', store.parse_round_spec(decisions=str(path)), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait="!!document.querySelector('.art-frame')")
    b.js("window.__fixture=null;addEventListener('message',e=>{"
         "if(e.data&&e.data.fixture==='custom')window.__fixture=e.data;"
         "if(e.data&&e.data.fixtureError)window.__error=e.data.fixtureError})")

    def ask():
        # The frame may still be about:blank when the board first paints; post until the page's own listener answers.
        return ("(function(){try{document.querySelector('.art-frame').contentWindow.postMessage({fixtureCommand:'custom'},'*')}"
                "catch(e){}return window.__fixture?.fixture==='custom'})()")

    assert b.until(ask(), timeout=20), b.js('window.__error')
    anchors = b.js('window.__fixture.anchors')
    assert len(anchors) == 1 and anchors[0]['path'] == '[data-mr-part="copy"]'
    assert b.js('window.__fixture.frozen') is True


def test_artifact_frame_anchors_and_records_the_tapped_target(server, env, tmp_path, chrome):
    """E6 hardened the artifact frame's CSP; the anchors must still work inside it, not just on prototypes."""
    path = tmp_path / 'artifact.json'
    path.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "a1", "title": "Artifact", "markable": True,
                                             "html": ARTIFACT}]}), encoding='utf-8')
    data = store.create_review('Artifact anchors', '', ORIGIN, 'local', store.parse_round_spec(decisions=str(path)), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait="!!document.querySelector('.art-frame')")
    b.js("window.__fixture=null;window.__hover=null;addEventListener('message',e=>{"
         "if(e.data&&e.data.fixture==='described')window.__fixture=e.data;"
         "if(e.data&&e.data.fixture==='hover')window.__hover=e.data;"
         "if(e.data&&e.data.fixtureError)window.__error=e.data.fixtureError})")

    def describe(ready=''):
        # The frame may still be about:blank when the board first paints; post until the page's own listener answers.
        return ("(function(){try{document.querySelector('.art-frame').contentWindow.postMessage({fixtureCommand:'describe'},'*')}"
                "catch(e){}return window.__fixture?.fixture==='described'" + ready + "})()")

    assert b.until(describe(), timeout=20), b.js('window.__error')
    assert b.js('window.__fixture.target.part') == 'copy'          # the page's own script sees the comment API
    on(b)
    b.js('window.__fixture=null')
    assert b.until(describe(' && window.__fixture.commentMode === true'), timeout=20)      # the probe took comment mode
    # An auto-height artifact starts at 120px and grows to the probe's hello; a tap below that first box would miss it.
    assert b.until("document.querySelector('.art-frame').getBoundingClientRect().height > 200", timeout=10)
    b.js("document.querySelector('.art-frame').scrollIntoView({block:'center'})")
    point = b.js('window.__fixture.point')                          # a point the probe measured in comment mode
    # A cold compositor can still map a pointer event to the pre-scroll page, so the tap lands on the heading
    # instead of the copy (it still opens a popover, just the wrong one). Wait, bounded, until a real move at the
    # measured point arrives inside the frame at that same point, then tap; never leave a pin from a missed tap.
    landed = None
    for attempt in range(1, 11):
        frame = json.loads(b.js("JSON.stringify((()=>{let r=document.querySelector('.art-frame').getBoundingClientRect();return [r.left,r.top]})())"))
        b.js('window.__hover=null')
        for dx in (-1, 0):                                          # two moves: a same-position move may coalesce
            b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": frame[0] + point[0] + dx,
                                                    "y": frame[1] + point[1], "button": "none"}, session=b.sid)
        landed = b.until("window.__hover?.target === 'copy' && Math.abs(window.__hover.x - %r) <= 1.5 "
                         "&& Math.abs(window.__hover.y - %r) <= 1.5" % (point[0], point[1]), timeout=2)
        if not landed:
            continue
        b.click(frame[0] + point[0], frame[1] + point[1])
        opened = b.until("document.querySelector('.popover .pop-where')?.textContent.startsWith('on Artifact')", timeout=2)
        if opened:
            if attempt > 1:
                print("artifact tap opened the popover on attempt %d" % attempt)
            break
        assert not json.loads(b.js(f'JSON.stringify(({DRAFT})?.pins || [])')), \
            "a missed tap left a stray pin with no popover"
    else:
        opened = None
    assert landed, "a real pointer move never landed on the artifact's copy target in 10 bounded attempts"
    assert opened, "the artifact tap opened no popover in 10 bounded attempts"
    save(b)
    assert json.loads(b.js(f'JSON.stringify({DRAFT})'))['pins'][0]['target']['part'] == 'copy'


def test_a_page_cannot_hijack_the_probe_prototypes_or_shadow_document_body(server, env, tmp_path, chrome):
    """A page may replace Element.prototype.getAttribute, redefine Node.prototype.textContent and plant
    <img name="body"> before the probe boots. The probe must still see the real body, the real attribute value and the
    real text, so the posted click target resolves."""
    path = tmp_path / 'hijack.json'
    path.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "a1", "title": "Hijack", "markable": True,
                                             "html": HIJACK}]}), encoding='utf-8')
    data = store.create_review('Hijack', '', ORIGIN, 'local', store.parse_round_spec(decisions=str(path)), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait="!!document.querySelector('.art-frame')")
    b.js("window.__clicks=[];window.__state=null;window.__resolved=null;addEventListener('message',e=>{"
         "if(e.data&&e.data.type==='click')window.__clicks.push(e.data);"
         "if(e.data&&e.data.fixture==='state')window.__state=e.data;"
         "if(e.data&&e.data.fixture==='resolved')window.__resolved=e.data;"
         "if(e.data&&e.data.fixtureError)window.__error=e.data.fixtureError})")

    def tell(cmd, ready):
        return ("(function(){try{document.querySelector('.art-frame').contentWindow.postMessage({fixtureCommand:%s},'*')}"
                "catch(e){}return %s})()" % (json.dumps(cmd), ready))

    on(b)
    assert b.until(tell('state', "window.__state && window.__state.comment === true"), timeout=20), b.js('window.__error')
    b.js("document.querySelector('.art-frame').contentWindow.postMessage({fixtureCommand:'click'},'*')")
    assert b.until("window.__clicks.length === 1", timeout=10), "the probe posted no click"
    target = json.loads(b.js("JSON.stringify(window.__clicks[0].target)"))
    assert target is not None and target['part'] == 'copy' and target['part'] != 'HIJACKED', target
    assert 'Review this hijacked copy' in target['label'] and 'HIJACKED' not in target['label'], target
    b.js("document.querySelector('.art-frame').contentWindow.postMessage({fixtureCommand:'resolve',path:%s},'*')"
         % json.dumps(target['path']))
    assert b.until("window.__resolved && window.__resolved.ok === true"), target['path']
