"""Execute production board functions in Node against a minimal DOM, without dependencies."""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Fragment(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = {'children': []}
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        n = {'tag': tag, 'attrs': attrs, 'children': []}
        self.stack[-1]['children'].append(n)
        if tag not in ('img', 'br', 'hr', 'input', 'meta', 'link', 'base', 'embed'):
            self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].get('tag') == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1]['children'].append({'text': data})


def run_dom(test, html=()):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for executed board DOM tests')
    source = (ROOT / 'mockreview/board/board.js').read_text(encoding='utf-8')
    functions = []
    for name in ['h', 'append', 'safeLink', 'secureLink', 'cleanFragment', 'renderTable', 'mockLive', 'mockClick']:
        start = re.search(r'^  function ' + name + r'\(', source, re.M)
        if start is None:  # Allows the revert proof to execute the old functions too.
            continue
        end = source.find('\n  function ', start.end())
        functions.append(source[start.start():end])
    result = subprocess.run([node, str(ROOT / 'tests/board_dom.js')], input=json.dumps({
        'functions': '\n'.join(functions), 'trees': {h: Fragment(h).root for h in html}, 'test': test}),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
    assert result.returncode == 0, result.stderr


def test_https_retained_and_target_rel_replaced():
    html = '<a href="https://example.test/path?a=1&amp;b=2" target="_self" rel="opener" onclick="bad()" ping="https://example.test/track" download="file">Open</a>'
    run_dom('''
const a = cleanFragment(''' + json.dumps(html) + ''').querySelectorAll('a')[0];
assert.equal(a.getAttribute('href'), 'https://example.test/path?a=1&b=2');
assert.equal(a.getAttribute('target'), '_blank');
assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
for (const attr of ['onclick', 'ping', 'download']) assert.equal(a.getAttribute(attr), null);
assert.equal(a.textContent, 'Open');
''', [html])


@pytest.mark.parametrize('url', [
    '/relative', 'relative', '//example.test/', '#fragment', 'javascript:alert(1)',
    'JaVaScRiPt:alert(1)', 'data:text/html,evil', 'vbscript:evil', 'file:///tmp/test',
    'mailto:test@example.test', 'jav&#x61;script:alert(1)', '&#106;avascript:alert(1)',
    'java&#9;script:alert(1)', 'java&#10;script:alert(1)', ' javascript:alert(1)',
    'https://exa&#9;mple.test/', ' https://example.test/', 'https://example.test/ ',
    'https://example.test/\x7f', 'https:example.test', 'http://',
])
def test_unsafe_links_have_no_navigation_attributes(url):
    html = '<a href="' + url + '" target="named" rel="opener">Open</a>'
    run_dom('''
const a = cleanFragment(''' + json.dumps(html) + ''').querySelectorAll('a')[0];
for (const attr of ['href', 'target', 'rel']) assert.equal(a.getAttribute(attr), null);
''', [html])


def test_table_bare_urls_are_links_and_script_stays_text():
    run_dom('''
const table = renderTable({columns: ['Name', 'Value'], rows: [
 ['Site', 'http://example.test/path'], ['HTML', '<script>alert(1)</script>'],
 ['Prose', 'See https://example.test/'], ['Relative', '//example.test/'], ['Number', 42]
]});
const links = table.querySelectorAll('a');
assert.equal(links.length, 2);
assert.equal(links[0].getAttribute('href'), 'http://example.test/path');
assert.equal(links[0].textContent, 'http://example.test/path');
assert.equal(links[1].getAttribute('href'), 'https://example.test/');
for (const a of links) {
 assert.equal(a.getAttribute('target'), '_blank');
 assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
}
assert.equal(table.querySelectorAll('script').length, 0);
assert.deepEqual(table.querySelectorAll('td').map(c => c.textContent), [
 'http://example.test/path', '<script>alert(1)</script>', 'See https://example.test/', '//example.test/', '42'
]);
''')


def test_table_multiple_embedded_urls_preserve_all_text():
    run_dom('''
const text = 'Read https://example.test/a and http://example.test/b';
const html = '<script>"https://example.test/a"</script> & <b>literal</b>';
const table = renderTable({columns: ['Notes', 'HTML'], rows: [[text, html]]});
const cells = table.querySelectorAll('tbody')[0].querySelectorAll('th,td');
assert.equal(cells[0].textContent, text);
assert.deepEqual(cells[0].querySelectorAll('a').map(a => a.getAttribute('href')),
 ['https://example.test/a', 'http://example.test/b']);
assert.equal(cells[1].textContent, html);
assert.equal(table.querySelectorAll('script,b').length, 0);
assert.equal(cells[1].querySelectorAll('a')[0].getAttribute('href'), 'https://example.test/a');
for (const a of table.querySelectorAll('a')) {
 assert.equal(a.getAttribute('target'), '_blank');
 assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
}
''')


@pytest.mark.parametrize('text, label', [
    ('See https://example.test/a.', 'https://example.test/a'),
    ('See (https://example.test/a), please.', 'https://example.test/a'),
    ('See [https://example.test/a]; please.', 'https://example.test/a'),
    ('See {https://example.test/a}!', 'https://example.test/a'),
    ('See <https://example.test/a>.', 'https://example.test/a'),
    ('See "https://example.test/a".', 'https://example.test/a'),
    ("See 'https://example.test/a'.", 'https://example.test/a'),
    ('See https://example.test/a?!', 'https://example.test/a'),
    ('See (https://example.test/a_(b)).', 'https://example.test/a_(b)'),
    ('See https://example.test/a[b].', 'https://example.test/a[b]'),
    ('See HTTPS://example.test/a?q=1&b=2#part: done.', 'HTTPS://example.test/a?q=1&b=2#part'),
    ('First https://example.test/a, then http://example.test/b.', 'https://example.test/a'),
])
def test_table_url_punctuation_preserves_text(text, label):
    run_dom('''
const text = ''' + json.dumps(text) + ''';
const label = ''' + json.dumps(label) + ''';
const table = renderTable({columns: ['Notes'], rows: [[text]]});
const cell = table.querySelectorAll('tbody')[0].querySelectorAll('th')[0];
const a = cell.querySelectorAll('a')[0];
assert.equal(a.textContent, label);
assert.equal(a.getAttribute('href'), safeLink(label));
assert.equal(a.getAttribute('target'), '_blank');
assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
assert.equal(cell.textContent, text);
''')


@pytest.mark.parametrize('text', [
    'See //example.test/a or /relative.',
    'See javascript:alert(1), data:text/plain,example or mailto:test@example.test.',
    'See javascript:https://example.test/a or data:https://example.test/b.',
    'See //https://example.test/a or ftp://example.test/b.',
    'See https:example.test/a or http://.',
])
def test_table_other_schemes_and_relative_urls_stay_text(text):
    run_dom('''
const text = ''' + json.dumps(text) + ''';
const table = renderTable({columns: ['Notes'], rows: [[text]]});
const cell = table.querySelectorAll('tbody')[0].querySelectorAll('th')[0];
assert.equal(cell.querySelectorAll('a').length, 0);
assert.equal(cell.textContent, text);
''')


def test_shadow_link_click_pins_in_comment_mode_and_opens_without_it():
    source = (ROOT / 'mockreview/board/board.js').read_text(encoding='utf-8')
    assert "mockClick(m, e);" in source
    anchors = (ROOT / 'mockreview/board/anchors.js').read_text(encoding='utf-8')
    live = re.search(r"^export const LIVE = '([^']*)';", anchors, re.M).group(1)
    run_dom('''
global.LIVE = ''' + json.dumps(live) + ''';
let comment = true, pins = 0;
global.S = {get commentMode() { return comment; }};
global.canComment = () => true;
global.tapMock = () => { pins++; };
const a = document.createElement('a'); a.setAttribute('href', 'https://example.test/');
const span = document.createElement('span'); a.appendChild(span);
const host = document.createElement('div');
function click(path, target = host) {   // no path: an event without composedPath, read through e.target
  const e = {target, prevented: false, preventDefault() { this.prevented = true; }};
  if (path) e.composedPath = () => path;
  mockClick({}, e);
  return e;
}
// Comment mode: a click inside a mock link pins and cancels the link; plain content pins too.
let e = click([span, a, host]); assert.equal(pins, 1); assert.equal(e.prevented, true);
e = click([host]); assert.equal(pins, 2);
// A pin marker and anything inside [data-mr-live] never pin and keep their default.
const pin = document.createElement('button'); pin.setAttribute('class', 'mpin');
e = click(null, pin); assert.equal(pins, 2); assert.equal(e.prevented, false);
const keep = document.createElement('div'); keep.setAttribute('data-mr-live', '');
const inside = document.createElement('button'); keep.appendChild(inside);
e = click([inside, keep, host]); assert.equal(pins, 2); assert.equal(e.prevented, false);
// Comment mode off: the link keeps its default navigation and nothing pins.
comment = false;
e = click([span, a, host]); assert.equal(pins, 2); assert.equal(e.prevented, false);
''')


def test_other_href_carriers_and_svg_aliases_are_removed():
    html = '<svg><a href="https://example.test/" xlink:href="data:text/html,evil" target="_self">SVG</a></svg><area href="data:text/html,evil">'
    run_dom('''
const root = cleanFragment(''' + json.dumps(html) + ''');
assert.equal(root.querySelectorAll('a')[0].getAttribute('href'), 'https://example.test/');
assert.equal(root.querySelectorAll('a')[0].getAttribute('xlink:href'), null);
assert.equal(root.querySelectorAll('area')[0].getAttribute('href'), null);
''', [html])


def test_one_comment_toggle_and_no_seg_pickers():
    src = (ROOT / 'mockreview' / 'board' / 'board.js').read_text(encoding='utf-8')
    state = re.search(r'var S = \{(.*?)\n  \};', src, re.S)
    assert state and re.search(r'\bcommentMode:\s*false\b', state.group(1)), \
        'S.commentMode must exist in the state initialiser and default to false'
    for gone in ('mockTool', 'protoTool', "'interact'"):
        assert gone not in src, f'{gone} must no longer appear in board.js'
    load = re.search(r'^  function loadCfg\(st\) \{(.*?)\n  \}', src, re.M | re.S)
    assert load, 'loadCfg not found'
    assert 'interactive_elements' not in load.group(1), 'interactive_elements must not choose the starting mode'
    toggle = re.search(r'^  function commentToggle\([^)]*\) \{(.*?)\n  \}', src, re.M | re.S)
    assert toggle and "'aria-pressed': on ? 'true' : 'false'" in toggle.group(1)
    mode = re.search(r'^  function setCommentMode\(on\) \{(.*?)\n  \}', src, re.M | re.S)
    assert mode and re.search(r"classList\.toggle\('use',\s*!want\)", mode.group(1)), \
        'setCommentMode must keep .mock-host.use off while comment mode is on'


def test_mock_links_receive_pointer_events():
    """Comment mode needs real element hit testing, native controls and text selection."""
    src = (ROOT / 'mockreview' / 'board' / 'board.js').read_text(encoding='utf-8')
    base = re.search(r"var MOCK_BASE = ((?:'[^']*'\s*\+?\s*)+);", src)
    assert base, 'MOCK_BASE not found'
    css = ''.join(re.findall(r"'([^']*)'", base.group(1)))
    assert re.search(r'\.cq\{[^}]*pointer-events:\s*auto', css), 'comment mode must allow element hit testing'
    assert 'user-select:none' not in css
    rule = re.search(r'([^{}]*a\[href\][^{}]*)\{([^}]*)\}', css)
    assert rule and re.search(r'pointer-events:\s*auto', rule.group(2)), 'links in mocks must get pointer-events:auto'
    assert re.search(r':host\(\.use\) \.cq\{pointer-events:\s*auto\}', css), 'Tap must let the mock take pointer events'
