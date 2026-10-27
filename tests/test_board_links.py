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
    for name in ['h', 'append', 'safeLink', 'secureLink', 'cleanFragment', 'renderTable', 'mockClick']:
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
 ['Prose', 'See https://example.test/'], ['Relative', '//example.test/']
]});
const links = table.querySelectorAll('a');
assert.equal(links.length, 1);
assert.equal(links[0].getAttribute('href'), 'http://example.test/path');
assert.equal(links[0].getAttribute('target'), '_blank');
assert.equal(links[0].getAttribute('rel'), 'noopener noreferrer');
assert.equal(links[0].textContent, 'http://example.test/path');
assert.equal(table.querySelectorAll('script').length, 0);
assert.ok(table.textContent.includes('<script>alert(1)</script>'));
''')


def test_shadow_link_click_opens_without_pin_and_other_clicks_still_pin():
    source = (ROOT / 'mockreview/board/board.js').read_text(encoding='utf-8')
    assert "mockClick(m, e);" in source
    run_dom('''
let pins = 0;
global.tapMock = () => { pins++; };
const a = document.createElement('a'); a.setAttribute('href', 'https://example.test/');
const span = document.createElement('span'); a.appendChild(span);
const host = document.createElement('div');
const event = {target: host, composedPath: () => [span, a, host],
 preventDefault: () => { throw Error('link must keep default navigation'); }};
mockClick({}, event); assert.equal(pins, 0);
mockClick({}, {target: host, composedPath: () => [host]}); assert.equal(pins, 1);
const pin = document.createElement('button'); pin.setAttribute('class', 'mpin');
mockClick({}, {target: pin}); assert.equal(pins, 1);
''')


def test_other_href_carriers_and_svg_aliases_are_removed():
    html = '<svg><a href="https://example.test/" xlink:href="data:text/html,evil" target="_self">SVG</a></svg><area href="data:text/html,evil">'
    run_dom('''
const root = cleanFragment(''' + json.dumps(html) + ''');
assert.equal(root.querySelectorAll('a')[0].getAttribute('href'), 'https://example.test/');
assert.equal(root.querySelectorAll('a')[0].getAttribute('xlink:href'), null);
assert.equal(root.querySelectorAll('area')[0].getAttribute('href'), null);
''', [html])
