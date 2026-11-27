"""Native artifact geometry and real input across the sandbox boundary."""

import base64
import json
import math
import os
import time
from pathlib import Path

import pytest

from mockreview import export, store
from test_live_round_tool import Board, chrome  # noqa: F401 (fixture)
from test_long_page_display import DRAFT

SURFACE = '.art-canvas'
FRAME = '#dz-deck iframe'
PAGE = '''<!doctype html><style>
html,body{margin:0;color:#1a2340;background:#eef2fb;font:20px system-ui}
main{position:relative;height:1000px;background:linear-gradient(#fff,#dbe5fa)}
h1{position:absolute;left:40px;top:28px;margin:0;font-size:42px}
#target{position:absolute;top:440px;left:620px;width:200px;height:120px;font:inherit}
</style><main><h1>Layout review</h1><button id="target">Open details</button></main>
<script>
document.querySelector('button').onclick = function () { this.textContent = 'Details opened'; };
window.resizePage = height => { document.querySelector('main').style.height = height + 'px'; };
window.wheelBurst = () => {
  for (let i = 0; i < 500; i++) parent.postMessage({__mockreview:1,type:'canvas-wheel',
    deltaX:1,deltaY:0,clientX:720,clientY:500,ctrlKey:false,metaKey:false}, '*');
};
</script>'''


def _spec(tmp_path, **kwargs):
    p = tmp_path / 'decisions.json'
    p.write_text(json.dumps({'blocks': [dict(type='artifact', id='deck', title='Layout', html=PAGE, **kwargs)]}))
    return store.parse_round_spec(decisions=str(p))


def test_canvas_width_validation(tmp_path):
    for display, width in [('canvas', 8000), ('long', 1920)]:
        block = _spec(tmp_path, display=display, desktop_width=width)['decisions']['blocks'][0]
        assert (block['display'], block['desktop_width']) == (display, width)
    for display in ('frame', 'page', 'long', 'canvas'):
        with pytest.raises(store.ReviewError, match='320-(8000|1920)'):
            _spec(tmp_path, display=display, desktop_width=8001 if display == 'canvas' else 8000)


def test_canvas_exports_use_the_same_untransformed_document_as_long(env, tmp_path):
    """The export is the page at 100%, like a long page: no canvas wrapper, no camera, and no display branch."""
    import inspect
    review = store.create_review('Canvas review', '', {'platform': 'discord', 'chat_id': 'test-chat'}, 'local',
                                 _spec(tmp_path, display='canvas'), wake=False)
    data = store.load(review['id'])
    for csp in (export.EXPORT_CSP, export.EXPORT_SCRIPT_CSP):
        html = export.build_export_html(data, 1, False, csp=csp)
        assert 'mr-artifact-page' in html and 'Open details' in html
        assert 'canvas-world' not in html and 'translate(' not in html
    source = inspect.getsource(export._artifact_figure)
    assert 'display' not in source and 'canvas' not in source


def _open(tmp_path, server, chrome, width=1440, markable=False):
    review = store.create_review('Canvas review', '', {'platform': 'discord', 'chat_id': 'test-chat'}, 'local',
                                 _spec(tmp_path, display='canvas', markable=markable), wake=False)
    b = Board(chrome, f"{server}/r/{review['id']}/", width=width, wait=f"!!document.querySelector('{FRAME}')")
    # Each Board is a new tab; an inactive tab renders no frames, and Chrome's cross-process hit-test data for the
    # transformed iframe then goes stale, so a synthetic click can be dropped. Make this tab active and focused.
    b.cdp.call('Page.bringToFront', session=b.sid)
    b.cdp.call('Emulation.setFocusEmulationEnabled', {'enabled': True}, session=b.sid)
    assert b.until(f"document.querySelector('{FRAME}').offsetHeight === 1000 && !!document.querySelector('{SURFACE}')")
    target = b.frame_target(lambda url: review['id'] in url and url.endswith('/blocks/deck.html'))
    b.frame = b.cdp.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
    b.cdp.call('Page.bringToFront', session=b.sid)
    return b


def _rect(b, sel):
    return json.loads(b.js(f'JSON.stringify(document.querySelector({json.dumps(sel)}).getBoundingClientRect())'))


def _view(b):
    r = _rect(b, FRAME)
    return r['left'], r['top'], r['width'] / 1440


def _wheel(b, x, y, dx=0, dy=0, modifiers=0):
    b.cdp.call('Input.dispatchMouseEvent', dict(type='mouseWheel', x=x, y=y, deltaX=dx, deltaY=dy,
                                               modifiers=modifiers), session=b.sid)


def _wait_zoom(b, zoom):
    assert b.until(f"Math.abs(document.querySelector('{FRAME}').getBoundingClientRect().width/1440 - {zoom}) < 0.001", 3), _view(b)


def _button(b, label):
    b.press(f'{SURFACE} button[aria-label="{label}"]')


def _point(b, x=720, y=500):
    left, top, z = _view(b)
    return round(left + x*z), round(top + y*z)


def _zoom(b, z):
    x, y = _point(b)
    _wheel(b, x, y, dy=-math.log(z/_view(b)[2])/0.0105, modifiers=2)
    _wait_zoom(b, z)
    # CSS geometry updates before Chrome commits the transformed frame's input hit-test data.
    b.cdp.pump(.2)


def _in_frame(b, expr):
    return b.cdp.evaluate(expr, session=b.frame)


def _frame_until(b, expr, timeout=3):
    """The probe learns comment mode from a postMessage: a click sent before it lands is a page click, not a pin."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _in_frame(b, expr):
            return True
        b.cdp.pump(.05)
    return False


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('markable', [False, True])
@pytest.mark.browser
def test_canvas_fills_the_shell_at_both_widths(env, server, tmp_path, chrome, width, markable):
    b = _open(tmp_path, server, chrome, width, markable)
    bar, foot, surface = (_rect(b, s) for s in ('.dzbar', '#dzfoot', SURFACE))
    assert surface['left'] == 0 and surface['width'] == b.js('document.documentElement.clientWidth')
    assert surface['top'] == pytest.approx(bar['bottom'], abs=1)
    assert surface['bottom'] == pytest.approx(foot['top'], abs=1)
    assert bar['top'] == 0 and foot['bottom'] == pytest.approx(b.js('innerHeight'), abs=1)
    before = _view(b)
    _wheel(b, *_point(b), dx=24, dy=30)
    assert b.until(f"document.querySelector('{FRAME}').getBoundingClientRect().left < {before[0] - 20}", 3)
    _wheel(b, *_point(b), dy=-40, modifiers=2)
    assert b.until(f"document.querySelector('{FRAME}').getBoundingClientRect().width > {before[2]*1440 + 10}", 3)
    assert [_rect(b, s) for s in ('.dzbar', '#dzfoot', SURFACE)] == [bar, foot, surface]


@pytest.mark.browser
def test_iframe_wheel_pans_and_zooms_at_pointer_with_clamps(env, server, tmp_path, chrome):
    b = _open(tmp_path, server, chrome)
    left, top, z = _view(b)
    x, y = _point(b, 565, 423)
    assert b.js(f"document.elementFromPoint({x},{y}) === document.querySelector('{FRAME}')")
    _wheel(b, x, y, dx=36, dy=43)
    assert b.until(f"Math.abs(document.querySelector('{FRAME}').getBoundingClientRect().left - ({left}-36)) < 1", 3)
    l2, t2, z2 = _view(b)
    assert (t2, z2) == pytest.approx((top - 43, z), abs=1e-4)
    _wheel(b, x, y, dy=-50, modifiers=2)
    _wait_zoom(b, min(2.5, z * math.exp(0.525)))
    l3, t3, z3 = _view(b)
    assert (l3 + (x-l2)/z2*z3, t3 + (y-t2)/z2*z3) == pytest.approx((x, y), abs=1)
    for dy, zoom, modifier in [(-1000, 2.5, 2), (1000, .15, 4)]:
        _wheel(b, x, y, dy=dy, modifiers=modifier)
        _wait_zoom(b, zoom)


def _drag(b, x, y, button='left'):
    mask = 4 if button == 'middle' else 1
    b.cdp.pump(.15)   # the shield that makes a drag pan is shown this frame, hit-testable the next
    for kind, dx, dy, buttons in [('mousePressed', 0, 0, mask), ('mouseMoved', 80, 45, mask),
                                   ('mouseReleased', 80, 45, 0)]:
        b.cdp.call('Input.dispatchMouseEvent', dict(type=kind, x=x+dx, y=y+dy, button=button,
                                                   buttons=buttons, clickCount=1), session=b.sid)


def _space(b, down):
    b.cdp.call('Input.dispatchKeyEvent', dict(type='keyDown' if down else 'keyUp', key=' ', code='Space',
                                             windowsVirtualKeyCode=32), session=b.sid)


@pytest.mark.browser
def test_zoom_controls_and_iframe_keys_and_drag(env, server, tmp_path, chrome):
    b = _open(tmp_path, server, chrome)
    fit = _view(b)[2]
    _button(b, 'Zoom out')
    assert _view(b)[2] < fit
    _button(b, 'Zoom in')
    _wait_zoom(b, fit)
    _button(b, '100%')
    _wait_zoom(b, 1)
    _button(b, 'Fit')
    _wait_zoom(b, fit)
    b.click(*_point(b))
    assert _in_frame(b, "document.querySelector('button').textContent") == 'Details opened'
    assert b.js(f"document.activeElement === document.querySelector('{FRAME}')")
    for key, predicate in [('+', f'>{fit + .01}'), ('-', f'<{fit + .001}'), ('1', '===1'), ('0', f'<{fit + .001}')]:
        b.key(key)
        assert b.until(f"document.querySelector('{FRAME}').getBoundingClientRect().width/1440 {predicate}", 3)
    for mode in ('space', 'hand', 'middle'):
        before = _view(b)
        if mode == 'space':
            _space(b, True)
            assert b.until("!!document.querySelector('.canvas-shield:not([hidden])')", 3)
        if mode == 'hand':
            _button(b, 'Hand tool')
        _drag(b, *_point(b), button='middle' if mode == 'middle' else 'left')
        assert b.until(f"document.querySelector('{FRAME}').getBoundingClientRect().left > {before[0]+75}", 3), mode
        assert _view(b) == pytest.approx((before[0]+80, before[1]+45, before[2]), abs=1)
        if mode == 'space':
            _space(b, False)
            assert b.until("!document.querySelector('.canvas-shield:not([hidden])')", 3)
        if mode == 'hand':
            _button(b, 'Hand tool')


def _comment_ready(b):
    """The probe learns comment mode from a postMessage: a click sent before it lands is a page click, not a pin."""
    assert _frame_until(b, "document.documentElement.classList.contains('mr-comment')"), 'the page never entered comment mode'
    # Chrome routes input into a cross-process frame from hit-test data that settles a frame late, and the very first
    # pointer event after a transform change is sometimes dropped. A zero-delta wheel is a no-op for the surface and
    # makes the next real click arrive; without it a tap right after the comment toggle went missing.
    x, y = _point(b)
    b.cdp.call('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': x, 'y': y, 'deltaX': 0, 'deltaY': 0}, session=b.sid)
    b.cdp.pump(.25)


def _tap_pin(b, x, y, count):
    b.click(x, y)
    return b.until("!!document.querySelector('.popover textarea')", 4), (count, x, y, b.js(
        f"JSON.stringify([document.elementFromPoint({x},{y})?.tagName, document.querySelector('.popover')?.ariaLabel])"),
        _in_frame(b, "document.documentElement.className"),
        _in_frame(b, "document.querySelector('button') && document.querySelector('button').textContent"))


def _fill_pin(b, count):
    b.js("(t=>{t.value='Check this spot';t.dispatchEvent(new Event('input'))})(document.querySelector('.popover textarea'))")
    b.press('.popover .btn-primary')
    assert b.until(f'({DRAFT})?.pins.length === {count}', 5)
    return json.loads(b.js(f"JSON.stringify(({DRAFT}).pins.at(-1))"))


def _save_pin(b, x, y, count, check_anchor=True):
    _comment_ready(b)
    opened, why = _tap_pin(b, x, y, count)
    assert opened, why
    assert b.until("document.querySelector('.popover').getAnimations().every(a => a.playState !== 'running')", 3)
    if check_anchor:   # at phone width the composer is a bottom sheet, not an anchored popover
        r = _rect(b, '.popover')
        # This fixture keeps the popover away from viewport clamps. Its placement offsets are +18, -28.
        assert (r['left'] - 18, r['top'] + 28) == pytest.approx((x, y), abs=6)
    return _fill_pin(b, count)


def _marker(b, index):
    def search(node):
        attrs = dict(zip(node.get('attributes', [])[::2], node.get('attributes', [])[1::2]))
        if attrs.get('data-i') == str(index) and node.get('nodeName') == 'BUTTON':
            return node['nodeId']
        for child in node.get('children', []) + node.get('shadowRoots', []):
            found = search(child)
            if found:
                return found
        return None

    def read():
        root = b.cdp.call('DOM.getDocument', {'depth': -1, 'pierce': True}, session=b.frame)['root']
        node = search(root)
        if not node:
            return None
        try:
            ref = b.cdp.call('DOM.resolveNode', {'nodeId': node}, session=b.frame)['object']['objectId']
            return b.cdp.call('Runtime.callFunctionOn', {'objectId': ref, 'returnByValue': True,
                              'functionDeclaration': 'function(){return this.getBoundingClientRect().toJSON()}'},
                              session=b.frame)['result'].get('value')
        except Exception as e:
            if 'node' not in str(e).lower():
                raise
            return None  # redraw replaced the button between the two CDP calls

    return read()


@pytest.mark.browser
def test_pins_keep_their_anchor_fractions_and_screen_size_at_zoom(env, server, tmp_path, chrome):
    b = _open(tmp_path, server, chrome)
    b.press('#comment-toggle')
    pins, sizes = [], []
    for i, z in enumerate((.5, 1, 2)):
        _zoom(b, z)
        # Separate taps by a few native pixels so each hits the element, not an existing marker above it.
        pins.append(_save_pin(b, *_point(b, 722-i*2, 498+i*2), i+1))
        assert 'Open details' in pins[-1]['target']['label']
        if b.js("!!document.querySelector('.popover')"):
            b.press('.popover .pop-close, .popover .tp-close')
        b.cdp.pump(.35)
        m = _marker(b, i)
        assert m, (i, z)
        assert m['left'] == pytest.approx(722-i*2, abs=3)
        assert m['bottom'] == pytest.approx(498+i*2, abs=3)
        sizes.append(m['width']*z)
    for p in pins:
        assert p['x'] == pytest.approx(.5, abs=.003)
    for p, q in zip(pins, pins[1:]):
        assert abs(p['x'] - q['x']) <= .003
        assert abs(p['y'] - q['y']) <= .003
    assert max(sizes) <= min(sizes)*1.3, sizes


@pytest.mark.browser
def test_pin_list_pans_far_pin_to_the_middle_without_zooming(env, server, tmp_path, chrome):
    b = _open(tmp_path, server, chrome)
    b.press('#comment-toggle')
    p = _save_pin(b, *_point(b), 1)
    if b.js("!!document.querySelector('.popover')"):
        b.press('.popover .pop-close, .popover .tp-close')
    _wheel(b, *_point(b), dx=1100, dy=1100)
    assert b.until(f"document.querySelector('{FRAME}').getBoundingClientRect().right < 400", 3)
    zoom = _view(b)[2]
    b.press('#pins-deck .pin-row')
    assert b.until("!!document.querySelector('.popover.tpop')", 5)
    s = _rect(b, SURFACE)
    f = _rect(b, FRAME)
    for fraction, axis, length in [(p['x'], 'left', 'width'), (p['y'], 'top', 'height')]:
        assert .2 < (f[axis] + fraction*f[length] - s[axis])/s[length] < .8
    assert _view(b)[2] == pytest.approx(zoom, abs=1e-6)


@pytest.mark.browser
def test_world_follows_growth_and_shrink_without_resetting_view(env, server, tmp_path, chrome):
    b = _open(tmp_path, server, chrome)
    _button(b, '100%')
    before = _view(b)
    for height, expected in [(3400, 3400), (620, 620), (31000, 30000)]:
        _in_frame(b, f'resizePage({height})')
        assert b.until(f"document.querySelector('{FRAME}').offsetHeight === {expected}", 6)
        assert _view(b) == pytest.approx(before, abs=.01)
        assert _rect(b, FRAME)['height'] == pytest.approx(expected, abs=.01)
    b.press('.wseg button[title="Mobile width"]')
    assert b.until(f"document.querySelector('{FRAME}')?.offsetHeight === 1000", 5)
    assert b.js(f"document.querySelector('{FRAME}').offsetWidth") == 1440


@pytest.mark.browser
def test_iframe_wheel_flood_has_its_own_bounded_budget(env, server, tmp_path, chrome):
    b = _open(tmp_path, server, chrome)
    before = _view(b)
    _in_frame(b, 'wheelBurst()')
    assert b.until(f"document.querySelector('{FRAME}').getBoundingClientRect().left < {before[0]-40}", 3)
    b.cdp.pump(.4)
    assert 40 < before[0] - _view(b)[0] <= 180
    _button(b, '100%')
    _wait_zoom(b, 1)


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.browser
def test_canvas_screenshots(env, server, tmp_path, chrome, width):
    path = os.environ.get('CANVAS_SHOTS')
    if not path:
        pytest.skip('set CANVAS_SHOTS to capture the visual review')
    b = _open(tmp_path, server, chrome, width)
    b.press('#comment-toggle-mobile' if width == 390 else '#comment-toggle')
    _comment_ready(b)
    for _ in range(3):   # a headless hit-test can lose the first tap at a fresh fit; the capture is not the assertion here
        opened, _why = _tap_pin(b, *_point(b), 1)
        if opened:
            break
        b.cdp.pump(.2)
    assert opened, _why
    _fill_pin(b, 1)
    if b.js("!!document.querySelector('.popover')"):
        b.press('.popover .pop-close, .popover .tp-close')
    for label, zoom in [('fit', None), ('2x', 2), ('0.3x', .3)]:
        if zoom:
            _zoom(b, zoom)
        b.cdp.call('Input.dispatchMouseEvent', dict(type='mouseMoved', x=24, y=24), session=b.sid)
        b.cdp.pump(.5)
        m = _marker(b, 0)
        assert m, (width, label)
        x, y = _point(b, m['left'] + m['width']/2, m['top'] + m['height']/2)
        assert b.js(f"document.elementFromPoint({x},{y}) === document.querySelector('{FRAME}')")
        data = b.cdp.call('Page.captureScreenshot', {'format': 'png'}, session=b.sid)['data']
        (Path(path) / f'canvas-{width}-{label}.png').write_bytes(base64.b64decode(data))
