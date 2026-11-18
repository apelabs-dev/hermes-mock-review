"""Server-rendered pages: the sign-in page, the dead-link page, the invite / sign-in link confirmation
forms, the review list and the Settings shell. Markup only, no script except the Settings page's own file.

The sign-in page and the dead-link page never name a review, so they read the same whether or not it exists.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Dict, List, Optional

PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
SETTINGS_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

# Look A: graphite + indigo. Text is 12 / 13 / 14 / 16 / 20 px; controls use --r (10 px), cards --rc (12 px);
# every control is 36 px on desktop and 44 px on a phone.
_STYLE = (
    ":root{--b:#fafafa;--s:#fff;--s2:#f1f1f4;--l:#e2e2e8;--t:#17171c;--m:#666672;--a:#4f46e5;--ai:#fff;"
    "--as:rgba(79,70,229,.10);--ok:#15803d;--oks:rgba(21,128,61,.10);--wn:#a16207;--wns:rgba(161,98,7,.12);"
    "--err:#dc2626;--pin:#5b5bf0;--r:10px;--rc:12px;--sh:0 16px 40px -14px rgba(20,20,40,.28),0 2px 6px rgba(20,20,40,.08);"
    "color-scheme:light dark}"
    "@media(prefers-color-scheme:dark){:root{--b:#0b0b0e;--s:#141418;--s2:#1c1c22;--l:#2a2a33;--t:#ededf2;"
    "--m:#a0a0ac;--a:#8585ff;--ai:#0b0b0e;--as:rgba(133,133,255,.16);--ok:#4ade80;--oks:rgba(74,222,128,.14);"
    "--wn:#fbbf24;--wns:rgba(251,191,36,.14);--err:#f87171;"
    "--sh:0 16px 40px -12px rgba(0,0,0,.7),0 2px 6px rgba(0,0,0,.4)}}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--b);color:var(--t);font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',"
    "Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}"
    ".shell{max-width:640px;margin:0 auto;padding:32px 20px 64px}"
    ".brand{display:flex;align-items:center;gap:8px;margin:0 0 24px;color:inherit;text-decoration:none}"
    ".logo{width:28px;height:28px;border-radius:8px;background:transparent;color:var(--ai);display:grid;"
    "place-items:center;flex:none;overflow:hidden}"
    ".logo svg{display:block;width:100%;height:100%}"
    ".brand b{font-size:14px;font-weight:600}"
    "h1{font-size:20px;line-height:1.25;margin:0 0 12px;font-weight:600;letter-spacing:-.01em}"
    "h2{font-size:13px;font-weight:600;color:var(--m);display:flex;align-items:center;gap:8px;margin:24px 0 8px}"
    "h2 span{font-size:12px;font-weight:500;background:var(--s2);border-radius:999px;padding:1px 7px;color:var(--t)}"
    ".card{background:var(--s);border:1px solid var(--l);border-radius:var(--rc);padding:24px}"
    "p{margin:8px 0;font-size:14px}.muted{color:var(--m);font-size:13px}"
    "code{font-family:ui-monospace,monospace;font-size:13px;background:var(--b);border:1px solid var(--l);"
    "border-radius:5px;padding:1px 5px}"
    "a{color:var(--a);text-decoration:none;font-weight:600}a:hover{text-decoration:underline}"
    "label{display:block;font-size:12px;color:var(--m);margin:12px 0 4px}"
    "input[type=text],input[type=search]{font:inherit;width:100%;box-sizing:border-box;height:36px;padding:0 12px;"
    "border:1px solid var(--l);border-radius:var(--r);background:var(--s);color:var(--t);font-size:13px}"
    "input:focus{outline:none;border-color:var(--a);box-shadow:0 0 0 3px var(--as)}"
    "button{font:inherit;height:36px;min-height:36px;margin-top:14px;padding:0 16px;border-radius:var(--r);"
    "border:1px solid transparent;background:var(--a);color:var(--ai);cursor:pointer;font-weight:600;font-size:13px}"
    "button:hover{filter:brightness(1.05)}"
    ":focus-visible{outline:2px solid var(--a);outline-offset:2px}"
    ".top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 20px}"
    ".top h1{margin:0}.top .link{font-size:13px;font-weight:500}"
    ".search{margin:0 0 8px;position:relative}"
    ".search input{padding-left:36px;height:36px}"
    ".search svg{position:absolute;left:10px;top:10px;width:16px;height:16px;stroke:var(--m);fill:none;"
    "stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}"
    ".list{border:1px solid var(--l);border-radius:var(--rc);background:var(--s);overflow:hidden}"
    ".row{display:flex;align-items:center;gap:12px;min-height:60px;padding:10px 16px;border-top:1px solid var(--l);"
    "color:inherit;text-decoration:none}.row:first-child{border-top:0}.row:hover{background:var(--s2)}"
    ".row-main{min-width:0;flex:1}"
    ".row b{display:block;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".row small{display:block;font-size:12px;color:var(--m);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".badge{display:inline-flex;align-items:center;height:22px;padding:0 8px;border-radius:999px;background:var(--s2);"
    "color:var(--m);font-size:12px;font-weight:500;white-space:nowrap;flex:none}"
    ".badge.ac{background:var(--as);color:var(--a)}.badge.wn{background:var(--wns);color:var(--wn)}"
    ".chev{width:16px;height:16px;stroke:var(--m);fill:none;stroke-width:1.5;stroke-linecap:round;"
    "stroke-linejoin:round;flex:none}"
    ".empty{margin:12px 16px;color:var(--m);font-size:13px}"
    "@media(max-width:600px){.shell{padding:24px 16px 48px}.card{padding:16px}"
    "input[type=text],input[type=search],button{height:44px;min-height:44px}.row{padding:10px 12px}}"
)


# The confirm form's page only: with no-referrer a browser sends `Origin: null` on a form POST, which the server refuses.
# same-origin still keeps the link out of every request to another site.
CONFIRM_REFERRER = "same-origin"


# The one copy of the mark's path data: the board's own favicon file, read once here and inlined into the pages.
_LOGO_SVG = (Path(__file__).with_name("board") / "favicon.svg").read_text(encoding="utf-8").strip()


def _page(title: str, body: str, referrer: str = "no-referrer") -> bytes:
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="referrer" content="{referrer}">'
            f'<meta name="robots" content="noindex,nofollow"><title>{html.escape(title)}</title>'
            '<link rel="icon" href="/_/favicon.svg" type="image/svg+xml">'
            f'<style>{_STYLE}</style>'
            f'<body>{body}</body></html>').encode("utf-8")


def _logo() -> str:
    return '<span class="logo" aria-hidden="true">' + _LOGO_SVG + '</span>'


def _brand() -> str:
    return '<a class="brand" href="/">' + _logo() + '<b>Mock review</b></a>'


def signin(identity: Optional[Dict[str, str]] = None) -> bytes:
    """Shown to anyone not signed in, for every review URL alike."""
    known = ""
    if identity:
        known = ('<p class="muted">Your proxy says you are <strong>' + html.escape(identity.get("subject", "")) +
                 '</strong>, but that identity has no account on this board yet. Open an invite link once to link it.</p>')
    return _page("Sign in · mock-review", (
        '<div class="shell">' + _brand() + '<div class="card"><h1>Sign in to mock-review</h1>'
        '<p>This board has no passwords. To get in, open the invite or share link the board\'s owner sent you.</p>'
        + known +
        '<p class="muted">The owner signs in on the computer that runs the board, or with a one-time link from '
        '<code>hermes mockreview login</code>.</p></div></div>'))


DEAD = _page("Link not valid · mock-review", (
    '<div class="shell">' + _brand() + '<div class="card"><h1>This link is not valid</h1>'
    '<p>It may have expired, been used already, or been replaced. Ask the person who sent it for a new one.</p></div></div>'))

THROTTLED = _page("Too many attempts · mock-review", (
    '<div class="shell">' + _brand() + '<div class="card"><h1>Too many attempts</h1>'
    '<p>Wait a minute, then open the link again.</p></div></div>'))

LINK_FULL = _page("Link full · mock-review", (
    '<div class="shell">' + _brand() + '<div class="card"><h1>This link is full</h1>'
    '<p>This link is full. Ask the owner for a new one.</p></div></div>'))


def confirm(purpose: str, action: str, csrf: str, invite_name: bool) -> bytes:
    """The button that uses an invite or sign-in link. A GET never uses the link (chat apps and mail scanners
    prefetch links); this form's POST does."""
    title = "Join mock-review" if purpose == "invite" else "Sign in to mock-review"
    name = ('<label for="name">Your name, as others will see it</label>'
            '<input type="text" id="name" name="name" maxlength="60" autocomplete="name">') if invite_name else ""
    return _page(title, (
        '<div class="shell">' + _brand() + '<div class="card">'
        f'<h1>{html.escape(title)}</h1>'
        f'<p>{"This invite works once." if purpose == "invite" else "This sign-in link works once."}</p>'
        f'<form method="post" action="{html.escape(action)}">{name}'
        f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
        f'<button type="submit">{"Join" if purpose == "invite" else "Sign in"}</button></form></div></div>'),
        CONFIRM_REFERRER)


_SEARCH_SVG = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 21-4.34-4.34" />'
               '<circle cx="11" cy="11" r="8" /></svg>')
_CHEV_SVG = '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>'


def index(groups: List[Dict[str, object]], who_line: str, settings_link: bool, query: str = "") -> bytes:
    """Home: reviews grouped by what needs the reader (needs you / waiting on Hermes / done)."""
    parts = []
    for group in groups:
        rows = group.get("rows") or []
        label = str(group.get("label") or "")
        if not rows:
            continue
        items = "".join(
            f'<a class="row" href="/r/{html.escape(str(r["id"]))}/">'
            f'<span class="row-main"><b title="{html.escape(str(r["title"]))}">{html.escape(str(r["title"]))}</b>'
            f'<small>{html.escape(str(r["meta"]))}</small></span>'
            f'<span class="badge {html.escape(str(r.get("tone") or ""))}">{html.escape(str(r.get("badge") or ""))}</span>'
            f'{_CHEV_SVG}</a>'
            for r in rows)
        parts.append(f'<h2>{html.escape(label)} <span>{len(rows)}</span></h2><div class="list">{items}</div>')
    if not parts:
        parts.append('<div class="list"><p class="empty">' +
                     ('No reviews match that search.' if query else 'No reviews yet.') + '</p></div>')
    link = '<a class="link" href="/settings">Settings</a>' if settings_link else ""
    return _page("Mock reviews · mock-review", (
        '<div class="shell"><div class="top"><div class="brand" style="margin:0">'
        + _logo() + '<h1>Mock reviews</h1></div>' + link + '</div>'
        f'<p class="muted">{html.escape(who_line)}</p>'
        '<form class="search" method="get" action="/" role="search">' + _SEARCH_SVG +
        f'<input type="search" name="q" value="{html.escape(query)}" placeholder="Search reviews" aria-label="Search reviews">'
        '</form>' + "".join(parts) + '</div>'))


SETTINGS = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">'
            '<meta name="robots" content="noindex,nofollow"><title>Settings · mock-review</title>'
            '<link rel="icon" href="/_/favicon.svg" type="image/svg+xml">'
            '<link rel="stylesheet" href="/_/tw.css"><link rel="stylesheet" href="/_/settings.css"><body><div id="app" class="page">'
            '<p class="muted">Loading settings…</p></div><script type="module" src="/_/settings.js"></script></body></html>'
            ).encode("utf-8")
