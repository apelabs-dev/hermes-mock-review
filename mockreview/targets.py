"""Bounded plain anchor metadata and install-time identities. No selector is executed here."""
from __future__ import annotations

from html.parser import HTMLParser
import math
import re
from typing import Any, Optional

# A deliberately small CSS grammar matching finder output: type/id/class/attribute,
# descendant/child combinators and numeric nth-child/of-type. No selector lists or functions.
_IDENT = r'(?:-?(?:[_a-zA-Z\u0080-\uffff]|\\[^\r\n])(?:[-_a-zA-Z0-9\u0080-\uffff]|\\[^\r\n])*|--(?:[-_a-zA-Z0-9\u0080-\uffff]|\\[^\r\n])*)'
_ATTR = r'\[' + _IDENT + r'(?:=(?:"(?:[^"\\\x00-\x1f]|\\[^\r\n])*"|\'(?:[^\'\\\x00-\x1f]|\\[^\r\n])*\'))?\]'
_SIMPLE = re.compile(r'(?:[.#]' + _IDENT + r'|' + _ATTR + r'|:nth-(?:child|of-type)\([1-9][0-9]*\))')
_TYPE = re.compile(_IDENT)


def safe_path(v: Any) -> str:
    if not isinstance(v, str) or not 0 < len(v) <= 300 or re.search(r'[\x00-\x1f\x7f{};`]', v):
        return ''
    # Tokenize left to right. Nested quantified selector regexes can backtrack exponentially
    # on attacker-controlled malformed paths even with the 300-character cap.
    at = 0
    while at < len(v):
        match = _TYPE.match(v, at) if v[at] not in '.#[:' else None
        start = at
        if match:
            at = match.end()
        while at < len(v):
            match = _SIMPLE.match(v, at)
            if not match:
                break
            at = match.end()
        if at == start:
            return ''
        if at == len(v):
            return v
        space = False
        while at < len(v) and v[at] == ' ':
            at += 1
            space = True
        if at < len(v) and v[at] == '>':
            at += 1
            while at < len(v) and v[at] == ' ':
                at += 1
        elif not space:
            return ''
        if at == len(v):
            return ''
    return ''


def _text(v: Any, cap: int) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f]', ' ', v)).strip()[:cap] if isinstance(v, str) else ''


def _number(v: Any, cap: float = 1) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        return round(min(cap, max(0, v)), 6) if math.isfinite(v) else 0.0
    except OverflowError:
        return 0.0


def clean_target(v: Any) -> Optional[dict]:
    if not isinstance(v, dict):
        return None
    path, sig = safe_path(v.get('path')), v.get('sig')
    if not path or not isinstance(sig, dict):
        return None
    tag, h = sig.get('tag'), sig.get('h')
    if not isinstance(tag, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', tag):
        return None
    if not isinstance(h, str) or not re.fullmatch(r'[a-z0-9]{1,40}', h):
        return None
    pin, doc = v.get('pin'), v.get('doc')
    pin = pin if isinstance(pin, dict) else {}
    doc = doc if isinstance(doc, dict) else {}
    out = dict(path=path, x=_number(v.get('x')), y=_number(v.get('y')),
               pin={k: _number(pin.get(k)) for k in ('x', 'y')}, sig=dict(tag=tag, h=h),
               label=_text(v.get('label'), 120), doc={k: _number(doc.get(k), 1000000) for k in ('x', 'y')})
    for key, cap in (('part', 40), ('id', 80)):
        value = _text(v.get(key), cap)
        if value:
            out[key] = value
    q = v.get('quote')
    if isinstance(q, dict) and isinstance(q.get('exact'), str) and 0 < len(q['exact']) <= 160:
        out['quote'] = {'type': 'TextQuoteSelector', 'exact': q['exact'].replace('\x00', ''),
                        'prefix': q.get('prefix', '')[-32:] if isinstance(q.get('prefix', ''), str) else '',
                        'suffix': q.get('suffix', '')[:32] if isinstance(q.get('suffix', ''), str) else ''}
        pos = v.get('position')
        if isinstance(pos, dict) and all(type(pos.get(k)) is int for k in ('start', 'end')):
            if 0 <= pos['start'] < pos['end'] <= 1000000:
                out['position'] = dict(type='TextPositionSelector', start=pos['start'], end=pos['end'])
    return out


def target_markdown(v: Any, quote: bool = True) -> str:
    target = clean_target(v)
    if not target:
        return ''
    bits = []
    for key in ('part', 'label', 'path'):
        if target.get(key):
            bits.append(key + ': ' + target[key].replace('`', ''))
    if quote and target.get('quote'):
        bits.append('quote: ' + _text(target['quote']['exact'], 160).replace('`', ''))
    return '\n   Target — ' + '; '.join(bits)


def stamp_html(html: str) -> str:
    """Add stable local IDs once at install; preserve author markup byte-for-byte otherwise.

    This is identity stamping, not sanitization. The board still strips active content.
    IDs are scoped to each mock/document and persisted with its round, never the source file.
    """
    class Stamper(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.insertions = []
            self.used = set()
            self.offsets = [0]
            self.counter = 0
            for m in re.finditer('\n', html):
                self.offsets.append(m.end())

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get('data-mr-id'):
                self.used.add(attrs['data-mr-id'])
            if tag in {'html', 'body', 'head', 'style', 'script', 'meta', 'link', 'title', 'base'} or 'data-mr-id' in attrs:
                return
            self.counter += 1
            line, col = self.getpos()
            offset = self.offsets[line - 1] + col + len(tag) + 1
            self.insertions.append((offset, self.counter))

        handle_startendtag = handle_starttag

    parser = Stamper()
    parser.feed(html)
    for offset, number in reversed(parser.insertions):
        ident = f'mr-{number}'
        while ident in parser.used:
            ident += '-auto'
        html = html[:offset] + f' data-mr-id="{ident}"' + html[offset:]
    return html


def stamp_decisions(value: Any) -> Any:
    if isinstance(value, list):
        return [stamp_decisions(v) for v in value]
    if isinstance(value, dict):
        return {k: stamp_html(v) if k == 'html' and isinstance(v, str) else stamp_decisions(v) for k, v in value.items()}
    return value
