"""The mockreview command line. bin/mockreview runs main(); the Hermes plugin mounts the same tree as
`hermes mockreview ...` via add_commands(). Every subcommand prints JSON except `settings` (text unless --json)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import api, config, notify, owner, service, settings, store
from .store import ReviewError
from . import secrets
from .console import safe_output

DEST = "mr_cmd"      # sub-command dest; distinct from Hermes's own top-level dests
FN = "mr_fn"


def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _look(a) -> Dict[str, Any]:
    return {k: getattr(a, k, None) for k in settings.REVIEW_KEYS}


def _brand(a) -> Dict[str, Any]:
    """project (default: the current directory), no_design and off_brand of a create / add-round call."""
    return {"project": os.path.abspath(a.project) if a.project else os.getcwd(), "no_design": a.no_design,
            "off_brand": a.off_brand}


def cmd_create(a) -> int:
    origin = {"platform": a.platform, "chat_id": a.chat_id}
    if a.thread is not None:
        origin["thread_id"] = a.thread
    out(api.create(a.title, origin,
                   brief=a.brief, mode=a.mode, decisions=a.decisions, prototype=a.prototype,
                   design=a.design, allow_live_demo=a.allow_live_demo, wake=a.wake,
                   **_brand(a), **_look(a)))
    return 0


def cmd_add_round(a) -> int:
    out(api.add_round(a.review_id, decisions=a.decisions, prototype=a.prototype, design=a.design,
                      allow_live_demo=a.allow_live_demo, allow_reask=a.allow_reask, **_brand(a), **_look(a)))
    return 0


def cmd_feedback(a) -> int:
    obj = api.get_feedback(a.review_id, a.round)
    md = obj.pop("markdown")
    if a.json:
        out({**obj, "markdown": md})
    else:
        print("```json")
        out(obj)
        print("```\n")
        print(md)
    return 0


def cmd_list(a) -> int:
    out(api.list_reviews(include_closed=a.all))
    return 0


def cmd_close(a) -> int:
    out(api.close(a.review_id))
    return 0


def cmd_ack(a) -> int:
    out(api.ack(a.review_id, a.round))
    return 0


def cmd_notify_test(a) -> int:
    data = store.load(a.review_id)
    r = store.get_round(data, a.round)
    if r.get("feedback") and not store.guest_submission(r):
        payload = service.build_payload(data, r)
    else:
        payload = {"event_type": "review.submitted", "review_id": data["id"], "round": r["n"],
                   **store.normalize_origin(data.get("origin")), "title": data["title"],
                   "feedback_path": service.feedback_path(data["id"], r["n"]),
                   "summary": "- (test) no feedback submitted yet", "review_url": store.review_url(data),
                   "mode": data["mode"], "amended": False, "test": True}
    if a.secret_env:
        secret = secrets.get_secret(a.secret_env)
        if not secret:
            print(f"error: {a.secret_env} is not set (Hermes secret scope or environment)", file=sys.stderr)
            return 2
    else:
        secret, err = notify.load_secret()
        if not secret:
            print(f"error: {err} (or pass --secret-env VAR)", file=sys.stderr)
            return 2
    ok, msg = notify.post(a.to, payload, secret, f"mockreview-test-{data['id']}-r{r['n']}-{os.getpid()}")
    out({"ok": ok, "to": a.to, "result": msg, "payload": payload})
    return 0 if ok else 1


def cmd_migrate(a) -> int:
    from . import migrate
    out(migrate.run(a.mode, dry_run=a.dry_run))
    return 0


def cmd_owner(a) -> int:
    """Who owns storage and wake-ups for this process, and would a create pass preflight? Sends nothing."""
    state, info, why = owner.probe()
    try:
        ctx = owner.resolve()
        res: Dict[str, Any] = {"ok": True, "context": ctx.as_dict()}
        if not ctx.wake.get("ready"):
            res["ok"] = False
    except ReviewError as e:
        res = {"ok": False, "error": e.message}
    res["server"] = {"state": state, "info": info, "why": why or None}
    res["own_store"] = str(config.own_data_root())
    out(res)
    return 0 if res["ok"] else 1


def cmd_adopt(a) -> int:
    out(owner.adopt(a.source, a.backup_dir, note=a.note, target=a.to, dry_run=a.dry_run))
    return 0


def cmd_serve(a) -> int:
    from . import server
    return server.serve(a.host, a.port, worker=not a.no_worker)


def cmd_mcp(a) -> int:
    from . import mcp
    return mcp.serve(sys.stdin, sys.stdout, mcp.Options(
        platform=a.platform if a.platform is not None else (os.environ.get("MOCKREVIEW_PLATFORM") or None),
        chat_id=a.chat_id if a.chat_id is not None else (os.environ.get("MOCKREVIEW_CHAT_ID") or None),
        thread=a.thread if a.thread is not None else (os.environ.get("MOCKREVIEW_THREAD") or None),
        wake=a.wake,
        token=a.token if a.token is not None else (os.environ.get("MOCKREVIEW_TOKEN") or None)))


def cmd_design(a) -> int:
    with owner.bound(owner.resolve_soft()):
        if a.design_cmd == "save":
            out({"ok": True, "saved": a.name, "design": store.save_design(a.name, Path(a.dir))})
        elif a.design_cmd in ("bind", "unbind"):
            fn = store.bind_design if a.design_cmd == "bind" else store.unbind_design
            out({"ok": True, "name": a.name, "projects": fn(a.name, a.path)})
        elif a.design_cmd == "show":
            d = store.design_dir(a.name)
            out({"name": a.name, "path": str(d), **store.validate_design(d)})
        else:
            out(store.list_designs())
    return 0


# ---------------------------------------------------------------- users, sign-in links and access

CLI_ACTOR = "cli"


def _link(path: str, public: bool) -> Dict[str, Any]:
    """Full URLs for a link path: this computer's address, and the public one when the board has one."""
    urls = {"url": config.local_base() + path}
    base = config.public_link_base()
    if base:
        urls["public_url"] = base + path
        if public:
            urls["url"] = urls["public_url"]
    return urls


def _review(review_id: Optional[str]) -> Optional[str]:
    if review_id and not store.exists(review_id):
        raise ReviewError("review not found", 404)
    return review_id


def cmd_login(a) -> int:
    from . import users
    with owner.bound(owner.resolve_soft()):
        link = users.create_login_link(CLI_ACTOR)
        out({"ok": True, "user_id": link["user_id"], **_link(link["path"], public=False),
             "expires_at": link["expires_at"], "note": "works once, for 15 minutes"})
    return 0


def cmd_admin(a) -> int:
    from . import users
    with owner.bound(owner.resolve_soft()):
        uid = users.find_user(a.user)["id"] if a.user else None
        link = users.create_login_link(CLI_ACTOR, uid)
        out({"ok": True, "user_id": link["user_id"], **_link(link["path"], public=True),
             "expires_at": link["expires_at"], "note": "works once, for 15 minutes"})
    return 0


def cmd_users(a) -> int:
    from . import users
    with owner.bound(owner.resolve_soft()):
        if a.users_cmd == "list":
            out(users.list_users())
            return 0
        uid = users.find_user(a.user)["id"]
        if a.users_cmd in ("promote", "demote"):
            out(users.set_role(CLI_ACTOR, uid, "admin" if a.users_cmd == "promote" else "member"))
        elif a.users_cmd == "delete":
            out(users.delete_user(CLI_ACTOR, uid))
        elif a.users_cmd == "grant":
            out(users.grant(CLI_ACTOR, uid, a.role, _review(a.review), a.all_reviews, a.expires))
        else:
            out(users.revoke_grant(CLI_ACTOR, uid, a.review, a.all_reviews))
    return 0


def cmd_invite(a) -> int:
    from . import users
    with owner.bound(owner.resolve_soft()):
        if a.list:
            out(users.list_invites())
            return 0
        if a.revoke is not None:
            out(users.revoke_invite(CLI_ACTOR, a.revoke))
            return 0
        instance = a.role if a.role in users.INSTANCE_ROLES else "member"
        review_role = a.role if a.role in users.REVIEW_ROLES else None
        inv = users.create_invite(CLI_ACTOR, instance, _review(a.review), review_role, a.all_reviews, a.expires,
                                  a.label or "", a.grant_expires)
        path = inv.pop("path")
        out({**inv, **_link(path, public=True), "note": "works once"})
    return 0


def cmd_share(a) -> int:
    from . import users
    with owner.bound(owner.resolve_soft()):
        _review(a.review_id)
        if a.action == "show":
            link = users.share_link(a.review_id)
        else:
            if a.round is not None and a.round != "latest":
                rounds = [r["n"] for r in store.load(a.review_id)["rounds"]]
                if not (a.round.isdecimal() and int(a.round) in rounds):
                    raise ReviewError("round must be latest or one of this review's rounds", 400)
            expires = users.UNSET if a.expires is None else a.expires
            link = users.share(CLI_ACTOR, a.review_id, a.action, guest_role=a.role, expires=expires,
                               audience=a.audience, round=a.round)
        seats = users.seats(a.review_id)
        if link and link.get("path"):
            out({**link, "url": users.share_url(a.review_id, link["path"]), "seats": seats})
        else:
            out({"ok": True, "review_id": a.review_id, "active": False, "seats": seats})
    return 0


def cmd_tokens(a) -> int:
    from . import users
    with owner.bound(owner.resolve_soft()):
        uid = users.find_user(a.user)["id"] if a.user else users.owner_user()["id"]
        if a.tokens_cmd == "list":
            out(users.list_tokens(uid))
        elif a.tokens_cmd == "create":
            out({**users.create_token(uid, a.name), "note": "shown once; only its hash is kept"})
        else:
            out(users.revoke_token(uid, a.id))
    return 0


def settings_text(resolved: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> str:
    """Plain text, one setting per line: name = value  (source). No tables (chat-safe)."""
    lines = [f"mock-review settings (config: {info['config']}, read with {info['reader']})"]
    if info.get("error"):
        lines.append(f"WARNING: config.yaml settings ignored: {info['error']}")
    group = None
    for s in settings.SPEC:
        if s.group != group:
            group = s.group
            lines.append(f"[{group}]")
        r = resolved[s.name]
        v = r["value"]
        shown = ", ".join(v) if isinstance(v, list) else ("" if v is None else str(v).lower() if isinstance(v, bool) else str(v))
        lines.append(f"  {s.name} = {shown or '(empty)'}  ({r['source']})")
        for w in r.get("warnings", []):
            lines.append(f"    ! {w}")
    return "\n".join(lines)


def cmd_settings(a) -> int:
    resolved = settings.resolve_all()
    info = settings.describe()
    if a.json:
        out({"config": info, "settings": resolved})
    else:
        print(settings_text(resolved, info))
    return 0


def cmd_housekeeping(a) -> int:
    out(api.housekeeping())
    return 0


def cmd_public(a) -> int:
    from . import public
    if a.public_cmd == "status":
        out(public.status())
    elif a.public_cmd == "on":
        out(public.enable(a.port))
    else:
        out(public.disable(a.port))
    return 0


def _bool_flag(c, name: str, dest: str, help_on: str, help_off: str) -> None:
    c.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_on)
    c.add_argument(f"--no-{name}", dest=dest, action="store_false", help=help_off)


def _round_args(c) -> None:
    c.add_argument("--decisions", metavar="/abs/decisions.json",
                   help="a decision round: the board renders these decisions + inline mocks itself (the default)")
    c.add_argument("--prototype", metavar="/abs/index.html",
                   help="a clickable prototype that fills the canvas under the board bar (needs offer_live_demo "
                        "or --allow-live-demo)")
    c.add_argument("--design", metavar="NAME|/abs/dir",
                   help="the project's design profile (saved name or a profile dir); add-round inherits it")
    _bool_flag(c, "allow-live-demo", "allow_live_demo", "allow a --prototype round for this call",
               "refuse a --prototype round for this call")
    c.add_argument("--project", metavar="/abs/dir",
                   help="the project this board is for (default: the current directory): a design profile bound to it, "
                        "to its git repository or to the main checkout of its worktree applies on its own")
    c.add_argument("--no-design", dest="no_design", action="store_true", default=False,
                   help="a board that deliberately has no design profile (needed when profiles are saved, none is "
                        "bound to the project and --design is not given)")
    c.add_argument("--off-brand", metavar="REASON", default=None,
                   help="skip the off-brand check of the mocks' colours and fonts against the design profile, and "
                        "record why (1-200 characters)")
    g = c.add_argument_group("per-review look (default: saved settings; stored in the review)")
    g.add_argument("--devices", metavar="desktop,tablet,mobile", help="device widths offered on the board")
    g.add_argument("--default-device", choices=("auto",) + settings.DEVICES)
    g.add_argument("--default-theme", choices=("project", "dark", "light"))
    _bool_flag(g, "theme-toggle", "show_theme_toggle", "show the theme toggle", "hide the theme toggle")
    g.add_argument("--mobile-width", type=int, metavar="PX")
    g.add_argument("--tablet-width", type=int, metavar="PX")
    _bool_flag(g, "pins", "pins_enabled", "allow pins on mocks", "no pins")
    _bool_flag(g, "require-note", "require_note_on_change", "Send needs a note on every Change",
               "Change marks need no note")
    g.add_argument("--max-pins", type=int, metavar="N")
    g.add_argument("--footer-button", choices=("send", "build"),
                   help="the footer button on a round with a gate: Build (default) or Send")
    _bool_flag(g, "interactive", "interactive_elements", "mocks carry working controls",
               "static mocks")

def add_commands(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    sub = p.add_subparsers(dest=DEST, required=True)

    from . import setup   # first, so `--help` leads with the command a new user needs
    c = setup.add_parser(sub)
    c.set_defaults(**{FN: setup.run})

    c = sub.add_parser("create", help="create a review (round 1 = --decisions or --prototype)")
    c.add_argument("--title", required=True)
    c.add_argument("--brief", default="")
    c.add_argument("--platform", required=True, help="origin platform, for example discord or telegram")
    c.add_argument("--chat-id", required=True, help="origin chat identifier on that platform")
    c.add_argument("--thread", help="optional origin thread/topic identifier")
    c.add_argument("--mode", choices=("tailnet", "tailscale", "local", "lan", "public"), default=None,
                   help="hosting (default: the default_host setting)")
    _round_args(c)
    _bool_flag(c, "wake", "wake", "require a working wake route and wake Hermes on Send (the default)",
               "a board nobody is woken by: no wake-route check, Send stores feedback and notifies no one")
    c.set_defaults(**{FN: cmd_create})

    c = sub.add_parser("add-round", help="add the next round (--decisions or --prototype)")
    c.add_argument("review_id")
    _round_args(c)
    c.add_argument("--allow-reask", dest="allow_reask", action="store_true", default=False,
                   help="ask again blocks the last round approved or answered (refused without it: they belong in a "
                        "`settled` block)")
    c.set_defaults(**{FN: cmd_add_round})

    c = sub.add_parser("design", help="per-project design profiles the board wears (colours, font, mock CSS, icon)")
    ds = c.add_subparsers(dest="design_cmd", required=True)
    d = ds.add_parser("save", help="save/refresh a profile from a dir holding design.json (+ mock.css, fonts, icon)")
    d.add_argument("name")
    d.add_argument("dir")
    d = ds.add_parser("show", help="print a saved profile, including the projects bound to it")
    d.add_argument("name")
    ds.add_parser("list")
    d = ds.add_parser("bind", help="bind a project directory to a saved profile, so boards made for it wear the profile")
    d.add_argument("name")
    d.add_argument("path", metavar="/abs/project/dir")
    d = ds.add_parser("unbind", help="remove a project directory from a saved profile")
    d.add_argument("name")
    d.add_argument("path", metavar="/abs/project/dir")
    c.set_defaults(**{FN: cmd_design})

    c = sub.add_parser("feedback", help="print a round's feedback (JSON + markdown)")
    c.add_argument("review_id")
    c.add_argument("--round", type=int)
    c.add_argument("--json", action="store_true", help="one JSON object (markdown inside)")
    c.set_defaults(**{FN: cmd_feedback})

    c = sub.add_parser("ack", help="mark a round's submission as handled: no stalled ping for it")
    c.add_argument("review_id")
    c.add_argument("--round", type=int, help="default: the newest round")
    c.set_defaults(**{FN: cmd_ack})

    c = sub.add_parser("list", help="list open reviews")
    c.add_argument("--all", action="store_true", help="include closed reviews")
    c.set_defaults(**{FN: cmd_list})

    c = sub.add_parser("close", help="close a review: no more rounds or feedback")
    c.add_argument("review_id")
    c.set_defaults(**{FN: cmd_close})

    c = sub.add_parser("settings", help="print the resolved settings and where each value came from")
    c.add_argument("--json", action="store_true")
    c.set_defaults(**{FN: cmd_settings})

    c = sub.add_parser("housekeeping", help="run the auto-close / prune pass once (the server runs it hourly)")
    c.set_defaults(**{FN: cmd_housekeeping})

    c = sub.add_parser("public", help="serve the board on a public HTTPS URL through Tailscale Funnel")
    ps = c.add_subparsers(dest="public_cmd", required=True)
    for name, text in (("status", "show the Funnel mapping for this board"),
                       ("on", "map a Funnel HTTPS port to this board (prints the URL)"),
                       ("off", "remove this board's Funnel mapping")):
        p2 = ps.add_parser(name, help=text)
        p2.add_argument("--port", type=int, default=None,
                        help="HTTPS port: 443, 8443 or 10000 (default: the funnel_port setting, 8443)")
    c.set_defaults(**{FN: cmd_public})

    c = sub.add_parser("owner", help="show who owns storage and wake-ups and whether a create would pass preflight")
    c.set_defaults(**{FN: cmd_owner})

    c = sub.add_parser("adopt", help="move one review from a private store into the watched store (backup, "
                                     "collision checks, nothing lost)")
    c.add_argument("source", help="absolute path of the review directory to move")
    c.add_argument("--backup-dir", required=True, help="where the tar.gz backup of the source goes")
    c.add_argument("--to", help="absolute path of the watched store (default: the running server's store, else "
                                "<Hermes home>/workspace/mock-reviews)")
    c.add_argument("--dry-run", action="store_true", help="validate and check collisions only; write nothing")
    c.add_argument("--note", help="wake note for a pending delivery (default: 'recovered delivery, not a new request')")
    c.set_defaults(**{FN: cmd_adopt})

    c = sub.add_parser("migrate", help="convert reviews from the removed 1.x hosting mode to local or tailnet")
    c.add_argument("--mode", choices=("local", "tailnet"), default=None,
                   help="target hosting (default: derived from default_host)")
    c.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    c.set_defaults(**{FN: cmd_migrate})

    c = sub.add_parser("notify-test", help="send the wake-up payload to --to (testing only)")
    c.add_argument("review_id")
    c.add_argument("--to", required=True, help="URL to POST to")
    c.add_argument("--round", type=int)
    c.add_argument("--secret-env", help="Hermes secret name for HMAC signing (default: MOCKREVIEW_WEBHOOK_SECRET)")
    c.set_defaults(**{FN: cmd_notify_test})

    c = sub.add_parser("serve", help="run the board server in the foreground until Ctrl+C (works with autostart off; "
                                     "exits 2 if the port is taken)")
    c.add_argument("--host", default=None, help="default: the host setting (127.0.0.1)")
    c.add_argument("--port", type=int, default=None, help="default: the port setting (8790)")
    c.add_argument("--no-worker", action="store_true", help="no retry/notice/housekeeping background thread")
    c.set_defaults(**{FN: cmd_serve})

    c = sub.add_parser("login", help="print a one-time link that signs this computer's owner in from another browser")
    c.set_defaults(**{FN: cmd_login})

    c = sub.add_parser("users", help="list people and change their role or access")
    us = c.add_subparsers(dest="users_cmd", required=True)
    us.add_parser("list", help="every account, its role and how it signs in")
    for name, text in (("promote", "make an admin"), ("demote", "make a member (refused for the last admin)"),
                       ("delete", "delete the account and end its sessions (refused for the only admin)")):
        u = us.add_parser(name, help=text)
        u.add_argument("user", help="id, email or exact name")
    for name, text in (("grant", "give access to one review or to all reviews"), ("revoke", "remove that access")):
        u = us.add_parser(name, help=text)
        u.add_argument("user", help="id, email or exact name")
        where = u.add_mutually_exclusive_group(required=True)
        where.add_argument("--review", help="review id")
        where.add_argument("--all-reviews", action="store_true", help="every review, including future ones")
        if name == "grant":
            u.add_argument("--role", choices=("reviewer", "commenter", "viewer"), default="commenter")
            u.add_argument("--expires", help="end the access after this long, for example 30d or 12h")
    c.set_defaults(**{FN: cmd_users})

    c = sub.add_parser("invite", help="print a single-use invite link that creates an account")
    c.add_argument("--review", help="also give access to this review")
    c.add_argument("--all-reviews", action="store_true", help="also give access to every review")
    c.add_argument("--role", choices=("admin", "member", "reviewer", "commenter", "viewer"), default=None,
                   help="admin or member (the default), or the role on --review / --all-reviews (default commenter)")
    c.add_argument("--expires", default=None, help="how long the link works (default 7d)")
    c.add_argument("--grant-expires", default=None, help="end the review access after this long")
    c.add_argument("--label", help="a note for the invite list, for example who it is for")
    c.add_argument("--list", action="store_true", help="list invites instead")
    c.add_argument("--revoke", type=int, metavar="ID", help="revoke an unused invite instead")
    c.set_defaults(**{FN: cmd_invite})

    c = sub.add_parser("admin", help="admin tasks for a person who cannot sign in")
    ad = c.add_subparsers(dest="admin_cmd", required=True)
    d = ad.add_parser("login-link", help="print a one-time sign-in link for an account (default: the owner)")
    d.add_argument("--user", help="id, email or exact name")
    c.set_defaults(**{FN: cmd_admin})

    c = sub.add_parser("share", help="show, create, regenerate or stop a review's guest link")
    c.add_argument("review_id")
    c.add_argument("action", nargs="?", choices=("show", "create", "regenerate", "update", "stop"), default="show",
                   help="regenerate replaces the link and signs out its guests; stop ends it")
    c.add_argument("--role", choices=("reviewer", "commenter", "viewer"), default=None,
                   help="what guests may do (default commenter)")
    c.add_argument("--expires", default=None, help="end the link after this long, for example 7d")
    c.add_argument("--audience", choices=("anyone", "members"), default=None,
                   help="anyone with the link (default) or signed-in people only")
    c.add_argument("--round", default=None, metavar="ROUND",
                   help="what viewer guests see: latest (default) or a round number")
    c.set_defaults(**{FN: cmd_share})

    c = sub.add_parser("tokens", help="personal access tokens (stored hashed)")
    ts = c.add_subparsers(dest="tokens_cmd", required=True)
    for name in ("list", "create", "revoke"):
        t = ts.add_parser(name)
        t.add_argument("--user", help="id, email or exact name (default: the owner)")
        if name == "create":
            t.add_argument("name", help="what the token is for")
        if name == "revoke":
            t.add_argument("id", type=int)
    c.set_defaults(**{FN: cmd_tokens})

    c = sub.add_parser("mcp", help="run the MCP server on stdio (Claude Code, Codex, OpenCode)")
    c.add_argument("--platform", default=None, help="origin platform for wake-ups (default: MOCKREVIEW_PLATFORM)")
    c.add_argument("--chat-id", default=None, help="origin chat ID for wake-ups (default: MOCKREVIEW_CHAT_ID)")
    c.add_argument("--thread", default=None, help="optional origin thread ID (default: MOCKREVIEW_THREAD)")
    c.add_argument("--wake", action="store_true", help="wake the origin chat on Send (default: off)")
    c.add_argument("--token", default=None,
                   help="personal access token for list_comments/reply/resolve (default: MOCKREVIEW_TOKEN)")
    c.set_defaults(**{FN: cmd_mcp})

    return p


def run(a) -> int:
    with safe_output():
        return _run(a)


def _run(a) -> int:
    try:
        return getattr(a, FN)(a)
    except ReviewError as e:
        print(json.dumps({"ok": False, "error": e.message}), file=sys.stderr)
        return 1
    except settings.SettingsError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


def main(argv: Optional[list] = None) -> int:
    with safe_output():
        p = argparse.ArgumentParser(prog="mockreview", description="Review board for Hermes mockups.")
        add_commands(p)
        return run(p.parse_args(argv))
