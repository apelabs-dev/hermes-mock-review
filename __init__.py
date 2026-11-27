"""mock-review for Hermes: the mock_review_* tools, `hermes mockreview ...`, `/mockreview` and the
mock-review:usage skill.

The handlers call the mockreview package in-process (api.py, the same functions bin/mockreview runs), not the CLI in a
subprocess: results come back as dicts (no stdout to parse), errors are ReviewError messages, and settings are read
through ctx.get_config, so Hermes's own view of config.yaml (profiles, managed scope) is the config layer. The package
is found inside the installed plugin directory.

Settings precedence: a tool argument (per-review override) > MOCKREVIEW_* env var > plugins.entries.mock-review.settings
> built-in default (mockreview/settings.py). Importing and registering start nothing. Optional autostart runs
inside the gateway when the webhook platform connects.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
REPO = PLUGIN_DIR
TOOLSET = "mock-review"


def _mockreview():
    if str(REPO) not in sys.path and (REPO / "mockreview" / "__init__.py").is_file():
        sys.path.insert(0, str(REPO))
    import mockreview.api as api          # noqa: F401  (imports have no side effects)
    import mockreview.cli as cli
    import mockreview.settings as settings
    from mockreview.store import ReviewError
    return api, cli, settings, ReviewError


# ---------------------------------------------------------------- tool schemas (short: they cost tokens every turn)

_DEVICE = {"type": "string", "enum": ["desktop", "tablet", "mobile"]}
LOOK_PROPS: Dict[str, Any] = {
    "devices": {"type": "array", "items": _DEVICE, "description": "Widths offered (default: saved setting)."},
    "default_device": {"type": "string", "enum": ["auto", "desktop", "tablet", "mobile"]},
    "default_theme": {"type": "string", "enum": ["project", "dark", "light"]},
    "show_theme_toggle": {"type": "boolean"},
    "pins_enabled": {"type": "boolean"},
    "require_note_on_change": {"type": "boolean"},
    "max_pins": {"type": "integer"},
    "hide_resolved_pins": {"type": "boolean", "description": "Hide a pin's marker once its thread is resolved."},
    "mobile_width": {"type": "integer"},
    "tablet_width": {"type": "integer"},
}
ROUND_PROPS: Dict[str, Any] = {
    "decisions": {"type": "string", "description": "Absolute path to decisions.json (a decision round)."},
    "prototype": {"type": "string", "description": "Absolute path to a clickable index.html (\"try it live\")."},
    "design": {"type": "string", "description": "Design profile name or absolute dir."},
    "project": {"type": "string", "description": (
        "Absolute path of the project (repo) this board is for. Pass it: the design profile bound to it applies on "
        "its own, in worktrees too. With saved profiles, a create with no design, no binding and no no_design fails; "
        "mocks must use the profile's colours and fonts (var(--accent) ...) or the call fails.")},
    "no_design": {"type": "boolean", "description": (
        "A board that deliberately has no design profile. Needed only when profiles are saved and none applies.")},
    "off_brand": {"type": "string", "maxLength": 200, "description": (
        "Why this round is off-brand on purpose (1-200 characters). Skips the check that mocks use only the "
        "profile's colours and fonts; recorded in the review.")},
}
REASK_PROP: Dict[str, Any] = {"type": "boolean", "description": "Re-ask approved blocks (refused without it)."}


def _live_demo_prop(on: bool) -> Dict[str, Any]:
    return {"type": "boolean", "description": (
        "Allow a prototype round for this call." if on else
        "Live demos are off (offer_live_demo). Only true if the user asked for a live demo.")}


def _mode_prop(ask: bool, host: str) -> Dict[str, Any]:
    return {"type": "string", "enum": ["tailnet", "local", "public"], "description": (
        "Hosting. Ask the user first: tailnet (private), local (this computer's address) or public (an internet URL "
        "behind Tailscale Funnel or any TLS proxy)."
        if ask else
        f"Hosting; omit for the saved default ({host}).")}


def schemas(ask_host: bool, default_host: str, live_demo: bool) -> Dict[str, Dict[str, Any]]:
    return {
        "mock_review_create": {
            "name": "mock_review_create",
            "description": "Create a mock review board (round 1) for the user to mark up. Pass project (repo path).",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"},
                "origin_platform": {"type": "string", "maxLength": 64, "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    "description": "Omit: read from this chat's session. Only to override; never guess."},
                "origin_chat_id": {"type": "string", "minLength": 1, "maxLength": 256,
                    "pattern": "^[A-Za-z0-9_.:@!#+/~=-]{1,256}$",
                    "description": "Omit: read from this chat's session. Pass with origin_platform to override."},
                "origin_thread_id": {"type": "string", "minLength": 1, "maxLength": 256,
                    "pattern": "^[A-Za-z0-9_.:@!#+/~=-]{1,256}$",
                    "description": "Omit: read from this chat's session."},
                "brief": {"type": "string"},
                "mode": _mode_prop(ask_host, default_host),
                **ROUND_PROPS, "allow_live_demo": _live_demo_prop(live_demo), **LOOK_PROPS,
                "wake": {"type": "boolean", "description": "False: demo board nobody is woken by (skips the wake-route check)."}},
                "required": ["title"]},
        },
        "mock_review_add_round": {
            "name": "mock_review_add_round",
            "description": "Add the next round to an open mock review (same link). Pass project (repo path).",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"}, **ROUND_PROPS, "allow_live_demo": _live_demo_prop(live_demo),
                "allow_reask": REASK_PROP,
                **LOOK_PROPS},
                "required": ["review_id"]},
        },
        "mock_review_feedback": {
            "name": "mock_review_feedback",
            "description": "Read a round's feedback as JSON + markdown. action is send or build: follow next_step (build) or next_round (send).",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"}, "round": {"type": "integer", "description": "Default: current."}},
                "required": ["review_id"]},
        },
        "mock_review_ack": {
            "name": "mock_review_ack",
            "description": "Mark a round's feedback as handled (answered here, or no round coming): no stalled ping. Not a substitute for building.",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"}, "round": {"type": "integer", "description": "Default: current."}},
                "required": ["review_id"]},
        },
        "mock_review_list": {
            "name": "mock_review_list",
            "description": "List mock reviews (open only unless include_closed).",
            "parameters": {"type": "object", "properties": {"include_closed": {"type": "boolean"}}},
        },
        "mock_review_patch_block": {
            "name": "mock_review_patch_block",
            "description": "Patch one block's HTML in the current round in place (a pin fix). Pass the round and "
                           "rev you read.",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"},
                "block": {"type": "string"},
                "html": {"type": "string"},
                "option": {"type": "string", "description": "Required for a decision block with options."},
                "round": {"type": "integer",
                          "description": "The round you read; it must still be the newest round."},
                "if_rev": {"type": "integer", "description": "The block's rev from the round you read."},
                "request_ids": {"type": "array", "items": {"type": "string"},
                                "description": "The pin fix request ids this patch closes."}},
                "required": ["review_id", "block", "html", "round", "if_rev"]},
        },
        "mock_review_list_comments": {
            "name": "mock_review_list_comments",
            "description": "List a review's comment threads with their comments (open threads by default).",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "resolved", "all"],
                           "description": "Which threads to list (default: open)."}},
                "required": ["review_id"]},
        },
        "mock_review_reply": {
            "name": "mock_review_reply",
            "description": "Reply in a comment thread as Hermes (author agent:hermes). Never activates @Hermes or wakes anyone.",
            "parameters": {"type": "object", "properties": {
                "thread_id": {"type": "string"},
                "text": {"type": "string", "maxLength": 2000, "description": "Reply text, at most 2000 characters."},
                "request_id": {"type": "string",
                               "description": "The mention wake's request_id this reply answers (closes its chip)."}},
                "required": ["thread_id", "text"]},
        },
        "mock_review_resolve": {
            "name": "mock_review_resolve",
            "description": "Resolve a comment thread (reopen with reopen=true). Never wakes anyone.",
            "parameters": {"type": "object", "properties": {
                "thread_id": {"type": "string"},
                "reopen": {"type": "boolean", "description": "Reopen instead of resolving."},
                "request_id": {"type": "string",
                               "description": "The mention wake's request_id this answer closes."}},
                "required": ["thread_id"]},
        },
    }


# ---------------------------------------------------------------- comment tools (in process; replies post as Hermes)

def _comment_actor():
    from mockreview import users
    user = users.owner_user()
    return user, f"user:{user['id']}"


def _comment_as(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"user_id": user["id"], "name": user["name"], "role": "owner"}


def _comment_as_agent() -> Dict[str, Any]:
    """Who a plugin reply or resolve is attributed to on the board: Hermes, never the owner who holds the pen."""
    from mockreview import threads
    return {"actor": threads.AGENT_ACTOR, "name": threads.AGENT_NAME, "role": "agent"}


def _comment_status(value: Any) -> str:
    from mockreview.store import ReviewError
    status = str(value or "open").strip().lower()
    if status not in ("open", "resolved", "all"):
        raise ReviewError("status must be open, resolved or all", 400)
    return status


def _comment_list(args: Dict[str, Any]) -> Dict[str, Any]:
    """The board's threads as the install's owner sees them. Read-only, so no audit row."""
    from mockreview import owner, store, threads
    from mockreview.store import ReviewError
    review_id = str(args.get("review_id") or "").strip()
    if not review_id:
        raise ReviewError("review_id is required")
    status = _comment_status(args.get("status"))
    with owner.bound(owner.resolve_soft()):
        user, actor = _comment_actor()
        if not store.exists(review_id):
            raise ReviewError("review not found", 404)
        scope = threads.view_scope(review_id, guest=False, role="owner")
        all_threads = threads.list_threads(review_id, principal=actor, include_resolved=True, scope=scope)
        shown = [t for t in all_threads if status == "all" or t["status"] == status]
        return {"review_id": review_id, "status": status, "as": _comment_as(user),
                "counts": {"open": sum(1 for t in all_threads if t["status"] == "open"),
                           "resolved": sum(1 for t in all_threads if t["status"] == "resolved")},
                "rev": threads.rev(review_id, scope), "threads": shown}


def _comment_reply(args: Dict[str, Any]) -> Dict[str, Any]:
    """Reply in a thread as Hermes, never activating it: an agent's answer must not wake an agent.

    The comment is stored under the fixed agent actor (`threads.AGENT_ACTOR`), so nobody sees it as their own; the
    audit row names the real owner account that made the call.

    A `hermes` mention the caller passes is dropped the same way the MCP reply drops it, and `can_activate=False`
    makes the guard independent of the role. The chip request this answer settles closes here.
    """
    from mockreview import db, mcp, owner, service, threads, users
    from mockreview.store import ReviewError
    thread_id = str(args.get("thread_id") or "").strip()
    if not thread_id:
        raise ReviewError("thread_id is required")
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ReviewError("text is required")
    if len(text) > 2000:
        raise ReviewError("text must be at most 2000 characters", 400)
    mentions = mcp._agent_mentions(args.get("mentions"))
    request_id = args.get("request_id")
    request_id = str(request_id) if request_id else None
    with owner.bound(owner.resolve_soft()):
        user, actor = _comment_actor()
        row = threads.by_id(thread_id)
        review_id = str(row["review_id"])
        thread, comment_id = threads.reply_comment(
            review_id, thread_id, threads.AGENT_ACTOR, threads.AGENT_NAME, "agent", text,
            mentions=mentions, can_activate=False, principal=actor, request_id=request_id)
        comment = next((c for c in thread.get("comments") or [] if c["id"] == comment_id), None)
        with db.tx() as conn:
            users.audit(conn, actor, "plugin.reply", f"review:{review_id}",
                        thread_id=thread_id, comment=comment_id)
        closed = service.close_mention_requests(review_id, thread_id, request_id)
        return {"review_id": review_id, "as": _comment_as_agent(), "thread": thread, "comment": comment,
                "closed": closed}


def _comment_resolve(args: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve (or reopen) a thread as Hermes; the answer also closes the thread's mention chip request."""
    from mockreview import db, owner, service, threads, users
    from mockreview.store import ReviewError
    thread_id = str(args.get("thread_id") or "").strip()
    if not thread_id:
        raise ReviewError("thread_id is required")
    reopen = args.get("reopen")
    if reopen is not None and not isinstance(reopen, bool):
        raise ReviewError("reopen must be true or false", 400)
    status = "open" if reopen else "resolved"
    request_id = args.get("request_id")
    request_id = str(request_id) if request_id else None
    with owner.bound(owner.resolve_soft()):
        user, actor = _comment_actor()
        row = threads.by_id(thread_id)
        review_id = str(row["review_id"])
        thread = threads.set_status(review_id, thread_id, status, threads.AGENT_ACTOR, principal=actor,
                                    drop_wakes=not request_id)
        with db.tx() as conn:
            users.audit(conn, actor, "plugin.reopen" if reopen else "plugin.resolve",
                        f"review:{review_id}", thread_id=thread_id)
        closed = service.close_mention_requests(review_id, thread_id, request_id)
        return {"review_id": review_id, "status": status, "as": _comment_as_agent(), "thread": thread,
                "closed": closed}


def _wake_origin(explicit: Dict[str, Any]) -> "tuple[Dict[str, Any], str]":
    """The chat a waking board is created for: (origin, "") or ({}, why not).

    Explicit origin_* arguments win. With none, the calling session's own context names the chat (Hermes binds it for
    the turn; platforms.session_origin). A Desktop origin also needs its session's key, which only that context has:
    an explicit Desktop origin takes it when the context is that same chat. Nothing is ever guessed."""
    from mockreview import platforms
    here, why = platforms.session_origin()
    if not explicit:
        return (here, "") if here else ({}, f"the calling chat could not be read from this session ({why})")
    if not all(explicit.get(key) for key in ("platform", "chat_id")):
        return {}, "origin_platform and origin_chat_id go together: pass both or neither"
    if (explicit.get("platform") == platforms.LOCAL and here and here.get("platform") == platforms.LOCAL
            and here.get("chat_id") == explicit.get("chat_id")):
        return {**explicit, "session_key": here["session_key"]}, ""
    return explicit, ""


# ---------------------------------------------------------------- handlers

class MockReview:
    """Tool, CLI and slash-command handlers bound to one plugin context."""

    def __init__(self, ctx: Any):
        self.ctx = ctx

    def config_value(self, name: str) -> Any:
        _, _, settings, _ = _mockreview()
        try:
            return self.ctx.get_config(name, settings.MISSING)
        except Exception:  # an older Hermes without plugin settings
            return settings.MISSING

    def _bind(self):
        api, cli, settings, err = _mockreview()
        settings.set_config_source(self.config_value)
        return api, cli, settings, err

    def tool(self, name: str) -> Callable[..., str]:
        def run(args: Dict[str, Any] | None = None, **kwargs: Any) -> str:
            try:
                api, _, settings, ReviewError = self._bind()
            except Exception as e:
                return json.dumps({"error": f"mock-review package not importable from {REPO}: {e}"})
            a = dict(args or {})
            look = {k: a.get(k) for k in settings.REVIEW_KEYS if a.get(k) is not None}
            try:
                if name == "mock_review_create":
                    if not a.get("title"):
                        return json.dumps({"error": "title is required"})
                    origin = {key: a["origin_" + key] for key in ("platform", "chat_id", "thread_id")
                              if "origin_" + key in a}
                    if settings.get("wake_required", a.get("wake")):
                        origin, why = _wake_origin(origin)
                        if not origin:
                            return json.dumps({"error": f"Wake is enabled, but {why}. Create the board from the chat "
                                               "that should be woken, or pass origin_platform and origin_chat_id "
                                               "(and origin_thread_id) exactly as your session context gives them; "
                                               "never guess a destination. Use wake=false only for a demo board."})
                    out: Any = api.create(a["title"], origin or None, brief=a.get("brief") or "",
                                          mode=a.get("mode"), decisions=a.get("decisions"),
                                          prototype=a.get("prototype"), design=a.get("design"),
                                          allow_live_demo=a.get("allow_live_demo"), wake=a.get("wake"),
                                          project=a.get("project"), no_design=a.get("no_design"),
                                          off_brand=a.get("off_brand"), **look)
                elif name == "mock_review_add_round":
                    if not a.get("review_id"):
                        return json.dumps({"error": "review_id is required"})
                    out = api.add_round(a["review_id"], decisions=a.get("decisions"), prototype=a.get("prototype"),
                                        design=a.get("design"), allow_live_demo=a.get("allow_live_demo"),
                                        project=a.get("project"), no_design=a.get("no_design"),
                                        off_brand=a.get("off_brand"), allow_reask=a.get("allow_reask"), **look)
                elif name == "mock_review_feedback":
                    if not a.get("review_id"):
                        return json.dumps({"error": "review_id is required"})
                    rnd = a.get("round")
                    out = api.get_feedback(a["review_id"], int(rnd) if rnd is not None else None)
                elif name == "mock_review_ack":
                    if not a.get("review_id"):
                        return json.dumps({"error": "review_id is required"})
                    rnd = a.get("round")
                    out = api.ack(a["review_id"], int(rnd) if rnd is not None else None)
                elif name == "mock_review_patch_block":
                    for key in ("review_id", "block", "html"):
                        if not a.get(key):
                            return json.dumps({"error": f"{key} is required"})
                    if a.get("round") is None:
                        return json.dumps({"error": "patch_block needs the round you read: pass round=<n> "
                                                    "(mock_review_feedback gives it)"})
                    if a.get("if_rev") is None:
                        return json.dumps({"error": "patch_block needs if_rev: the block revision from the round "
                                                    "you read"})
                    out = api.patch_block(a["review_id"], a["block"], a["html"], option=a.get("option"),
                                          round_n=a.get("round"), if_rev=a.get("if_rev"),
                                          request_ids=a.get("request_ids"))
                elif name == "mock_review_list_comments":
                    out = _comment_list(a)
                elif name == "mock_review_reply":
                    out = _comment_reply(a)
                elif name == "mock_review_resolve":
                    out = _comment_resolve(a)
                else:
                    out = {"reviews": api.list_reviews(include_closed=bool(a.get("include_closed")))}
                return json.dumps(out, ensure_ascii=False)
            except ReviewError as e:
                return json.dumps({"error": e.message})
            except settings.SettingsError as e:
                return json.dumps({"error": str(e)})
            except Exception as e:  # a tool never raises into Hermes
                log.exception("%s failed", name)
                return json.dumps({"error": f"internal error ({type(e).__name__}: {e})"})

        run.__name__ = name
        return run

    # `hermes mockreview ...`
    def cli_setup(self, parser) -> None:
        _, cli, _, _ = _mockreview()
        cli.add_commands(parser)

    def cli_run(self, args) -> int:
        _, cli, _, _ = self._bind()
        return cli.run(args)

    # `/mockreview list | open <id> | close <id> | settings`
    def slash(self, raw: str = "") -> str:
        try:
            api, cli, settings, ReviewError = self._bind()
        except Exception as e:
            return f"mock-review isn't available: {e}"
        parts = (raw or "").split()
        cmd = parts[0].lower() if parts else "help"
        try:
            if cmd == "list":
                rows = api.list_reviews()
                if not rows:
                    return "No open mock reviews."
                return "\n".join(f"{r['review_id']}: {r['title']} ({r['mode']}, round {r['rounds']}, "
                                 f"feedback {r['current_round_feedback']}) {r['url']}" for r in rows)
            if cmd == "open" and len(parts) == 2:
                link = api.review_link(parts[1])
                return f"{link['title']} ({link['status']}): {link['url'] or 'no public URL yet'}"
            if cmd == "close" and len(parts) == 2:
                api.close(parts[1])
                return f"Closed {parts[1]}."
            if cmd == "settings":
                return cli.settings_text(settings.resolve_all(), settings.describe())
        except ReviewError as e:
            return f"Error: {e.message}"
        except Exception as e:
            log.exception("/mockreview %s failed", raw)
            return f"Error: {type(e).__name__}: {e}"
        return "Usage: /mockreview list | open <review_id> | close <review_id> | settings"


    # ctx.register_platform_handler("webhook", ...): the gateway calls this when its webhook platform connects.
    def gateway_factory(self, native: Any = None, adapter: Any = None) -> str:
        try:
            self._bind()
            import mockreview.autostart as autostart
        except Exception as e:
            log.warning("mock-review autostart unavailable: %s", e)
            return "error"
        return autostart.gateway_factory(native, adapter, self.ctx)


def register(ctx: Any) -> None:
    mr = MockReview(ctx)
    _, _, settings, _ = mr._bind()

    def setting(name: str) -> Any:
        try:
            return settings.get(name)
        except Exception:
            return settings.BY_NAME[name].default

    defs = schemas(bool(setting("ask_host_each_time")), str(setting("default_host")), bool(setting("offer_live_demo")))
    for name, schema in defs.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=mr.tool(name),
                          description=schema["description"], emoji="🖼️")
    ctx.register_skill("usage", PLUGIN_DIR / "skills" / "usage" / "SKILL.md",
                       description="When and how to use the mock_review_* tools and settings")
    ctx.register_command("mockreview", handler=mr.slash, description="Mock reviews: list, open, close, settings",
                         args_hint="list | open <id> | close <id> | settings")
    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(name="mockreview", help="Mock review boards and settings",
                     setup_fn=mr.cli_setup, handler_fn=mr.cli_run,
                     description="mock-review CLI. First time? Run: hermes mockreview setup. "
                                 "`hermes mockreview settings` prints each setting and its source.")
    # The board server starts in the gateway process only, at webhook connect, and only with autostart on
    # (mockreview/autostart.py). Nothing starts here: register() also runs in the CLI and in `plugins validate`.
    register_platform = getattr(ctx, "register_platform_handler", None)
    if callable(register_platform):
        register_platform("webhook", mr.gateway_factory)
    else:
        log.debug("this Hermes has no register_platform_handler: mock-review autostart is unavailable")
    # A plugin rescan in the gateway registers again with a new context but never calls gateway_factory again: the
    # board already serving keeps waking Desktop chats through the newest context.
    import mockreview.autostart as autostart
    import mockreview.sessionwake as sessionwake
    board = autostart.running() if sessionwake.attached() else None
    if board is not None and sessionwake.owns_home(board.owner_home):
        sessionwake.attach(ctx)
