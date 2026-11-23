"""The artifact block on the board: the frame the board builds (sandbox, no srcdoc), the messages it will and will not
obey (a flood, a forged tap, wild sizes) and the vendored DOMPurify. The board functions are executed from the
production board.js in Node, as in test_board_plan.py."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import http
from mockreview import server as board_server, store

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'mockreview/board/board.js').read_text(encoding='utf-8')
VENDOR = ROOT / 'mockreview/board/vendor'
NAMES = ['h', 'append', 'paras', 'frameUrl', 'roundBase', 'num', 'newPinId', 'uuid', 'pct', 'wlabel', 'pinWhat',
         'artifactBody', 'renderArtifact', 'pushArtifact', 'pushPageState', 'artifactLoaded', 'artifactMessage',
         'isMarkableBlock']

# What the functions above reach for. Every place a message could leave the page is a recorder, so a test can say
# "nothing was sent": the artifact path has no submit, no fetch, nothing but a pin draft in memory.
STUBS = '''
var S = {width: 'desktop', commentMode: true, frames: [], frameTokens: {}, draft: {pins: []}};
var window = {crypto: {randomUUID: function () { return '12345678-1234-1234-1234-123456789abc'; }}};
var crypto = window.crypto;
var CFG = {pins_enabled: true}, LIMIT = {pins: 40}, WLABEL = {desktop: 'Desktop'};
var calls = {pops: [], saves: 0, redraws: 0, toasts: 0, sent: 0};
function isEditable() { return EDITABLE; }
function canComment() { return EDITABLE && pinsOn(); }
function pinVisible(p) { return !p.thread || !(T.gone(p.thread) || (CFG.hide_resolved_pins && T.resolved(p.thread))); }
var T = {mockEntries: function () { return []; }, gone: function () { return false; }, resolved: function () { return false; },
  activityFor: function () {}, activityState: function () { return ''; }};
function pinsOn() { return CFG.pins_enabled; }
function cleanTarget(v) { return v && typeof v === 'object' && typeof v.path === 'string' ? v : null; }
function notePop(o) { calls.pops.push(o); }
function saveDraft() { calls.saves++; }
function redrawPins() { calls.redraws++; }
// A saved pin also posts a thread; thread creation has its own tests, and this harness is about what the
// frame may make the board do, so the seam is stubbed: the draft pin lands, nothing goes over the wire.
function saveNewThread(pin, text) { pin.text = text; S.draft.pins.push(pin); }
function toast() { calls.toasts++; }
function MOCKS_NOOP() {}
var MOCKS = [], drawPins = function () {};
var EDITABLE = true;
global.fetch = function () { calls.sent++; throw new Error('the artifact path must never send'); };
global.XMLHttpRequest = function () { calls.sent++; throw new Error('the artifact path must never send'); };
function frameOf(b) {
  var f = S.frames[0];
  f.iframe.getBoundingClientRect = function () { return {left: 10, top: 20}; };
  f.iframe.contentWindow = {postMessage: function (m) { f.posted = (f.posted || []).concat([m]); }};
  return f;
}
'''


def run(test, tail=''):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for executed board DOM tests')
    parts = [re.search(r'^  var ART = .*$', SRC, re.M).group(0),
             "var NOTE_CHIP = " + re.search(r'^  var NOTE_CHIP = (.*);$', SRC, re.M).group(1) + ";", STUBS]
    for name in NAMES:
        start = re.search(r'^  function ' + name + r'\(', SRC, re.M)
        assert start, name
        end = SRC.find('\n  function ', start.end())
        chunk = SRC[start.start():end]
        parts.append(chunk.split('\n', 1)[0] if name == 'num' else chunk)   # `num` is a one-liner; the message listener follows it
    res = subprocess.run([node, str(ROOT / 'tests/board_dom.js')], input=json.dumps(
        {'functions': '\n'.join(parts), 'trees': {}, 'test': test}), capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=20)
    assert res.returncode == 0, res.stderr


R = "{n: 1}"
B = "{type: 'artifact', id: 'film', title: 'Film', file: 'blocks/film.html', markable: false}"


def test_the_frame_is_sandboxed_to_allow_scripts_only_and_loads_a_served_file():
    run("const r = " + R + ", b = " + B + """;
S.frameTokens = {1: 'tok-abc'};
const el = renderArtifact(r, b);
const fr = el.querySelectorAll('iframe');
assert.equal(fr.length, 1);
assert.equal(fr[0].getAttribute('sandbox'), 'allow-scripts');
assert.equal(fr[0].getAttribute('src'), 'rounds/1/~tok-abc/a/blocks/film.html');
assert.equal(fr[0].getAttribute('srcdoc'), null);
assert.equal(fr[0].getAttribute('referrerpolicy'), 'no-referrer');
assert.ok(!/allow-same-origin|allow-forms|allow-popups|allow-top-navigation/.test(fr[0].getAttribute('sandbox')));
assert.equal(S.frames.length, 1); assert.equal(S.frames[0].ctx, 'artifact');
""")


def test_without_a_token_in_state_the_frame_keeps_todays_url():
    run("const r = " + R + ", b = " + B + """;
delete S.frameTokens;
const el = renderArtifact(r, b);
assert.equal(el.querySelector('iframe').getAttribute('src'), 'rounds/1/a/blocks/film.html');
""")


@pytest.mark.parametrize("file", ["../x.html", "blocks/../../review.json", "http://evil.example.test/x.html", "//evil.example.test/x.html",
                                  "javascript:alert(1)", "blocks/x.HTML", "blocks/a/b.html", "data:text/html,<p>"])
def test_a_file_that_is_not_a_block_page_gets_no_frame(file):
    run("const r = " + R + ", b = {type: 'artifact', id: 'film', file: " + json.dumps(file) + """};
const el = renderArtifact(r, b);
assert.equal(el.querySelectorAll('iframe').length, 0);
assert.ok(el.textContent.includes('no page to show'));
assert.equal(S.frames.length, 0);
""")


def test_title_text_and_caption_are_text_not_markup():
    run("const r = " + R + """, b = {type: 'artifact', id: 'film', file: 'blocks/film.html', title: '<img src=x onerror=alert(1)>',
  text: '<script>alert(1)</script>', caption: '<b>cap</b>'};
const el = renderArtifact(r, b);
assert.equal(el.querySelectorAll('img').length, 0);
assert.equal(el.querySelectorAll('script').length, 0);
assert.equal(el.querySelectorAll('b').length, 0);
assert.ok(el.textContent.includes('<img src=x onerror=alert(1)>') && el.textContent.includes('<b>cap</b>'));
""")


def test_only_a_markable_artifact_is_a_decision():
    run("assert.equal(isMarkableBlock({type: 'artifact', id: 'a', markable: true}), true);"
        "assert.equal(isMarkableBlock({type: 'artifact', id: 'a', markable: false}), false);")


def test_a_fixed_height_is_clamped_and_auto_starts_small():
    run("const r = " + R + """;
let el = renderArtifact(r, {type: 'artifact', id: 'a', file: 'blocks/a.html', height: 99999});
assert.equal(el.querySelector('iframe').style.height, '4000px');
el = renderArtifact(r, {type: 'artifact', id: 'b', file: 'blocks/b.html', height: 'auto'});
assert.equal(el.querySelector('iframe').style.height, '120px');
""")


def test_hello_resizes_within_the_cap_and_a_bounded_number_of_times():
    run("const r = " + R + ", b = " + B + """;
renderArtifact(r, b); const f = frameOf(b);
artifactMessage(f, {type: 'hello', h: 1e12});
assert.equal(f.iframe.style.height, '4000px');
artifactMessage(f, {type: 'hello', h: 'not a number'});
assert.equal(f.iframe.style.height, '80px');
// the page keeps changing its mind: only the first ART.growths changes are followed
for (let i = 0; i < 500; i++) { f.since = Date.now(); f.count = 0; artifactMessage(f, {type: 'hello', h: 200 + i * 3}); }
assert.ok(f.grown <= ART.growths);
assert.ok(parseInt(f.iframe.style.height, 10) <= 200 + ART.growths * 3 + 3);
assert.equal(calls.sent, 0);
""")


def test_a_forged_tap_outside_pin_mode_or_without_edit_rights_opens_nothing():
    run("const r = " + R + ", b = " + B + """;
renderArtifact(r, b); const f = frameOf(b);
S.commentMode = false;
artifactMessage(f, {type: 'click', x: 0.5, y: 0.5, cx: 5, cy: 5});
assert.equal(calls.pops.length, 0);
S.commentMode = true; EDITABLE = false;
artifactMessage(f, {type: 'click', x: 0.5, y: 0.5, cx: 5, cy: 5});
assert.equal(calls.pops.length, 0);
CFG.pins_enabled = false; EDITABLE = true;
artifactMessage(f, {type: 'click', x: 0.5, y: 0.5, cx: 5, cy: 5});
assert.equal(calls.pops.length, 0);
assert.equal(S.draft.pins.length, 0); assert.equal(calls.sent, 0);
""")


def test_a_forged_tap_in_pin_mode_opens_a_draft_that_only_a_person_can_save_and_that_never_submits():
    run("const r = " + R + ", b = " + B + """;
renderArtifact(r, b); const f = frameOf(b);
artifactMessage(f, {type: 'click', x: 7, y: -3, cx: 1e9, cy: 'x'});
assert.equal(calls.pops.length, 1);
assert.equal(S.draft.pins.length, 0, 'a draft is not a pin until the person saves it');
const pop = calls.pops[0];
assert.ok(pop.isNew);
assert.ok(pop.anchor.x <= 10 + 4000 && pop.anchor.y >= 20);
pop.onSave('looks off');
assert.ok(/^p[0-9a-f]{8}$/.test(S.draft.pins[0].id));
delete S.draft.pins[0].id;
assert.deepEqual(S.draft.pins, [{block: 'film', option: null, w: 'desktop', x: 1, y: 0, text: 'looks off'}]);
assert.equal(calls.sent, 0);
""")


def test_a_postmessage_flood_is_ignored_after_the_per_second_budget():
    run("const r = " + R + ", b = " + B + """;
renderArtifact(r, b); const f = frameOf(b);
for (let i = 0; i < 100000; i++) artifactMessage(f, {type: 'click', x: 0.5, y: 0.5, cx: 1, cy: 1});
assert.ok(calls.pops.length <= ART.perSecond, 'opened ' + calls.pops.length);
assert.equal(S.draft.pins.length, 0); assert.equal(calls.sent, 0);
""")


def test_unknown_message_types_and_pins_of_another_block_do_nothing():
    run("const r = " + R + ", b = " + B + """;
renderArtifact(r, b); const f = frameOf(b);
S.draft.pins.push({block: 'other', option: null, w: 'desktop', x: 0.1, y: 0.1, text: 'mine'});
for (const t of ['submit', 'state', 'feedback', 'cmd', undefined]) artifactMessage(f, {type: t, i: 0});
artifactMessage(f, {type: 'pin', i: 0, cx: 1, cy: 1});      // pin 0 belongs to another block
artifactMessage(f, {type: 'pin', i: 99, cx: 1, cy: 1});
assert.equal(calls.pops.length, 0); assert.equal(calls.saves, 0); assert.equal(S.draft.pins.length, 1);
assert.equal(S.draft.pins[0].text, 'mine'); assert.equal(calls.sent, 0);
""")


# A page can navigate its own frame away from the block file. Only the block file carries the probe, so only it answers the
# board's ping; any other document is reset to the block file a few times and then dropped. `reply` is whether the frame's
# document answers like the probe does.
LOAD = """
ART.helloWait = 15;
function boot(reply) {
  renderArtifact(%s, %s); const f = S.frames[0];
  f.iframe.getBoundingClientRect = () => ({left: 0, top: 0});
  f.iframe.contentWindow = {postMessage: function (m) { if (reply() && m.cmd === 'ping') artifactMessage(f, {type: 'hello', h: 300}); }};
  return f;
}
const load = (f) => f.iframe.listeners.load();
""" % (R, B)


def test_a_frame_that_answers_the_ping_is_never_reset_however_often_it_loads():
    run(LOAD + """
const f = boot(() => true);
for (let i = 0; i < 12; i++) load(f);
setTimeout(() => {
  assert.equal(f.resets, 0); assert.equal(f.iframe.src, undefined, 'src untouched');
  assert.equal(f.gone, undefined); assert.equal(S.frames.length, 1);
  assert.equal(f.iframe.style.height, '300px');
}, 80);
""")


def test_a_late_but_real_probe_answer_inside_the_wait_is_not_a_navigation():
    run(LOAD + """
let answer = false;
const f = boot(() => answer);
load(f);
setTimeout(() => { answer = true; artifactMessage(f, {type: 'hello', h: 200}); }, 5);   // a slow machine: still inside helloWait
setTimeout(() => { assert.equal(f.resets, 0); assert.equal(f.iframe.src, undefined); assert.equal(S.frames.length, 1); }, 60);
""")


def test_a_document_that_never_answers_is_reset_to_the_block_file_then_dropped_after_the_cap():
    run("S.frameTokens = {1: 'tok-abc'};\n" + LOAD + """
const f = boot(() => false);
const seen = [];
function step(n) {
  load(f);                                            // the frame loaded something (the page navigated itself)
  setTimeout(() => {
    seen.push(f.iframe.src);
    if (f.gone) {
      assert.equal(f.resets, ART.resets); assert.equal(n, ART.resets + 1);
      assert.deepEqual(seen.slice(0, ART.resets), Array(ART.resets).fill('rounds/1/~tok-abc/a/blocks/film.html'));
      assert.equal(S.frames.length, 0); assert.equal(f.el.querySelectorAll('iframe').length, 0);
      assert.ok(f.el.textContent.includes('did not stay on its own file'));
      return;
    }
    assert.ok(n <= ART.resets, 'resets must stop: ' + n);
    step(n + 1);
  }, 40);
}
step(1);
""")


def test_the_board_source_never_loosens_the_artifact_sandbox():
    body = SRC[SRC.index('  var ART = '):SRC.index('  function firstShown')]
    code = re.sub(r'^\s*//.*$', '', body, flags=re.M)
    assert "sandbox: 'allow-scripts'" in code
    assert 'allow-same-origin' not in code and 'srcdoc' not in code and 'innerHTML' not in code
    # the messages the board reads from an artifact frame, and nothing that submits
    assert set(re.findall(r"d\.type === '(\w+)'", code)) == {'hello', 'click', 'pin', 'data', 'mark'}
    assert not re.search(r'\bfetch\(|XMLHttpRequest|sendBeacon|submit', code)


def test_a_crlf_artifact_page_is_upgraded_to_the_current_probe():
    """A Windows install's write_text turns every \\n into \\r\\n. The serve-time upgrade matches the probe by its
    marker, so a page already written with CRLF gets the current probe in memory and its file is left alone."""
    page = store.artifact_page("<p>Subtotal</p>")
    fresh = page.encode()
    old = page.replace("if (p.hermes) {", "if (false) {", 1)
    assert old != page
    crlf = old.encode().replace(b"\n", b"\r\n")
    before = bytes(crlf)
    upgraded = store.artifact_board_page(crlf)
    assert upgraded is not crlf
    assert crlf == before, "the bytes passed in are never modified"
    assert b"if (false) {" not in upgraded and b"if (p.hermes) {" in upgraded, "the current probe replaces the old one"
    assert upgraded.startswith(fresh[:fresh.index(b"</script>") + len(b"</script>")])
    assert upgraded.endswith(crlf[crlf.index(b"</script>") + len(b"</script>"):])
    assert store.artifact_board_page(fresh) == fresh, "an already-current page is returned unchanged"



# ---------------------------------------------------------------- DOMPurify, vendored

def test_the_vendored_dompurify_is_the_pinned_release():
    js = (VENDOR / 'purify.min.js').read_bytes()
    assert hashlib.sha256(js).hexdigest() == 'cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31'
    assert b'DOMPurify 3.4.17' in js.split(b'\n', 1)[0]
    readme = (VENDOR / 'README.md').read_text(encoding='utf-8')
    assert 'cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31' in readme and 'Apache-2.0 OR MPL-2.0' in readme
    assert 'Apache License' in (VENDOR / 'DOMPURIFY-LICENSE').read_text(encoding='utf-8')


PINNED_VENDOR = {
    'purify.min.js': 'cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31',
    'snapdom.min.js': '3274620b04a65f42f972dc5de97530b8929d7a6e3bbe922130af6263b8da5ab7',
    'turndown.min.js': 'ead805af2012a8434b58f1dab07b452370a7dcd3e9665ff64fc1d61dd2e38e9a',
}


def _attr_rules():
    rules = []
    for raw in (ROOT / '.gitattributes').read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        pattern, _, spec = line.partition(' ')
        attrs = {}
        for item in spec.split():
            if item.startswith('!'):
                attrs[item[1:]] = None
            elif item.startswith('-'):
                attrs[item[1:]] = False
            elif '=' in item:
                name, value = item.split('=', 1)
                attrs[name] = value
            else:
                attrs[item] = True
        rules.append((pattern, attrs))
    return rules


def _attr_matches(pattern, path):
    def segment(text):
        return re.escape(text).replace(r'\*\*', '.*').replace(r'\*', '[^/]*').replace(r'\?', '[^/]')
    regex = '/'.join(segment(part) for part in pattern.split('/'))
    if pattern.startswith('/') or '/' in pattern:
        return re.fullmatch(regex, path) is not None
    return any(re.fullmatch(regex, part) is not None for part in path.split('/'))


def _attr(path, name):
    state = None
    for pattern, attrs in _attr_rules():
        if _attr_matches(pattern, path) and name in attrs:
            state = attrs[name]
    return state


def test_git_never_normalises_the_pinned_vendored_bytes():
    """core.autocrlf=true must not rewrite the vendored bytes: the same sha256 pin is checked here and in
    test_export_routes.py, and a Windows checkout with CRLF would fail both."""
    for name, digest in PINNED_VENDOR.items():
        rel = 'mockreview/board/vendor/' + name
        assert (ROOT / rel).is_file(), rel
        assert _attr(rel, 'text') is False, rel + ' must carry -text in .gitattributes'
        assert _attr(rel, 'eol') is None, rel + ' must not carry an eol rule: byte-exact means untouched'
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest, rel
    served = 'mockreview/board/board.js'
    assert _attr(served, 'text') == 'auto' and _attr(served, 'eol') == 'lf', served


def test_the_board_loads_dompurify_from_itself_before_board_js_with_no_inline_script():
    html = (ROOT / 'mockreview/board/index.html').read_text(encoding='utf-8')
    tags = re.findall(r'<script\b[^>]*>', html)
    assert all(re.search(r'\bsrc="[^"]+"', t) for t in tags), tags      # no inline script: script-src 'self' holds
    srcs = [re.search(r'src="([^"]+)"', t).group(1) for t in tags]
    assert srcs.index('vendor/purify.min.js') < srcs.index('board.js')


def test_the_server_serves_the_vendored_file_and_the_board_csp_is_unchanged(server, review):
    code, h, body = http('GET', f"{server}/r/{review['id']}/vendor/purify.min.js")
    assert code == 200 and h['Content-Type'].startswith(('application/javascript', 'text/javascript'))
    assert hashlib.sha256(body).hexdigest() == 'cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31'
    code, h, _ = http('GET', f"{server}/r/{review['id']}/")
    csp = h['Content-Security-Policy']
    assert csp == board_server.BOARD_CSP and "script-src 'self';" in csp   # the vendored file needed no CSP change


@pytest.mark.parametrize("rel", ["vendor/", "vendor/README.md", "vendor/DOMPURIFY-LICENSE", "vendor/../review.json",
                                 "vendor/%2e%2e/review.json", "vendor/purify.min.js/x"])
def test_only_the_one_vendor_file_is_served(server, review, rel):
    code, _, body = http('GET', f"{server}/r/{review['id']}/{rel}")
    assert code in (400, 404) and b'token' not in body and b'DOMPurify' not in body
