"""The `hermes mockreview setup` onboarding wizard.

Interactive-but-headless-safe: when stdin is not a terminal and `--yes` is absent it prints a one-line usage
message and exits 2 before touching anything. Every Hermes-shaped operation goes through the `hermes` CLI as a
subprocess (argv list, no shell, a timeout, captured output); the plugin never reads or writes Hermes state files
itself. The signing secret is only ever generated or read in memory, written through Hermes's own
`hermes config set MOCKREVIEW_WEBHOOK_SECRET <value>` route, and every captured subprocess output is passed through
`redact()` before it is printed. The secret value is never printed, logged or stored by the wizard.

The pure helpers (`parse_version`, `redact`, `tailnet_base`, `detect_platforms`, `board_identity`, `gateway_state`,
`port_free`, …) are importable without a terminal; `run()` is the CLI entry point.

The order matters: the origin platforms are settled first (a stop there leaves the Hermes home untouched), then the
changes are made, and the next step is printed once at the very end, after the summary, because only then it is known
whether the board answers and whether the gateway runs.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets as stdlib_secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import api, config, owner, settings, store
from .store import ReviewError

# The installed plugin directory is the tree that holds plugin.yaml and mockreview/ (bin/mockreview lives under it).
PLUGIN_DIR = Path(__file__).resolve().parent.parent

SECRET_NAME = "MOCKREVIEW_WEBHOOK_SECRET"
SETTINGS = "plugins.entries.mock-review.settings"
WEBHOOK_ENABLED_KEY = "platforms.webhook.enabled"

VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MIN_VERSION = (0, 22, 0)
VERSION_TIMEOUT = 120    # `hermes --version` checks for updates on the first run in a Hermes home

# Hermes platforms that are not a chat you can be woken in: the webhook ingress, the Microsoft Graph change-notification
# ingress, the OpenAI-compatible API server and the local CLI. Auto-detection skips them; --origin-platform does not.
NON_CHAT_PLATFORMS = ("webhook", "msgraph_webhook", "api_server", "local")

HOSTING_CHOICES = ("local", "tailnet", "lan", "herenow")
OBSOLETE_KEYS = ("herenow_credentials", "subscriptions_file", "herenow_publish_script")

# The wake prompt Hermes renders for an inbound round. `{...}` placeholders are filled by Hermes from the payload.
PROMPT_TEMPLATE = (
    "Mock review feedback is ready: {review_id} round {round} — {title}. "
    "Read it with mock_review_feedback(review_id='{review_id}', round={round}), then act on the notes. "
    "Board: {review_url}"
)
ROUTE_DESCRIPTION = "Wakes the origin chat when a mock-review round is submitted."

# The deliver-only notice route renders the payload's `message` verbatim: an immediate ack (and a stalled ping later).
NOTICE_PROMPT_TEMPLATE = "{message}"
NOTICE_ROUTE_DESCRIPTION = "Instant ack and stalled-ping messages for a mock-review submission (deliver-only, mirrored into the session)."



def wake_route_name(route: str, platform: str) -> str:
    return f"{route}-{platform}"


def notice_route_name(route: str, platform: str) -> str:
    return f"{route}-{platform}-notice"


def wake_route_argv(route: str, platform: str, secret: str) -> List[str]:
    """`hermes webhook subscribe` argv for the agent route that wakes the origin chat on Send."""
    return ["webhook", "subscribe", wake_route_name(route, platform), "--secret", secret,
            "--prompt", PROMPT_TEMPLATE,
            "--description", ROUTE_DESCRIPTION,
            "--deliver", platform, "--deliver-chat-id", "{chat_id}"]


def notice_route_argv(route: str, platform: str, secret: str) -> List[str]:
    """`hermes webhook subscribe` argv for the deliver-only route that posts the ack and the stalled ping to the
    origin chat and mirrors them into that chat's session."""
    return ["webhook", "subscribe", notice_route_name(route, platform), "--secret", secret,
            "--prompt", NOTICE_PROMPT_TEMPLATE,
            "--description", NOTICE_ROUTE_DESCRIPTION,
            "--deliver", platform, "--deliver-chat-id", "{chat_id}",
            "--deliver-only", "--mirror-to-session"]


LAN_WARNING = ("Anyone who can reach this port can open every board and press Send — there is no login. "
               "Only do this on a network you trust.")

WEBHOOK_ENABLE_CMD = "hermes config set platforms.webhook.enabled true"
HERENOW_INSTALL_CMD = "hermes skills install official/productivity/here-now -y"
HERENOW_KEY_CMD = "hermes config set HERENOW_API_KEY <key>"
FOREGROUND_CMD = "hermes mockreview serve"

NON_TTY_USAGE = "not a terminal: re-run with --yes (and --hosting / --origin-platform), or see --help"

NO_PLATFORM_MESSAGE = ("no chat platform found in the Hermes config (platforms.*), so setup cannot tell where to wake "
                       "you. Re-run with the platform you review on: hermes mockreview setup --origin-platform "
                       "telegram (repeatable)")

TEST_TITLE = "mock-review setup test"
SETUP_BRIEF = "created by `hermes mockreview setup` to verify the create path"


# ---------------------------------------------------------------- small pure helpers

def hermes_bin() -> str:
    return os.environ.get("MOCKREVIEW_HERMES_BIN", "hermes")


class HermesCallError(subprocess.SubprocessError):
    """A `hermes` call that timed out or could not start. Carries only the leading subcommand words and the timeout:
    never the argv tail (it holds the signing secret) and never `str()` of the original error."""

    def __init__(self, words: str, timeout: Optional[float], started: bool, binary: str) -> None:
        super().__init__(f"hermes {words}".strip())
        self.words, self.timeout, self.started, self.binary = words, timeout, started, binary


def _subcommand_words(argv: List[str]) -> str:
    """Up to two leading plain words of a hermes argv ("config set", "webhook subscribe"); stops at a flag or a value."""
    words: List[str] = []
    for a in argv[:2]:
        if not re.fullmatch(r"[a-z][a-z-]*", a):
            break
        words.append(a)
    return " ".join(words)


def _hermes_cmd(argv: List[str]) -> List[str]:
    return [hermes_bin(), *argv]


def run_hermes(argv: List[str], *, timeout: int = 30) -> "subprocess.CompletedProcess[str]":
    """One `hermes` invocation: argv list, no shell, a timeout, captured output. A timeout or a binary that cannot start
    raises HermesCallError (a SubprocessError), whose text names no argument."""
    try:
        return subprocess.run(_hermes_cmd(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HermesCallError(_subcommand_words(argv), timeout, True, os.path.basename(hermes_bin())) from None
    except OSError:
        raise HermesCallError(_subcommand_words(argv), timeout, False, os.path.basename(hermes_bin())) from None


def parse_version(text: Optional[str]) -> Optional[Tuple[int, int, int]]:
    if not text:
        return None
    m = VERSION_RE.search(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def python_ok(info: Any = None) -> bool:
    """True when the interpreter is at least Python 3.9. `info` defaults to the running interpreter's version."""
    info = sys.version_info if info is None else info
    return tuple(info)[:2] >= (3, 9)


def redact(text: Optional[str], secret: Optional[str]) -> str:
    """Replace the signing secret with `***` in any text about to be shown. The only way captured output is printed."""
    text = "" if text is None else text
    if not secret:
        return text
    return text.replace(secret, "***")


def port_free(port: int) -> Tuple[bool, str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("", port))
        s.close()
        return True, ""
    except OSError as e:
        return False, str(e)


def tailnet_base(runner: Any = None) -> Tuple[Optional[str], str]:
    """The public base URL of this Tailscale node, or (None, why not). Never raises."""
    if runner is None:
        def runner():
            return subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
    try:
        proc = runner()
    except FileNotFoundError:
        return None, "tailscale is not installed"
    except subprocess.TimeoutExpired:
        return None, "tailscale timed out"
    if getattr(proc, "returncode", 1) != 0:
        lines = [ln.strip() for ln in (getattr(proc, "stderr", "") or "").splitlines() if ln.strip()]
        err = lines[0][:120] if lines else ""     # the first line only: a crash dump is not for the user
        return None, f"tailscale exited {proc.returncode}" + (f": {err}" if err else "")
    try:
        status = json.loads(getattr(proc, "stdout", "") or "")
    except ValueError:
        return None, "tailscale output is not valid JSON"
    if not isinstance(status, dict) or not isinstance(status.get("Self"), dict):
        return None, "tailscale reported no self node"
    name = str(status["Self"].get("DNSName") or "").rstrip(".")
    if status.get("BackendState") != "Running":
        return None, "tailscale backend is not running"
    if not name or not re.fullmatch(r"[a-zA-Z0-9.-]+", name):
        return None, "tailscale DNSName is unavailable"
    return f"https://{name}", ""


def webhook_enabled(run: Any = None) -> Tuple[bool, str]:
    """(enabled, raw output). Reads `hermes config get --json platforms.webhook.enabled`."""
    run = run or run_hermes
    proc = run(["config", "get", "--json", WEBHOOK_ENABLED_KEY])
    text = (proc.stdout or "").strip()
    return (proc.returncode == 0 and text.lower() == "true"), text


def platforms_config(run: Any = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """The `platforms` mapping from `hermes config get --json platforms`, or (None, why not). It can hold tokens: callers
    read names and the webhook port from it and never print it."""
    run = run or run_hermes
    proc = run(["config", "get", "--json", "platforms"])
    if proc.returncode != 0:
        return None, "no platforms configured"
    try:
        platforms = json.loads(proc.stdout or "")
    except ValueError:
        return None, "platforms config did not parse"
    if not isinstance(platforms, dict):
        return None, "platforms config is not a mapping"
    return platforms, ""


def chat_platforms(platforms: Optional[Dict[str, Any]]) -> List[str]:
    """The chat platforms in a `platforms` mapping, sorted. See `detect_platforms`."""
    names = []
    for name, entry in (platforms or {}).items():
        if str(name) in NON_CHAT_PLATFORMS or not PLATFORM_RE.fullmatch(str(name)):
            continue
        if isinstance(entry, dict) and entry.get("enabled") is not False:
            names.append(str(name))
    return sorted(names)


def detect_platforms(run: Any = None) -> Tuple[List[str], str]:
    """Origin platforms from `hermes config get --json platforms`. A platform counts when its entry is a dict whose
    `enabled` is not False, its name fits the origin-platform name rule, and it is a chat: the platforms in
    `NON_CHAT_PLATFORMS` (webhook ingress, Microsoft Graph ingress, API server, local CLI) never count here. An explicit
    `--origin-platform` is not filtered by this. No other hardcoded platform list."""
    platforms, why = platforms_config(run)
    if platforms is None:
        return [], why
    return chat_platforms(platforms), ""


def _get_json(url: str, timeout: float, limit: int = 4 * 1024 * 1024) -> Tuple[str, Any]:
    """GET one loopback URL, never through a proxy. ('ok', parsed JSON) | ('http', status) | ('hung', None) |
    ('refused', None) | ('garbage', None). Never raises."""
    fetch = urllib.request.build_opener(urllib.request.ProxyHandler({})).open     # a URL, not a file: no text mode
    try:
        with fetch(url, timeout=timeout) as r:
            raw = r.read(limit)
    except urllib.error.HTTPError as e:
        return "http", e.code
    except (http.client.HTTPException, ConnectionResetError, socket.timeout, TimeoutError):
        return "hung", None                      # something accepted the connection and did not speak HTTP in time
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError, ConnectionResetError)):
            return "hung", None
        return "refused", None
    except (OSError, ValueError):
        return "refused", None
    try:
        return "ok", json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return "garbage", None


def _same_path(a: Any, b: Any) -> bool:
    if not isinstance(a, str) or not a:
        return False
    try:
        return Path(a).expanduser().resolve() == Path(str(b)).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def board_identity(port: int, timeout: float = 2.0) -> Tuple[str, Dict[str, Any]]:
    """Who answers on 127.0.0.1:<port>? Asks `GET /api/owner` and returns (kind, owner info):

    ours                 a mock-review board for this Hermes home (its `hermes_home` or its `data_root` is ours)
    other-mock-review    a mock-review board for another Hermes home (info keeps `hermes_home`)
    foreign              something answers, and it is not a mock-review owner API
    none                 nothing answers

    Never raises.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return "none", {}
    if not 0 < port < 65536:
        return "none", {}
    # Connect on its own first, with a longer timeout than the request. Windows answers a closed loopback port only
    # after ~2 s of SYN retries, so one short HTTP timeout would read a free port as "hung", i.e. foreign.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=max(timeout, 5.0)):
            pass
    except ConnectionRefusedError:
        return "none", {}
    except OSError:
        pass                                     # leave the verdict to the HTTP probe below
    how, got = _get_json(f"http://127.0.0.1:{port}{owner.OWNER_PATH}", timeout, limit=65536)
    if how == "refused":
        return "none", {}
    if how != "ok":
        return "foreign", {}
    if not (isinstance(got, dict) and got.get("service") == owner.SERVICE and isinstance(got.get("data_root"), str)):
        return "foreign", {}
    if _same_path(got.get("hermes_home"), config.hermes_home()) or _same_path(got["data_root"], config.own_data_root()):
        return "ours", got
    return "other-mock-review", got


def board_loads(port: int, review_id: str, timeout: float = 4.0) -> bool:
    """True when this plugin's board answers on the port AND serves the review (`/r/<id>/state.json` names it)."""
    if board_identity(port)[0] != "ours":
        return False
    how, got = _get_json(f"http://127.0.0.1:{int(port)}/r/{review_id}/state.json", timeout)
    return how == "ok" and isinstance(got, dict) and got.get("id") == review_id


_GATEWAY_RUNNING = ("gateway is running", "gateway is supervised by launchd", "gateway service is running",
                    "detached fallback process is running", "detached gateway process is running",
                    "gateway process is running for this profile")
_GATEWAY_MANUAL = ("running manually, not as a system service", "gateway process is running for this profile")
_GATEWAY_STOPPED = ("gateway is not running", "gateway service is stopped", "gateway service is not loaded",
                    "no fallback process is running")


def gateway_state(run: Any = None) -> Tuple[str, bool]:
    """(state, manual) from `hermes gateway status`: state is "running", "stopped" or "unknown" (anything unexpected,
    a non-zero exit, a timeout, a missing binary); manual is True when Hermes says it was started by hand
    (`hermes gateway run`) rather than as a service. Never raises."""
    run = run or run_hermes
    try:
        proc = run(["gateway", "status"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "unknown", False
    if getattr(proc, "returncode", 1) != 0:
        return "unknown", False
    text = ((getattr(proc, "stdout", "") or "") + "\n" + (getattr(proc, "stderr", "") or "")).lower()
    manual = any(m in text for m in _GATEWAY_MANUAL)
    if any(m in text for m in _GATEWAY_RUNNING):
        return "running", manual
    if any(m in text for m in _GATEWAY_STOPPED):
        return "stopped", False
    return "unknown", False


def stale_webhook_routes(prefix: str, run: Any = None) -> List[str]:
    """`<prefix>-webhook` and `<prefix>-webhook-notice` when `hermes webhook list` shows them delivering to `webhook`
    itself (what an earlier setup created when it took the webhook ingress for a chat). Read-only. Any unexpected output
    gives []."""
    run = run or run_hermes
    try:
        proc = run(["webhook", "list"])
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    wanted = (wake_route_name(prefix, "webhook"), notice_route_name(prefix, "webhook"))
    current: Optional[str] = None
    found = set()
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"^\s*\u25c6\s+(\S+)\s*$", line)          # "  ◆ <name>"
        if m:
            current = m.group(1)
            continue
        m = re.match(r"^\s+Deliver:\s*(\S+)", line)               # "    Deliver: <target> [(direct — no agent)]"
        if m and current in wanted and m.group(1) == "webhook":
            found.add(current)
    return [name for name in wanted if name in found]


def webhook_listen(platforms: Optional[Dict[str, Any]]) -> Optional[Tuple[int, str]]:
    """(port, host) the Hermes webhook platform is configured to listen on (`platforms.webhook.extra`), or None."""
    entry = (platforms or {}).get("webhook")
    extra = entry.get("extra") if isinstance(entry, dict) else None
    if not isinstance(extra, dict):
        return None
    port = extra.get("port")
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port.strip())
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    host = extra.get("host")
    return port, host.strip() if isinstance(host, str) else ""


def derived_webhook_url(port: int, host: str, route: str) -> str:
    """The `webhook_url` for a webhook platform on `port`: localhost unless it is bound to one specific other address."""
    if host in ("", "0.0.0.0", "::", "127.0.0.1", "localhost", "::1"):
        host = "localhost"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/webhooks/{route}"


def _url_port(url: str) -> Optional[int]:
    try:
        parts = urlsplit(url)
        return parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None


def _is_windows() -> bool:
    return sys.platform == "win32"


def earlier_setup_test_reviews() -> List[str]:
    """Ids of the open setup test reviews an earlier run left: the title, the brief and the mode all match, so no other
    review is ever touched. [] when the store cannot be read."""
    try:
        rows = [r for r in api.list_reviews() if r.get("title") == TEST_TITLE and r.get("mode") == "local"
                and r.get("status") == "open"]
        found = []
        with owner.bound(owner.resolve_soft()):
            for row in rows:
                if store.load(row["review_id"]).get("brief") == SETUP_BRIEF:
                    found.append(row["review_id"])
        return found
    except (ReviewError, settings.SettingsError, OSError, ValueError, KeyError):
        return []


def existing_secret(run: Any = None) -> Tuple[Optional[str], str]:
    """Reuse an already-stored secret: `hermes config get` (masked, presence) then `--raw` (the value). The value is
    returned for reuse but must never be printed."""
    run = run or run_hermes
    presence = run(["config", "get", SECRET_NAME])
    if presence.returncode != 0:
        return None, "no existing secret"
    raw = run(["config", "get", "--raw", SECRET_NAME])
    if raw.returncode == 0 and raw.stdout.strip():
        return raw.stdout.strip(), "reusing the existing secret"
    return None, "secret is set but not readable"


# ---------------------------------------------------------------- request + plan

@dataclass
class Request:
    yes: bool = False
    dry_run: bool = False
    hosting: str = "local"
    hosting_explicit: bool = False
    port_explicit: bool = False
    origin_platforms: List[str] = field(default_factory=list)
    port: int = 8790
    webhook_route: str = "mock-review"
    autostart: bool = True
    plugin_dir: str = ""
    skip_validate: bool = False
    skip_test_review: bool = False


def _resolve(args: argparse.Namespace) -> Request:
    port = args.port if args.port is not None else int(settings.get("port"))
    route = args.webhook_route if args.webhook_route is not None else str(settings.get("webhook_route"))
    return Request(
        yes=bool(args.yes),
        dry_run=bool(args.dry_run),
        hosting=args.hosting or "local",
        hosting_explicit=args.hosting is not None,
        port_explicit=args.port is not None and int(args.port) != int(settings.get("port")),
        origin_platforms=list(args.origin_platform or []),
        port=int(port),
        webhook_route=str(route),
        autostart=not args.no_autostart,
        plugin_dir=args.plugin_dir or str(PLUGIN_DIR),
        skip_validate=bool(args.skip_validate),
        skip_test_review=bool(args.skip_test_review),
    )


def _ask_yes(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    try:
        ans = input(prompt)
    except EOFError:
        return default
    ans = ans.strip().lower()
    if not ans:
        return default
    return ans.startswith("y")


def _ask_text(prompt: str) -> str:
    if not sys.stdin.isatty():
        return ""
    try:
        return input(prompt)
    except EOFError:
        return ""


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    c = sub.add_parser("setup", help="onboard mock-review: secret, wake and notice routes, hosting, autostart, a test review")
    c.add_argument("-y", "--yes", action="store_true", help="non-interactive: accept defaults and make the changes")
    c.add_argument("--dry-run", action="store_true", help="print the whole plan and change nothing")
    c.add_argument("--hosting", choices=HOSTING_CHOICES, default=None,
                   help="how reviews are hosted (default: local)")
    c.add_argument("--origin-platform", action="append", metavar="NAME",
                   help="origin platform to create the wake and notice routes for (repeatable); default: detected platforms")
    c.add_argument("--port", type=int, default=None, help="board port (default: the port setting)")
    c.add_argument("--webhook-route", default=None, help="route prefix (default: the webhook_route setting)")
    c.add_argument("--no-autostart", action="store_true", help="leave autostart off")
    c.add_argument("--plugin-dir", default=None, help="plugin dir to validate (default: the installed plugin dir)")
    c.add_argument("--skip-validate", action="store_true", help="skip `hermes plugins validate`")
    c.add_argument("--skip-test-review", action="store_true", help="skip the test review")
    return c


# ---------------------------------------------------------------- the wizard

def _will_change(text: str) -> None:
    print(f"will change: {text}")


def _unique(names: List[str]) -> List[str]:
    return list(dict.fromkeys(names))


def _several_message(names: List[str]) -> str:
    return (f"several chat platforms are configured ({', '.join(names)}) and setup cannot ask here. Pick where to wake "
            f"you with --origin-platform, for example: hermes mockreview setup --origin-platform {names[0]} "
            "(repeatable)")


def choose_origins(req: Request, detected: List[str]) -> Optional[List[str]]:
    """The origin platforms for this run, or None after printing why setup stops. Changes nothing."""
    if req.origin_platforms:
        names = _unique(req.origin_platforms)
        bad = [n for n in names if not PLATFORM_RE.fullmatch(n)]
        if bad:
            print(f"origin platform name not valid: {', '.join(bad)} (lowercase letters, digits, - and _). "
                  "Example: hermes mockreview setup --origin-platform telegram (repeatable)")
            return None
        return names
    if not detected:
        print(NO_PLATFORM_MESSAGE)
        return None
    if len(detected) == 1:
        print(f"origin platform: {detected[0]} (the only chat platform configured; pass --origin-platform to choose "
              "another)")
        return list(detected)
    if req.yes or req.dry_run:
        print(_several_message(detected))
        return None
    listed = ", ".join(detected)
    for _ in range(2):
        answer = _ask_text(f"chat platforms found: {listed}. Which do you review on? (comma-separated, Enter = all) ")
        picked = _unique([p.strip().lower() for p in answer.split(",") if p.strip()])
        if not picked:
            return list(detected)
        unknown = [p for p in picked if p not in detected]
        if not unknown:
            return picked
        print(f"not in the list: {', '.join(unknown)} (choose from {listed})")
    print(_several_message(detected))
    return None


def next_steps(*, board_running: bool, autostart: bool, gateway: str, restart_for: List[str],
               windows: bool) -> List[str]:
    """The `next:` lines, one string each. `gateway` is "running", "stopped" or "unknown"."""
    start_gateway = "hermes gateway run (or hermes gateway start for the installed service)"
    lines: List[str] = []
    if board_running:
        if restart_for:
            lines.append("restart the process that runs the board (the gateway, or hermes mockreview serve) to apply "
                         f"the changed setting(s): {', '.join(restart_for)}")
        if gateway == "stopped":
            lines.append(f"wake-ups need the gateway: start it with: {start_gateway}")
    elif windows:
        lines.append(f"run {FOREGROUND_CMD} in a terminal (on Windows the board runs in the foreground)")
        if gateway == "stopped":
            lines.append(f"wake-ups need the gateway: start it with: {start_gateway}")
    elif not autostart:
        lines.append(f"run {FOREGROUND_CMD}")
        if gateway == "stopped":
            lines.append(f"wake-ups need the gateway: start it with: {start_gateway}")
    elif gateway == "running":
        lines.append("the board starts with the gateway: restart it (hermes gateway restart; a gateway you started "
                     f"with hermes gateway run: stop it and run it again), or run {FOREGROUND_CMD}")
    elif gateway == "stopped":
        lines.append("the board starts with the gateway: start it (hermes gateway run, or hermes gateway start for "
                     f"the installed service), or run {FOREGROUND_CMD}")
        lines.append("wake-ups need the gateway either way")
    else:
        lines.append("start or restart the gateway (hermes gateway start, hermes gateway restart, or hermes gateway "
                     f"run) or run {FOREGROUND_CMD}")
    lines.append("then ask your agent for a design review: that is the real test of the wake-up")
    return lines


def run(args: argparse.Namespace) -> int:
    """The wizard. A hermes call that hangs or cannot start ends the run with one line and exit 1, never a traceback."""
    try:
        return _run(args)
    except HermesCallError as e:
        what = f"hermes {e.words}".strip()
        if e.started:
            print(f"{what} did not answer within {e.timeout} s")
        else:
            print(f"cannot run {what} ({e.binary})")
        print("the steps before it were applied; setup is safe to re-run")
        return 1


def _run(args: argparse.Namespace) -> int:
    req = _resolve(args)

    if not req.yes and not req.dry_run and not sys.stdin.isatty():
        print(NON_TTY_USAGE)
        return 2

    # 0. preflight: Python version (pure, before any subprocess)
    if not python_ok():
        print(f"mock-review setup needs Python >= 3.9 "
              f"(running {'.'.join(map(str, sys.version_info[:3]))}). Upgrade Python, then re-run.")
        return 2

    failed = False
    summary: List[Tuple[str, str, str]] = []

    # 1. preflight: Hermes version
    try:
        ver_proc = run_hermes(["--version"], timeout=VERSION_TIMEOUT)
    except HermesCallError as e:
        if e.started:
            print(f"hermes --version did not answer within {VERSION_TIMEOUT} s (on the first run in a Hermes home it "
                  "checks for updates); re-run setup, it is safe to repeat")
        else:
            print(f"cannot run hermes ({e.binary}): install Hermes or set MOCKREVIEW_HERMES_BIN")
        return 2
    version = parse_version((ver_proc.stdout or "") + "\n" + (ver_proc.stderr or ""))
    if version is None or version < MIN_VERSION:
        shown = (ver_proc.stdout or "").strip().splitlines()
        shown = shown[0] if shown else "unknown"
        print(f"Hermes {shown!r} is too old or unreadable: mock-review setup requires Hermes >= 0.22.0. "
              "Upgrade Hermes, then re-run setup.")
        return 2

    # 1. preflight: where to wake the reviewer. Settled before anything is changed, so a stop leaves the home untouched.
    platforms_cfg, _ = platforms_config()
    platforms = choose_origins(req, chat_platforms(platforms_cfg))
    if platforms is None:
        return 2

    secret = ""
    secret_new = False

    # 1. preflight: webhook platform enabled
    webhook_ok, _ = webhook_enabled()
    if not webhook_ok:
        print(f"the webhook platform is disabled. Enable it with: {WEBHOOK_ENABLE_CMD}")
        do_enable = req.yes if (req.yes or req.dry_run) else _ask_yes("enable the webhook platform now? [Y/n] ", True)
        if do_enable and not req.dry_run:
            _will_change("enable the webhook platform")
            r = run_hermes(["config", "set", WEBHOOK_ENABLED_KEY, "true"])
            print(redact((r.stdout + r.stderr).strip(), secret))
            webhook_ok = True

    # 1. preflight: who holds the port (a warning only, and none for this plugin's own board)
    board_kind, board_info = board_identity(req.port)
    board_running = board_kind == "ours"
    if board_kind == "ours":
        print(f"board: already running on http://127.0.0.1:{req.port} (this plugin's board, pid {board_info.get('pid')})")
    elif board_kind == "other-mock-review":
        print(f"warning: port {req.port} is held by a mock-review board for another Hermes home "
              f"({board_info.get('hermes_home')}); this home's board cannot start there. "
              "Pick another port with --port.")
    else:
        free, port_note = port_free(req.port)
        if not free:
            print(f"warning: port {req.port} is busy ({port_note}); the board will not start there until it is freed.")
    summary.append(("preflight", "ok", f"hermes {'.'.join(map(str, version))}"))

    # 2. secret: reuse or generate + write
    secret, secret_note = existing_secret()
    if secret:
        print(f"secret: {secret_note} (value not printed)")
        summary.append(("secret", "ok", "reused"))
    else:
        if secret_note and secret_note != "no existing secret":
            print(f"note: {secret_note}; the old value is being replaced "
                  f"(any route using the old secret is re-created)")
        secret = stdlib_secrets.token_urlsafe(32)
        secret_new = True
        _will_change("generate and store a new webhook secret")
        if not req.dry_run:
            r = run_hermes(["config", "set", SECRET_NAME, secret])
            print(redact((r.stdout + r.stderr).strip(), secret))
        summary.append(("secret", "ok", "new"))

    # 3. routes: one per origin platform
    def subscribe(name: str, argv: List[str], what: str) -> str:
        """Create or update one route; returns "ok", "skipped" (webhook platform disabled) or "failed"."""
        _will_change(f"subscribe webhook route {name} ({what})")
        if req.dry_run:
            return "ok"
        r = run_hermes(argv)
        out_txt = redact((r.stdout + r.stderr).strip(), secret)
        print(out_txt)
        lowered = out_txt.lower()
        if r.returncode != 0:
            return "failed"
        if "disabled" in lowered or "subscription" not in lowered:
            print(f"route {name} was not created (webhook platform disabled)")
            return "skipped"
        return "ok"

    results = {"routes": [], "notice routes": []}
    for platform in platforms:
        results["routes"].append(subscribe(wake_route_name(req.webhook_route, platform),
                                           wake_route_argv(req.webhook_route, platform, secret),
                                           f"deliver {platform}"))
        results["notice routes"].append(subscribe(notice_route_name(req.webhook_route, platform),
                                                  notice_route_argv(req.webhook_route, platform, secret),
                                                  f"deliver {platform}, notice"))
    for label, outcomes in results.items():
        if req.dry_run:
            summary.append((label, "planned", f"{len(outcomes)} route(s)"))
        elif "failed" in outcomes:
            summary.append((label, "failed", f"{outcomes.count('failed')} of {len(outcomes)} route(s)"))
        elif "ok" in outcomes:
            summary.append((label, "ok", f"{outcomes.count('ok')} route(s)"))
        else:
            summary.append((label, "skipped", "webhook platform disabled"))
    failed = failed or any("failed" in o for o in results.values())

    # 3b. a self-delivering route from an earlier setup that took the webhook ingress for a chat
    stale = [] if "webhook" in platforms else stale_webhook_routes(req.webhook_route)
    if stale:
        names = ", ".join(stale)
        if req.yes or req.dry_run:
            print(f"note: the self-delivering route(s) {names} (Deliver: webhook) were left by an earlier setup. "
                  "Setup never removes a route unattended; remove them with:")
            for name in stale:
                print(f"  hermes webhook remove {name}")
            summary.append(("stale routes", "skipped", f"{len(stale)} left"))
        elif _ask_yes(f"remove the self-delivering route(s) {names} left by an earlier setup? [Y/n] ", True):
            removed_ok = True
            for name in stale:
                _will_change(f"remove webhook route {name}")
                r = run_hermes(["webhook", "remove", name])
                print(redact((r.stdout + r.stderr).strip(), secret))
                removed_ok = removed_ok and r.returncode == 0
            failed = failed or not removed_ok
            summary.append(("stale routes", "ok" if removed_ok else "failed", f"removed {len(stale)}"))
        else:
            summary.append(("stale routes", "skipped", f"{len(stale)} left"))

    # 3c. a webhook platform on a non-default port: point webhook_url at it unless the owner set webhook_url
    listen = webhook_listen(platforms_cfg)
    if listen:
        wport, whost = listen
        current, source, _ = settings.resolve_one("webhook_url")
        derived = derived_webhook_url(wport, whost, req.webhook_route)
        if source == "default":
            if current.rstrip("/") != derived:
                _will_change(f"set webhook_url to {derived} (the webhook platform listens on port {wport})")
                if not req.dry_run:
                    r = run_hermes(["config", "set", f"{SETTINGS}.webhook_url", derived])
                    print(redact((r.stdout + r.stderr).strip(), secret))
                summary.append(("webhook_url", "ok", derived))
        elif _url_port(current) != wport:
            print(f"warning: webhook_url is {current} (port {_url_port(current)}) but the Hermes webhook platform "
                  f"listens on port {wport}, so wake-ups would miss it. To follow the platform: "
                  f"hermes config set {SETTINGS}.webhook_url {derived}")

    # 4. hosting — recommend tailnet when the CLI is usable and the user hasn't chosen (interactive only)
    changed_at_start: List[str] = []   # settings the running board reads only when it starts (host, public_base)
    tailnet_pre: Optional[str] = None
    if not req.hosting_explicit and not req.yes and not req.dry_run and sys.stdin.isatty():
        base, note = tailnet_base()
        if base:
            host = base.split("//", 1)[1] if "//" in base else base
            if _ask_yes(f"Tailscale is up on {host}; tailnet hosting keeps reviews off the public internet — "
                        "use it? [Y/n] ", True):
                req.hosting = "tailnet"
                tailnet_pre = base
        elif note:
            print(f"tailnet unavailable ({note}) — staying on local hosting")

    if req.hosting == "tailnet":
        base, note = (tailnet_pre, "") if tailnet_pre else tailnet_base()
        if base:
            _will_change(f"set public_base to {base}")
            if not req.dry_run:
                r = run_hermes(["config", "set", f"{SETTINGS}.public_base", base])
                print(redact((r.stdout + r.stderr).strip(), secret))
                changed_at_start.append("public_base")
            print(f"then run once, manually: tailscale serve --bg {req.port}")
        else:
            print(f"tailnet unavailable ({note}) — staying on local hosting")
    elif req.hosting == "lan":
        print("warning: " + LAN_WARNING)
        _will_change("set host to 0.0.0.0")
        if not req.dry_run:
            r = run_hermes(["config", "set", f"{SETTINGS}.host", "0.0.0.0"])
            print(redact((r.stdout + r.stderr).strip(), secret))
            changed_at_start.append("host")
    elif req.hosting == "herenow":
        print(f"here.now hosting: install the official skill with: {HERENOW_INSTALL_CMD}")
        print(f"then set the key: {HERENOW_KEY_CMD}  (setup never installs the skill or writes the key)")
    else:
        print("hosting: local (nothing written)")
    summary.append(("hosting", "ok", req.hosting))

    # 4b. a --port that differs from the setting is saved, so the board, the test review and the
    # links all use it (otherwise --port only changed the preflight check)
    if req.port_explicit:
        _will_change(f"set the board port to {req.port}")
        if not req.dry_run:
            r = run_hermes(["config", "set", f"{SETTINGS}.port", str(req.port)])
            print(redact((r.stdout + r.stderr).strip(), secret))
            # not tracked in changed_at_start: a board that answers on this port needs no restart for it, and one that
            # does not has no restart advice to give beyond the start advice

    # 5. autostart
    if req.autostart:
        _will_change("enable autostart")
        if not req.dry_run:
            r = run_hermes(["config", "set", f"{SETTINGS}.autostart", "true"])
            print(redact((r.stdout + r.stderr).strip(), secret))
        summary.append(("autostart", "ok", ""))
    else:
        print("autostart left off (--no-autostart)")
        summary.append(("autostart", "skipped", ""))

    # 6. migration: unset obsolete settings
    removed: List[str] = []
    for key in OBSOLETE_KEYS:
        g = run_hermes(["config", "get", "--json", f"{SETTINGS}.{key}"])
        if g.returncode == 0:
            try:
                val = json.loads(g.stdout or "")
            except ValueError:
                val = (g.stdout or "").strip()
            if val not in (None, "", {}, []):
                removed.append(key)
    for key in removed:
        _will_change(f"remove obsolete setting {key}")
        if not req.dry_run:
            r = run_hermes(["config", "unset", f"{SETTINGS}.{key}"])
            print(redact((r.stdout + r.stderr).strip(), secret))
    summary.append(("migration", "ok" if removed else "skipped", ", ".join(removed)))

    # 7. validate
    if req.skip_validate:
        print("validate: skipped (--skip-validate)")
        summary.append(("validate", "skipped", ""))
    else:
        _will_change(f"validate the plugin (hermes plugins validate {req.plugin_dir})")
        if req.dry_run:
            summary.append(("validate", "ok", ""))
        else:
            r = run_hermes(["plugins", "validate", req.plugin_dir], timeout=90)
            for line in (r.stdout + r.stderr).strip().splitlines()[-8:]:
                if line.strip():
                    print(redact(line, secret))
            if r.returncode != 0:
                failed = True
                summary.append(("validate", "failed", ""))
            else:
                summary.append(("validate", "ok", ""))

    # 8. test review
    if req.skip_test_review:
        print("test review: skipped (--skip-test-review)")
        summary.append(("test-review", "skipped", ""))
    else:
        earlier = earlier_setup_test_reviews()
        if earlier:
            _will_change(f"close {len(earlier)} earlier setup test review(s)")
            if not req.dry_run:
                for review_id in earlier:
                    try:
                        api.close(review_id)
                    except (ReviewError, settings.SettingsError, OSError, ValueError):
                        print(f"note: could not close the earlier setup test review {review_id}")
        _will_change("create a test review (wake off, no origin)")
        if req.dry_run:
            summary.append(("test-review", "ok", ""))
        else:
            tmpdir = tempfile.mkdtemp(prefix="mockreview-setup-")
            try:
                dec = Path(tmpdir) / "decisions.json"
                dec.write_text(json.dumps({"blocks": [{"type": "text", "title": "Setup", "text": TEST_TITLE}]}), encoding="utf-8")
                try:
                    res = api.create(title=TEST_TITLE, origin=None, brief=SETUP_BRIEF,
                                     mode="local", decisions=str(dec), wake=False)
                except (ReviewError, settings.SettingsError) as e:
                    msg = getattr(e, "message", None) or str(e)
                    print(f"test review pending: {msg}")
                    summary.append(("test-review", "pending", "not created"))
                else:
                    review_id = res.get("review_id")
                    url = res.get("url") or (config.local_base() + "/r/" + review_id if review_id else "")
                    if review_id and board_loads(req.port, review_id):
                        board_running = True
                        print(f"test review created: {review_id}")
                        print(f"board url: {url}")
                        summary.append(("test-review", "ok", ""))
                    else:
                        print(f"test review saved: {review_id}; it will load once the board runs")
                        summary.append(("test-review", "pending", "board not running"))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

    # final summary
    print("setup summary:")
    if summary:
        width = max(len(label) for label, _, _ in summary)
        for label, status, note in summary:
            line = f"  {label.ljust(width)}  {status}"
            if note:
                line += f"  ({note})"
            print(line)

    # the next step, once, after everything is known
    if req.dry_run:
        print("next: run the same command without --dry-run to make these changes")
    else:
        gateway, _manual = gateway_state()
        for line in next_steps(board_running=board_running, autostart=req.autostart, gateway=gateway,
                               restart_for=changed_at_start if board_kind == "ours" else [], windows=_is_windows()):
            print(f"next: {line}")

    return 1 if failed else 0
