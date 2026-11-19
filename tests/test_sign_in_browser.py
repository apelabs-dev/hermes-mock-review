"""The reported sign-in bug in a real browser: the sign-in form, a share link opened in the same browser, Sign in.

Fails on 44d89b3: the confirm form posted the shared `mr_csrf` cookie value, the share link's guest session replaced
that cookie (one host-scoped jar, ports don't count), and the POST answered a raw JSON 403 instead of signing in.
"""

import json

import pytest

from mockreview import store, users
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}


def test_sign_in_survives_a_share_link_opened_in_the_same_browser(env, server, chrome, tmp_path, monkeypatch):
    # The browser is a phone or laptop, not this computer: a loopback request would already be the owner.
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    path = tmp_path / "round" / "decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"blocks": [{"type": "recap", "id": "recap", "items": ["one"]}]}), encoding="utf-8")
    data = store.create_review("Sign-in probe", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(path)))
    share = users.share("cli", data["id"], action="create", guest_role="commenter", audience="anyone")
    link = users.create_login_link("cli")

    board = Board(chrome, server + link["path"], wait="!!document.querySelector('form input[name=csrf]')")
    before = board.js("document.cookie")
    assert "mr_csrf=" not in before  # the confirm page sets no shared cookie
    # The same browser now opens a share link in another tab: one cookie jar, so this replaces mr_csrf.
    target = board.cdp.call("Target.createTarget", {"url": server + share["path"]})["targetId"]
    try:
        assert board.until("document.cookie !== %s" % json.dumps(before)), "the share link left mr_csrf alone"
        after = board.js("document.cookie")
        assert "mr_csrf=" in after
        board.press("form button")          # the confirm page that was already open: press Sign in
        assert board.until("location.pathname === '/'"), board.js("location.pathname + ' ' + document.body.innerText.slice(0, 200)")
        assert board.until("document.body.innerText.indexOf('Signed in as Owner') >= 0")
    finally:
        try:
            board.cdp.call("Target.closeTarget", {"targetId": target})
        except Exception:
            pass
