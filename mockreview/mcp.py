"""A Model Context Protocol server over stdio for coding agents outside Hermes.

Transport rule: stdout carries one JSON-RPC 2.0 message per line and nothing else. Every log line, warning and
traceback goes to stderr. A stray print on stdout breaks every MCP client, so this module never calls print().

The server implements the legacy initialize handshake (the era every client here speaks): ``initialize``,
``notifications/initialized``, ``ping``, ``tools/list`` and ``tools/call``. A modern-only client probes
``server/discover``, gets ``-32601`` and falls back to ``initialize``. Read until EOF, then exit 0.

The five tools call ``mockreview.api`` directly, exactly like the CLI and the Hermes plugin tools: never a second
code path, never a store read or write of its own. Wake is off by default because an external CLI has no Hermes
session context; an explicit ``--platform``/``--chat-id`` pair (or the matching tool arguments) selects an origin.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import api, settings
from .store import ReviewError

log = logging.getLogger("mockreview.mcp")

SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
DEFAULT_PROTOCOL = SUPPORTED[0]

TOOL_NAMES: Tuple[str, ...] = (
    "mock_review_create",
    "mock_review_add_round",
    "mock_review_feedback",
    "mock_review_ack",
    "mock_review_list",
)

WAKE_REQUIRED = (
    "Wake is enabled: explicit origin_platform and origin_chat_id are required. Copy source/platform, chat ID and "
    "optional thread ID from your session context in the system prompt; never guess a destination. Use wake=false "
    "only for a demo board."
)
INSTRUCTIONS = (
    "Create a review board with mock_review_create and send its url to the reviewer. "
    "Read the marks back with mock_review_feedback. "
    "Acknowledge a handled round with mock_review_ack so no stalled ping is sent."
)

_JSON_TYPES = {"bool": "boolean", "int": "integer", "float": "number", "list": "array", "str": "string"}


@dataclass
class Options:
    platform: Optional[str] = None
    chat_id: Optional[str] = None
    thread: Optional[str] = None
    wake: bool = False


# ---------------------------------------------------------------- tool descriptors

def _round_properties() -> Dict[str, Any]:
    return {
        "decisions": {"type": "string", "description": "Absolute path to decisions.json (a decision round)."},
        "prototype": {"type": "string",
                      "description": "Absolute path to a clickable index.html (\"try it live\")."},
        "design": {"type": "string", "description": "Design profile name or absolute dir."},
        "allow_live_demo": {"type": "boolean",
                            "description": "Allow a prototype round for this call. Only true if the user asked for a "
                                           "live demo."},
        **brand_properties(),
    }


def brand_properties() -> Dict[str, Any]:
    return {
        "project": {"type": "string",
                    "description": "Absolute path of the project (repo) this board is for. Pass it: the design profile "
                                   "bound to it applies on its own, in worktrees too."},
        "no_design": {"type": "boolean",
                      "description": "A board that deliberately has no design profile. Needed only when profiles are "
                                     "saved and none applies."},
        "off_brand": {"type": "string", "maxLength": 200,
                      "description": "Why this round is off-brand on purpose (1-200 characters). Skips the check that "
                                     "mocks use only the profile's colours and fonts; recorded in the review."},
    }


def _look_properties() -> Dict[str, Any]:
    """The per-review look keys, derived from settings.SPEC (never retyped here)."""
    props: Dict[str, Any] = {}
    for name in settings.REVIEW_KEYS:
        s = settings.BY_NAME[name]
        prop: Dict[str, Any] = {"type": _JSON_TYPES.get(s.type, "string"), "description": s.description}
        if s.type == "list":
            prop["items"] = {"type": "string"}
        if s.choices:
            prop["enum"] = list(s.choices)
        props[name] = prop
    return props


def _origin_properties() -> Dict[str, Any]:
    # Advisory only: store.normalize_origin is the single validator (it also bounds the platform and the identifiers),
    # so these schemas stay loose on purpose rather than duplicating its regexes here.
    return {
        "origin_platform": {"type": "string",
                            "description": "Copy source/platform from your session context; never guess."},
        "origin_chat_id": {"type": "string",
                           "description": "Copy the origin chat ID from your session context; required for wake."},
        "origin_thread_id": {"type": "string",
                             "description": "Copy the thread ID from your session context if present; otherwise omit."},
    }


def tools() -> List[Dict[str, Any]]:
    look = _look_properties()
    round_props = _round_properties()
    return [
        {
            "name": "mock_review_create",
            "description": "Create a mock review board (round 1) for the user to mark up. Returns review_id + url. "
                           "Pass project (absolute repo path) so the project's bound design profile applies; with "
                           "saved profiles, a create with no design, no binding and no no_design fails. Mocks must "
                           "use the profile's colours and fonts (var(--accent) ...), or the call fails. interactive_elements is "
                           "on by default: build every control a mock shows so it works without script (select, "
                           "details, checkbox or radio inputs styled with :checked, text inputs).",
            "inputSchema": {"type": "object", "properties": {
                "title": {"type": "string"},
                "brief": {"type": "string"},
                **round_props,
                "mode": {"type": "string", "enum": ["tailnet", "tailscale", "local", "lan", "herenow"],
                         "description": "Hosting: tailnet (private), local (this computer), lan or herenow "
                                        "(password protected by default)."},
                "wake": {"type": "boolean",
                         "description": "Wake the origin chat on Send. Off by default; needs an explicit origin."},
                "password": {"type": "boolean",
                             "description": "Password protect here.now (default true); returned next to the URL."},
                **look,
                **_origin_properties(),
                }, "required": ["title"]},
        },
        {
            "name": "mock_review_add_round",
            "description": "Add the next round to an open mock review (same link). Keeps the review's design profile; "
                           "pass project (absolute repo path) so a bound profile applies. Mocks must use the "
                           "profile's colours and fonts, or the call fails. After Build, post the "
                           "build result here. Only open blocks come back; approved ones go in a settled block.",
            "inputSchema": {"type": "object", "properties": {
                "review_id": {"type": "string"},
                **round_props,
                "allow_reask": {"type": "boolean",
                                "description": "Ask again blocks the last round approved or answered. Without it "
                                               "such a round is refused: settled blocks belong in a `settled` block."},
                **look,
                }, "required": ["review_id"]},
        },
        {
            "name": "mock_review_feedback",
            "description": "Read a round's feedback (marks, pins, notes) as JSON + markdown. action is send or build; "
                           "on build, follow next_step; on send, next_round says what the next round may ask.",
            "inputSchema": {"type": "object", "properties": {
                "review_id": {"type": "string"},
                "round": {"type": "integer", "description": "Default: current."},
                }, "required": ["review_id"]},
        },
        {
            "name": "mock_review_ack",
            "description": "Mark a round's feedback as handled (answered here, or no round coming): no stalled ping. "
                           "Not instead of building after Build.",
            "inputSchema": {"type": "object", "properties": {
                "review_id": {"type": "string"},
                "round": {"type": "integer", "description": "Default: current."},
                }, "required": ["review_id"]},
        },
        {
            "name": "mock_review_list",
            "description": "List mock reviews (open only unless include_closed).",
            "inputSchema": {"type": "object", "properties": {
                "include_closed": {"type": "boolean"},
                }, "required": []},
        },
    ]


# ---------------------------------------------------------------- protocol

def plugin_version() -> str:
    """The version line of the plugin.yaml next to this package; "unknown" on any failure (stdlib only)."""
    try:
        text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
        m = re.search(r"^version:\s*([0-9][^\s#]*)", text, re.MULTILINE)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_result(value: Any, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": is_error}


def _look(args: Dict[str, Any]) -> Dict[str, Any]:
    return {k: args.get(k) for k in settings.REVIEW_KEYS if args.get(k) is not None}


def _brand(args: Dict[str, Any]) -> Dict[str, Any]:
    """project, no_design, off_brand as given: no default project here, the agent passes it."""
    return {"project": args.get("project"), "no_design": args.get("no_design"), "off_brand": args.get("off_brand")}


def _round_number(args: Dict[str, Any]) -> Optional[int]:
    value = args.get("round")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ReviewError("round must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ReviewError("round must be an integer")


def _create(args: Dict[str, Any], options: Options) -> Dict[str, Any]:
    if not args.get("title"):
        raise ReviewError("title is required")
    platform = args.get("origin_platform")
    if platform is None:
        platform = options.platform
    chat_id = args.get("origin_chat_id")
    if chat_id is None:
        chat_id = options.chat_id
    thread_id = args.get("origin_thread_id")
    if thread_id is None:
        thread_id = options.thread
    origin = None
    if platform and chat_id:
        origin = {"platform": platform, "chat_id": chat_id}
        if thread_id:
            origin["thread_id"] = thread_id
    wake = args["wake"] if isinstance(args.get("wake"), bool) else options.wake
    if wake and not (platform and chat_id):
        raise ReviewError(WAKE_REQUIRED, 409)
    return api.create(
        str(args["title"]), origin,
        brief=args.get("brief") or "",
        mode=args.get("mode"),
        decisions=args.get("decisions"),
        prototype=args.get("prototype"),
        design=args.get("design"),
        allow_live_demo=args.get("allow_live_demo"),
        wake=wake,
        password=args.get("password"),
        **_brand(args),
        **_look(args),
    )


def _add_round(args: Dict[str, Any], options: Options) -> Dict[str, Any]:
    if not args.get("review_id"):
        raise ReviewError("review_id is required")
    return api.add_round(
        str(args["review_id"]),
        decisions=args.get("decisions"),
        prototype=args.get("prototype"),
        design=args.get("design"),
        allow_live_demo=args.get("allow_live_demo"),
        allow_reask=args.get("allow_reask"),
        **_brand(args),
        **_look(args),
    )


def _feedback(args: Dict[str, Any], options: Options) -> Dict[str, Any]:
    if not args.get("review_id"):
        raise ReviewError("review_id is required")
    return api.get_feedback(str(args["review_id"]), _round_number(args))


def _ack(args: Dict[str, Any], options: Options) -> Dict[str, Any]:
    if not args.get("review_id"):
        raise ReviewError("review_id is required")
    return api.ack(str(args["review_id"]), _round_number(args))


def _list(args: Dict[str, Any], options: Options) -> Dict[str, Any]:
    return {"reviews": api.list_reviews(include_closed=bool(args.get("include_closed")))}


_TOOL_FUNCS = {
    "mock_review_create": _create,
    "mock_review_add_round": _add_round,
    "mock_review_feedback": _feedback,
    "mock_review_ack": _ack,
    "mock_review_list": _list,
}


def call_tool(name: str, arguments: Dict[str, Any], options: Options) -> Dict[str, Any]:
    """Run one tool and always return the MCP content shape, never raise."""
    func = _TOOL_FUNCS.get(name)
    if func is None:
        return _tool_result({"error": f"unknown tool: {name}"}, True)
    try:
        return _tool_result(func(arguments, options))
    except ReviewError as e:
        return _tool_result({"error": e.message}, True)
    except settings.SettingsError as e:
        return _tool_result({"error": str(e)}, True)
    except Exception as e:  # a tool never raises into the client; the traceback goes to stderr only
        log.exception("mock-review MCP tool %s failed", name)
        return _tool_result({"error": f"internal error ({type(e).__name__}: {e})"}, True)


def handle_request(req: Any, options: Options) -> Optional[Dict[str, Any]]:
    """One decoded JSON-RPC request in, one response dict out, or None for a notification.

    Pure: no stdin/stdout inside; serve() owns the line loop.
    """
    if not isinstance(req, dict):
        return _error(None, -32600, "invalid request: expected a JSON object")
    request_id = req.get("id")
    has_id = "id" in req
    method = req.get("method")
    if not isinstance(method, str) or not method:
        return _error(request_id if has_id else None, -32600, "invalid request: method is required")
    if not has_id:
        return None  # notification: never answered, never an error line
    params = req.get("params")
    if method == "initialize":
        if params is not None and not isinstance(params, dict):
            return _error(request_id, -32602, "invalid params: expected an object")
        params = params or {}
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED else DEFAULT_PROTOCOL
        client_info = params.get("clientInfo")
        log.info("initialize: requested protocolVersion=%r clientInfo=%s", requested, client_info)
        return _result(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock-review", "version": plugin_version()},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": tools()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _error(request_id, -32602, "invalid params: expected an object")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(request_id, -32602, "invalid params: name is required")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "invalid params: arguments must be an object")
        return _result(request_id, call_tool(name, arguments, options))
    return _error(request_id, -32601, f"method not found: {method}")


def _utf8_lines(stdin: Any):
    """Read lines as UTF-8 whatever the console code page says.

    MCP frames MUST be UTF-8. On Windows a piped child gets the ANSI code page, so a curly quote or an emoji in a
    title would raise UnicodeDecodeError on read and kill the process. Reading the binary layer makes the encoding
    ours instead of the text wrapper's; undecodable bytes become U+FFFD, which the JSON parser rejects as -32700
    without taking the session down. A stream without a binary layer (io.StringIO in the tests) is used as is.
    """
    buf = getattr(stdin, "buffer", None)
    if buf is None:
        return stdin
    return (raw.decode("utf-8", "replace") for raw in buf)


def _write_response(stdout: Any, response: Dict[str, Any]) -> None:
    """Write one UTF-8 JSON-RPC line, falling back to escaped ASCII for a lone surrogate.

    Encoding is pinned here for the same reason as the reader: the text wrapper would use the console code page,
    and a UnicodeEncodeError mid-session would cost the client its server. A stream without a binary layer
    (io.StringIO in the tests) keeps its own encoding.
    """
    text = json.dumps(response, ensure_ascii=False)
    buf = getattr(stdout, "buffer", None)
    if buf is None:
        try:
            stdout.write(text + "\n")
        except UnicodeEncodeError:
            stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
        stdout.flush()
        return
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate cannot be UTF-8 encoded; escaped ASCII always can
        data = json.dumps(response, ensure_ascii=True).encode("ascii")
    buf.write(data + b"\n")
    buf.flush()


def serve(stdin: Any, stdout: Any, options: Options) -> int:
    """Read newline-delimited JSON-RPC from stdin, answer on stdout, return 0 at EOF."""
    _configure_logging()
    for raw in _utf8_lines(stdin):
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            response: Optional[Dict[str, Any]] = _error(None, -32700, "parse error")
        else:
            response = handle_request(req, options)
        if response is not None:
            _write_response(stdout, response)
    return 0


def _configure_logging() -> None:
    """INFO diagnostics on stderr only; stdout stays reserved for JSON-RPC."""
    if getattr(log, "_mockreview_mcp_handler", False):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("mockreview.mcp: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    log._mockreview_mcp_handler = True  # type: ignore[attr-defined]


def options_from_argv(argv: Optional[List[str]] = None) -> Options:
    p = argparse.ArgumentParser(prog="python -m mockreview.mcp",
                                description="Run the mock-review MCP server on stdio.")
    p.add_argument("--platform", default=os.environ.get("MOCKREVIEW_PLATFORM") or None,
                   help="origin platform for wake-ups (env: MOCKREVIEW_PLATFORM)")
    p.add_argument("--chat-id", default=os.environ.get("MOCKREVIEW_CHAT_ID") or None,
                   help="origin chat ID for wake-ups (env: MOCKREVIEW_CHAT_ID)")
    p.add_argument("--thread", default=os.environ.get("MOCKREVIEW_THREAD") or None,
                   help="optional origin thread ID (env: MOCKREVIEW_THREAD)")
    p.add_argument("--wake", action="store_true", help="wake the origin chat on Send (default: off)")
    a = p.parse_args(argv)
    return Options(platform=a.platform, chat_id=a.chat_id, thread=a.thread, wake=a.wake)


def main(argv: Optional[list] = None) -> int:
    return serve(sys.stdin, sys.stdout, options_from_argv(argv))


if __name__ == "__main__":
    sys.exit(main())
