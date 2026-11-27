"""The document column is the agent's to shape: the opening block, the closing block and an artifact's own title are
all optional, and a page that asks for nothing around it fills the space between the bar and the footer exactly.
Also the canvas ground (dotted, in the project's colour, moving with the camera), hand versus pin, and one dot per pin."""

import json

import pytest

from mockreview import store
from test_canvas_surface import FRAME, SURFACE, _button, _open as _open_canvas, _rect, _wheel, _zoom
from test_live_round_tool import Board, chrome  # noqa: F401 (fixture)

PAGE = '''<!doctype html><style>html,body{margin:0;height:100%}main{height:100%;background:#e8eefc;font:20px system-ui}</style>
<main><h1>The whole body</h1></main>'''
ORIGIN = {'platform': 'discord', 'chat_id': 'test-chat'}


def _spec(tmp_path, blocks, **top):
    path = tmp_path / 'decisions.json'
    path.write_text(json.dumps(dict(top, blocks=blocks)), encoding='utf-8')
    return store.parse_round_spec(decisions=str(path))


def _art(**kwargs):
    return dict(type='artifact', id='pg', title='A page', html=PAGE, **kwargs)


def _board(tmp_path, server, chrome, spec, width=1440):
    review = store.create_review('Chrome review', '', ORIGIN, 'local', spec, wake=False)
    return Board(chrome, f"{server}/r/{review['id']}/", width=width,
                 wait="!!document.querySelector('.dzbar') && !!document.querySelector('#pg, .doc')")


# ---- the switches, validated -------------------------------------------------------------------------------------


def test_every_block_is_shown_unless_the_round_says_otherwise(tmp_path):
    dec = _spec(tmp_path, [_art()])['decisions']
    assert dec['show_intro'] is True and dec['show_outro'] is True and dec['blocks'][0]['header'] is True


def test_intro_outro_and_header_can_each_be_switched_off(tmp_path):
    dec = _spec(tmp_path, [_art(header=False)], intro=False, outro=False)['decisions']
    assert dec['show_intro'] is False and dec['show_outro'] is False and dec['blocks'][0]['header'] is False
    assert _spec(tmp_path, [_art()], intro='Hello', outro=True)['decisions']['show_intro'] is True


@pytest.mark.parametrize('top, block', [({'intro': True}, {}), ({'intro': 0}, {}), ({'outro': 'no'}, {}),
                                        ({'outro': 1}, {}), ({}, {'header': 'no'}), ({}, {'header': 0})])
def test_a_misspelt_switch_is_an_error_not_a_silent_default(tmp_path, top, block):
    with pytest.raises(store.ReviewError):
        _spec(tmp_path, [_art(**block)], **top)


# ---- the board ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize('display', ['page', 'long', 'canvas'])
@pytest.mark.browser
def test_a_bare_page_has_nothing_around_it(env, server, tmp_path, chrome, display):
    b = _board(tmp_path, server, chrome, _spec(tmp_path, [_art(display=display, header=False)], intro=False, outro=False))
    assert b.until("!!document.querySelector('#dz-pg iframe')")
    for sel in ('.doc-head', '.blk-else', '#dz-end', '#dz-pg h2', '#dz-pg .itext', '.statusline'):
        assert b.js(f"!document.querySelector('{sel}')"), sel
    bar, foot = _rect(b, '.dzbar'), _rect(b, '#dzfoot')
    frame = _rect(b, '.art-canvas' if display == 'canvas' else '#dz-pg iframe')
    assert frame['top'] == pytest.approx(bar['bottom'], abs=1)
    if display != 'long':   # a page or a canvas is the space between the bar and the footer, exactly: no scroll box inside a scroll box
        assert frame['bottom'] == pytest.approx(foot['top'], abs=1)
        assert b.js('document.documentElement.scrollHeight') <= b.js('innerHeight') + 1


@pytest.mark.browser
def test_a_round_that_asks_for_nothing_keeps_the_usual_blocks_and_no_empty_box(env, server, tmp_path, chrome):
    b = _board(tmp_path, server, chrome, _spec(tmp_path, [_art()]))
    assert b.until("!!document.querySelector('#dz-pg iframe')")
    for sel in ('.doc-head', '.blk-else', '#dz-end', '#dz-pg h2'):
        assert b.js(f"!!document.querySelector('{sel}')"), sel
    assert b.js("[...document.querySelectorAll('.statusline')].every(e => e.textContent.trim() !== '')"), \
        'an empty bordered status line is drawn'


@pytest.mark.browser
def test_hand_and_pin_are_one_choice(env, server, tmp_path, chrome):
    b = _open_canvas(tmp_path, server, chrome)
    hand = f'{SURFACE} button[aria-label="Hand tool"]'
    pressed = lambda sel: b.js(f"document.querySelector('{sel}').getAttribute('aria-pressed')")   # noqa: E731
    b.press(hand)
    assert pressed(hand) == 'true'
    b.press('[data-comment-toggle]')
    assert b.until("document.body.classList.contains('tool-comment')", 3)
    assert pressed(hand) == 'false', 'the hand stayed on while pinning'
    b.press(hand)
    assert pressed(hand) == 'true' and b.until("!document.body.classList.contains('tool-comment')", 3), \
        'pinning stayed on while dragging'
    b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'c', bubbles: true}))")   # the C shortcut is the same switch
    assert b.until("document.body.classList.contains('tool-comment')", 3) and pressed(hand) == 'false'


@pytest.mark.browser
def test_the_canvas_ground_is_dotted_tinted_and_follows_the_camera(env, server, tmp_path, chrome):
    b = _open_canvas(tmp_path, server, chrome)
    ground = f"getComputedStyle(document.querySelector('{SURFACE}'))"
    assert 'radial-gradient' in b.js(f'{ground}.backgroundImage')
    bg, base = b.js(f'{ground}.backgroundColor'), b.js("getComputedStyle(document.querySelector('.dzbar')).backgroundColor")
    assert bg != 'rgba(0, 0, 0, 0)' and bg != base, 'the ground is the project colour with a touch of its accent'
    num = lambda expr: float(str(b.js(expr)).replace('px', ''))   # noqa: E731
    left = lambda: num(f'{ground}.backgroundPositionX')   # noqa: E731
    gap = lambda: num(f'{ground}.backgroundSize.split(" ")[0]')   # noqa: E731
    x0, g0 = left(), gap()
    assert g0 == pytest.approx(24 * _rect(b, FRAME)['width'] / 1440, abs=0.05), 'a dot gap is 24 px of page at the camera zoom'
    _wheel(b, 700, 400, dx=60)
    assert b.until(f'Math.abs({ground}.backgroundPositionX.replace("px","") - {x0}) > 20', 3), 'the dots stayed put while panning'
    assert left() == pytest.approx(_rect(b, FRAME)['left'], abs=1.5), 'the dots are not locked to the world'
    _zoom(b, 0.2)
    assert b.js(f"document.querySelector('{SURFACE}').style.getPropertyValue('--cv-a')") == '0%', 'dots are noise this far out'
    _zoom(b, 1)
    assert b.js(f"document.querySelector('{SURFACE}').style.getPropertyValue('--cv-a')") == '24%'


@pytest.mark.browser
def test_the_working_dot_and_the_unread_dot_are_the_same_dot(env, server, tmp_path, chrome):
    b = _open_canvas(tmp_path, server, chrome)
    b.js("""(() => { const doc = document.querySelector('.doc');
      for (const [cls, state] of [['un', ''], ['', 'working'], ['un', 'working']]) {
        const pin = document.createElement('button'); pin.className = 'mpin ' + cls; pin.dataset.test = cls + state;
        if (state) { pin.dataset.hermesState = state; const i = document.createElement('span'); i.className = 'hermes-pin-indicator'; pin.append(i); }
        doc.append(pin); } })()""")
    props = "['right', 'top', 'marginTop', 'width', 'height', 'borderTopWidth', 'backgroundColor']"
    unread = json.loads(b.js("JSON.stringify((s => " + props + ".map(p => s[p]))(getComputedStyle(document.querySelector('[data-test=\"un\"]'), '::after')))"))
    working = json.loads(b.js("JSON.stringify((s => " + props + ".map(p => s[p]))(getComputedStyle(document.querySelector('[data-test=\"working\"] .hermes-pin-indicator'))))"))
    assert unread == working, (unread, working)
    assert b.js("getComputedStyle(document.querySelector('[data-test=\"unworking\"]'), '::after').display") == 'none', \
        'a pin that is both unread and being worked on shows one dot'
