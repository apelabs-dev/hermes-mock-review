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
    "mobile_width": {"type": "integer"},
    "tablet_width": {"type": "integer"},
}
ROUND_PROPS: Dict[str, Any] = {
    "decisions": {"type": "string", "description": "Absolute path to decisions.json (a decision round)."},
    "prototype": {"type": "string", "description": "Absolute path to a clickable index.html (\"try it live\")."},
    "design": {"type": "string", "description": "Design profile name or absolute dir."},
}


def _live_demo_prop(on: bool) -> Dict[str, Any]:
    return {"type": "boolean", "description": (
        "Allow a prototype round for this call." if on else
        "Live demos are off (offer_live_demo). Only true if the user asked for a live demo.")}


def _mode_prop(ask: bool, host: str) -> Dict[str, Any]:
    return {"type": "string", "enum": ["tailnet", "local", "herenow"], "description": (
        "Hosting. Ask the user first: tailnet (private), local (this computer's address) or herenow (password protected by default)."
        if ask else
        f"Hosting; omit for the saved default ({host}).")}


def schemas(ask_host: bool, default_host: str, live_demo: bool) -> Dict[str, Dict[str, Any]]:
    return {
        "mock_review_create": {
            "name": "mock_review_create",
            "description": "Create a mock review board (round 1) for the user to mark up. Returns review_id + url.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"},
                "origin_platform": {"type": "string", "maxLength": 64, "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    "description": "Copy source/platform from your session context in the system prompt; never guess."},
                "origin_chat_id": {"type": "string", "minLength": 1, "maxLength": 256,
                    "pattern": "^[A-Za-z0-9_.:@!#+/~=-]{1,256}$",
                    "description": "Copy the origin chat ID from your session context in the system prompt; required for wake."},
                "origin_thread_id": {"type": "string", "minLength": 1, "maxLength": 256,
                    "pattern": "^[A-Za-z0-9_.:@!#+/~=-]{1,256}$",
                    "description": "Copy the thread ID from your session context in the system prompt if present; otherwise omit."},
                "brief": {"type": "string"},
                "mode": _mode_prop(ask_host, default_host),
                "password": {"type": "boolean", "description": "Password protect here.now (default true); return password next to URL."},
                **ROUND_PROPS, "allow_live_demo": _live_demo_prop(live_demo), **LOOK_PROPS,
                "wake": {"type": "boolean", "description": "False: demo board nobody is woken by (skips the wake-route check)."}},
                "required": ["title"]},
        },
        "mock_review_add_round": {
            "name": "mock_review_add_round",
            "description": "Add the next round to an open mock review (same link).",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"}, **ROUND_PROPS, "allow_live_demo": _live_demo_prop(live_demo),
                **LOOK_PROPS},
                "required": ["review_id"]},
        },
        "mock_review_feedback": {
            "name": "mock_review_feedback",
            "description": "Read a round's feedback (marks, pins, notes) as JSON + markdown.",
            "parameters": {"type": "object", "properties": {
                "review_id": {"type": "string"}, "round": {"type": "integer", "description": "Default: current."}},
                "required": ["review_id"]},
        },
        "mock_review_list": {
            "name": "mock_review_list",
            "description": "List mock reviews (open only unless include_closed).",
            "parameters": {"type": "object", "properties": {"include_closed": {"type": "boolean"}}},
        },
    }


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
                    if settings.get("wake_required", a.get("wake")) and not all(
                            origin.get(key) for key in ("platform", "chat_id")):
                        return json.dumps({"error": "Wake is enabled: explicit origin_platform and origin_chat_id "
                                           "are required. Copy source/platform, chat ID and optional thread ID from "
                                           "your session context in the system prompt; never guess a destination. "
                                           "Use wake=false only for a demo board."})
                    out: Any = api.create(a["title"], origin or None, brief=a.get("brief") or "",
                                          mode=a.get("mode"), decisions=a.get("decisions"),
                                          prototype=a.get("prototype"), design=a.get("design"),
                                          allow_live_demo=a.get("allow_live_demo"), wake=a.get("wake"), password=a.get("password"), **look)
                elif name == "mock_review_add_round":
                    if not a.get("review_id"):
                        return json.dumps({"error": "review_id is required"})
                    out = api.add_round(a["review_id"], decisions=a.get("decisions"), prototype=a.get("prototype"),
                                        design=a.get("design"), allow_live_demo=a.get("allow_live_demo"), **look)
                elif name == "mock_review_feedback":
                    if not a.get("review_id"):
                        return json.dumps({"error": "review_id is required"})
                    rnd = a.get("round")
                    out = api.get_feedback(a["review_id"], int(rnd) if rnd is not None else None)
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

    # `/mockreview list | open <id> | close <id> | password <id> on|off | settings`
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
                res = api.close(parts[1])
                extra = " Its here.now site was deleted." if res.get("site_deleted") else ""
                return f"Closed {parts[1]}.{extra}"
            if cmd == "password" and len(parts) == 3 and parts[2] in ("on", "off"):
                return json.dumps(api.password(parts[1], parts[2] == "on"), ensure_ascii=False)
            if cmd == "settings":
                return cli.settings_text(settings.resolve_all(), settings.describe())
        except ReviewError as e:
            return f"Error: {e.message}"
        except Exception as e:
            log.exception("/mockreview %s failed", raw)
            return f"Error: {type(e).__name__}: {e}"
        return "Usage: /mockreview list | open <review_id> | close <review_id> | password <review_id> on|off | settings"


    # ctx.register_platform_handler("webhook", ...): the gateway calls this when its webhook platform connects.
    def gateway_factory(self, native: Any = None, adapter: Any = None) -> str:
        try:
            self._bind()
            import mockreview.autostart as autostart
        except Exception as e:
            log.warning("mock-review autostart unavailable: %s", e)
            return "error"
        return autostart.gateway_factory(native, adapter)


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
    ctx.register_command("mockreview", handler=mr.slash, description="Mock reviews: list, open, close, password, settings",
                         args_hint="list | open <id> | close <id> | password <id> on|off | settings")
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
