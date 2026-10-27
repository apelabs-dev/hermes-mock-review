"""The mockreview command line. bin/mockreview runs main(); the Hermes plugin mounts the same tree as
`hermes mockreview ...` via add_commands(). Every subcommand prints JSON except `settings` (text unless --json)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import api, config, herenow, notify, owner, service, settings, store
from .store import ReviewError
from . import secrets
from .console import safe_output

DEST = "mr_cmd"      # sub-command dest; distinct from Hermes's own top-level dests
FN = "mr_fn"


def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _look(a) -> Dict[str, Any]:
    return {k: getattr(a, k, None) for k in settings.REVIEW_KEYS}


def cmd_create(a) -> int:
    origin = {"platform": a.platform, "chat_id": a.chat_id}
    if a.thread is not None:
        origin["thread_id"] = a.thread
    out(api.create(a.title, origin,
                   brief=a.brief, mode=a.mode, decisions=a.decisions, prototype=a.prototype,
                   design=a.design, allow_live_demo=a.allow_live_demo, wake=a.wake, password=a.password, **_look(a)))
    return 0


def cmd_add_round(a) -> int:
    out(api.add_round(a.review_id, decisions=a.decisions, prototype=a.prototype, design=a.design,
                      allow_live_demo=a.allow_live_demo, **_look(a)))
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


def cmd_notify_test(a) -> int:
    data = store.load(a.review_id)
    r = store.get_round(data, a.round)
    if r.get("feedback"):
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
            print(f"error: {a.secret_env} is not set in the active Hermes secret scope", file=sys.stderr)
            return 2
    else:
        secret, err = notify.load_secret()
        if not secret:
            print(f"error: {err} (or pass --secret-env VAR)", file=sys.stderr)
            return 2
    ok, msg = notify.post(a.to, payload, secret, f"mockreview-test-{data['id']}-r{r['n']}-{os.getpid()}")
    out({"ok": ok, "to": a.to, "result": msg, "payload": payload})
    return 0 if ok else 1


def cmd_publish(a) -> int:
    with owner.bound(owner.resolve_soft()):
        data = store.load(a.review_id)
        if data["mode"] != "herenow":
            raise ReviewError("only herenow reviews are published")
        data = herenow.publish(a.review_id)
        out({"review_id": a.review_id, "url": store.review_url(data), **herenow.password_result(data)})
    return 0


def cmd_password(a) -> int:
    out(api.password(a.review_id, a.state == "on"))
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


def cmd_poll(a) -> int:
    ids = [a.review_id] if a.review_id else herenow.open_reviews()
    out({rid: herenow.poll_review(rid) for rid in ids})
    return 0


def cmd_serve(a) -> int:
    from . import server
    return server.serve(a.host, a.port, worker=not a.no_worker)


def cmd_design(a) -> int:
    with owner.bound(owner.resolve_soft()):
        if a.design_cmd == "save":
            out({"ok": True, "saved": a.name, "design": store.save_design(a.name, Path(a.dir))})
        elif a.design_cmd == "show":
            d = store.design_dir(a.name)
            out({"name": a.name, "path": str(d), **store.validate_design(d)})
        else:
            out(store.list_designs())
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
    c.add_argument("--mode", choices=("tailscale", "tailnet", "local", "lan", "herenow"), default=None,
                   help="hosting (default: the default_host setting)")
    _round_args(c)
    _bool_flag(c, "wake", "wake", "require a working wake route and wake Hermes on Send (the default)",
               "a board nobody is woken by: no wake-route check, Send stores feedback and notifies no one")
    _bool_flag(c, "password", "password", "password protect this here.now review (default: saved setting)",
               "publish this here.now review without a password")
    c.set_defaults(**{FN: cmd_create})

    c = sub.add_parser("add-round", help="add the next round (--decisions or --prototype)")
    c.add_argument("review_id")
    _round_args(c)
    c.set_defaults(**{FN: cmd_add_round})

    c = sub.add_parser("design", help="per-project design profiles the board wears (colours, font, mock CSS, icon)")
    ds = c.add_subparsers(dest="design_cmd", required=True)
    d = ds.add_parser("save", help="save/refresh a profile from a dir holding design.json (+ mock.css, fonts, icon)")
    d.add_argument("name")
    d.add_argument("dir")
    d = ds.add_parser("show")
    d.add_argument("name")
    ds.add_parser("list")
    c.set_defaults(**{FN: cmd_design})

    c = sub.add_parser("feedback", help="print a round's feedback (JSON + markdown)")
    c.add_argument("review_id")
    c.add_argument("--round", type=int)
    c.add_argument("--json", action="store_true", help="one JSON object (markdown inside)")
    c.set_defaults(**{FN: cmd_feedback})

    c = sub.add_parser("list", help="list open reviews")
    c.add_argument("--all", action="store_true", help="include closed reviews")
    c.set_defaults(**{FN: cmd_list})

    c = sub.add_parser("close", help="close a review (herenow: archive Site Data, delete the site)")
    c.add_argument("review_id")
    c.set_defaults(**{FN: cmd_close})

    c = sub.add_parser("settings", help="print the resolved settings and where each value came from")
    c.add_argument("--json", action="store_true")
    c.set_defaults(**{FN: cmd_settings})

    c = sub.add_parser("housekeeping", help="run the auto-close / prune pass once (the server runs it hourly)")
    c.set_defaults(**{FN: cmd_housekeeping})

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

    c = sub.add_parser("notify-test", help="send the wake-up payload to --to (testing only)")
    c.add_argument("review_id")
    c.add_argument("--to", required=True, help="URL to POST to")
    c.add_argument("--round", type=int)
    c.add_argument("--secret-env", help="Hermes secret name for HMAC signing (default: MOCKREVIEW_WEBHOOK_SECRET)")
    c.set_defaults(**{FN: cmd_notify_test})

    c = sub.add_parser("publish", help="re-publish a herenow review")
    c.add_argument("review_id")
    c.set_defaults(**{FN: cmd_publish})

    c = sub.add_parser("password", help="set or remove an open here.now review's password gate")
    c.add_argument("review_id")
    c.add_argument("state", choices=("on", "off"))
    c.set_defaults(**{FN: cmd_password})

    c = sub.add_parser("poll", help="poll here.now Site Data once (what the server does every poll_seconds)")
    c.add_argument("review_id", nargs="?")
    c.set_defaults(**{FN: cmd_poll})

    c = sub.add_parser("serve", help="run the board server in the foreground until Ctrl+C (works with autostart off; "
                                     "exits 2 if the port is taken)")
    c.add_argument("--host", default=None, help="default: the host setting (127.0.0.1)")
    c.add_argument("--port", type=int, default=None, help="default: the port setting (8790)")
    c.add_argument("--no-worker", action="store_true", help="no retry/poll/housekeeping background thread")
    c.set_defaults(**{FN: cmd_serve})

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
