"""Helpers for the test_auth_* files: accounts with live sessions, and requests that look like they came from a chosen
peer address. A test server always listens on 127.0.0.1; `peer` replaces the address the handler sees."""

import http.client
import urllib.parse

import pytest

from mockreview import server as srv
from mockreview import users


def raw(method, url, body=None, headers=None):
    """One request that does not follow redirects. Returns (status, headers as a list of pairs, body)."""
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request(method, u.path + ("?" + u.query if u.query else ""), body, headers or {})
        r = conn.getresponse()
        return r.status, r.getheaders(), r.read()
    finally:
        conn.close()


def header(pairs, name):
    """Every value of one response header, joined with newlines (Set-Cookie comes more than once)."""
    return "\n".join(v for k, v in pairs if k.lower() == name.lower())


def token_of(path):
    """The token part of /invite/<t>, /login/<t> or /s/<t>."""
    return path.rsplit("/", 1)[1]


def account(review_id=None, role=None, instance="member", all_reviews=False, name="Member", identity=None):
    """A new account made through a real invite, signed in. Returns redeem()'s dict: user_id, session, csrf."""
    inv = users.create_invite("test", instance, review_id, role, all_reviews)
    return users.redeem("invite", token_of(inv["path"]), name=name, identity=identity)


def login(user_id):
    """Another signed-in session (another device) for an existing account."""
    return users.redeem("login", token_of(users.create_login_link("test", user_id)["path"]))


def signed_in(sess, csrf=True):
    """Headers that carry a session, and (for a POST) its CSRF token in the header too."""
    headers = {"Cookie": "mr_session={}; mr_csrf={}".format(sess["session"], sess["csrf"])}
    if csrf:
        headers["X-CSRF-Token"] = sess["csrf"]
    return headers


@pytest.fixture
def peer(monkeypatch):
    """Set peer["addr"] to the client address the handler should see (default: a public test address)."""
    state = {"addr": "192.0.2.10"}
    monkeypatch.setattr(srv.Handler, "_peer", lambda self: state["addr"])
    return state


@pytest.fixture
def loopback_owner_off(monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")


class Recorder:
    """A stream closer (streams.set_closer) that records what it was asked to close."""

    def __init__(self):
        self.sessions, self.links, self.guests, self.actors = [], [], [], []

    def close_sessions(self, session_ids):
        self.sessions.extend(session_ids)

    def close_link(self, review_id, generation):
        self.links.append((review_id, generation))

    def close_guests(self, review_id):
        self.guests.append(review_id)

    def close_actor(self, actor):
        self.actors.append(actor)
