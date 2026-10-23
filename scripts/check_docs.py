#!/usr/bin/env python3
"""Check the docs against the tree and run every documented command on a disposable Hermes home.

    python3 scripts/check_docs.py            # prints one line per check and a summary; exit 1 on any FAIL
    python3 scripts/check_docs.py --repo-slug owner/repo   # the repository's own address is not an identifier

What it does:
  1. Links, images and repo paths named in the docs must exist and, in a git checkout, be tracked (so they ship).
  2. Every `hermes ...` command written in the docs resolves (`--help` exits 0) and every flag shown with it exists.
  3. The docs and the scripts pass the identifier sweep of scripts/check_identifiers.py: its generic denylist, plus the
     private tokens in MOCKREVIEW_SWEEP_TOKENS when that is set.
  4. The decisions.json and design.json examples in docs/decisions-format.md pass the plugin's own validators.
  5. The generated pages are current (scripts/gen_docs_reference.py --check).
  6. The documented commands run against a throwaway HERMES_HOME created next to the real one, with the plugin copied
     in: setup, settings, owner, design, create, feedback, add-round, list, close, housekeeping, poll, notify-test
     against a local stand-in receiver, and `serve` in the foreground. Nothing touches the real Hermes home, the live
     gateway or the webhook port. The throwaway home and every process this script starts are removed at the end.

Needs the `hermes` CLI (0.22.0 or newer) on PATH.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_identifiers  # noqa: E402
from mockreview import cli, settings, store  # noqa: E402

DOC_FILES = ["README.md", "CHANGELOG.md", "AGENTS.md", "CLAUDE.md", "skills/usage/SKILL.md",
             "docs/decisions-format.md", "docs/hosting.md", "docs/security.md", "docs/troubleshooting.md"]
GENERATED = ["docs/settings.md", "docs/cli.md"]
PLUGIN_FILES = ("__init__.py", "plugin.yaml", "mockreview", "skills")
PATH_EXT = (".py", ".md", ".png", ".json", ".yaml", ".html", ".css", ".js", ".sh")
PATH_ROOTS = ("docs/", "scripts/", "mockreview/", "skills/", "tests/", "bin/", "examples/")
SCRIPT_FILES = ["scripts/gen_docs_reference.py", "scripts/gen_plugin_manifest.py", "scripts/check_docs.py",
                "scripts/check_identifiers.py"]
REPO_SLUG: Optional[str] = None

results: List[Tuple[str, str, str]] = []


def record(status: str, label: str, detail: str = "") -> None:
    results.append((status, label, detail))
    print(f"{status:4} {label}" + (f"  [{detail}]" if detail else ""))
    sys.stdout.flush()


def doc_paths() -> List[Path]:
    return [ROOT / f for f in DOC_FILES if (ROOT / f).exists()]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


def tracked_paths() -> Optional[set]:
    """Every file git tracks, and their parent directories; None outside a git checkout."""
    try:
        proc = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, timeout=60)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = set()
    for name in filter(None, proc.stdout.decode("utf-8", "replace").split("\0")):
        parts = name.split("/")
        out.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


def ships(path: Path, tracked: Optional[set]) -> bool:
    if not path.exists():
        return False
    if tracked is None:
        return True
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return False
    return rel in tracked


# ---------------------------------------------------------------------------------------------- static checks

def check_links_and_paths() -> None:
    bad: List[str] = []
    n = 0
    tracked = tracked_paths()
    for p in doc_paths() + [ROOT / g for g in GENERATED]:
        text = read(p)
        for m in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)\)", text):
            target = m.group(1).split("#")[0]
            if not target or re.match(r"[a-z]+:", target):
                continue
            n += 1
            if not ships(p.parent / target, tracked):
                bad.append(f"{p.relative_to(ROOT)} -> {target}")
        if p.name in ("cli.md", "settings.md"):
            continue
        for m in re.finditer(r"`([^`\s]+)`", strip_fences(text)):
            tok = m.group(1).rstrip(".,;:")
            if any(c in tok for c in "<>$~*|") or tok.startswith(("/", "http", "-")) or "." not in tok.split("/")[-1]:
                continue
            if "/" in tok and (tok.startswith(PATH_ROOTS) or tok.endswith(PATH_EXT)):
                if tok.startswith(("plugins.", "platforms.")):
                    continue
                n += 1
                if not ships(ROOT / tok, tracked):
                    bad.append(f"{p.relative_to(ROOT)}: `{tok}`")
        for m in re.finditer(r"`((?:README|CHANGELOG|AGENTS|CLAUDE|plugin|pytest)\.(?:md|yaml|ini)|bin/mockreview)`", text):
            n += 1
            if not ships(ROOT / m.group(1), tracked):
                bad.append(f"{p.relative_to(ROOT)}: `{m.group(1)}`")
    where = "exist and are tracked" if tracked is not None else "exist"
    record("FAIL" if bad else "OK", f"links, images and repo paths {where} ({n} checked)", "; ".join(sorted(set(bad))))


def check_identifiers_in_docs() -> None:
    tokens = check_identifiers.env_tokens()
    rules = check_identifiers.Rules(tokens, check_identifiers.repo_slug(str(ROOT), REPO_SLUG))
    files = doc_paths() + [ROOT / g for g in GENERATED] + [ROOT / f for f in SCRIPT_FILES if (ROOT / f).exists()]
    hits: List[str] = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        hits.extend(f"{rel}:{n} {rule}" for n, rule in rules.check_text(read(p), rel))
    scope = f"{len(tokens)} private token(s)" if tokens else f"generic denylist only, {check_identifiers.ENV_VAR} unset"
    record("FAIL" if hits else "OK", f"identifier sweep = {len(hits)} hit(s) ({len(files)} files, {scope})",
           "; ".join(hits[:8]))


def indented_json(text: str) -> List[dict]:
    """JSON objects shown as 4-space indented code blocks."""
    out = []
    for m in re.finditer(r"^(    \{\n.*?^    \}|    \{[^\n]*\})$", text, flags=re.S | re.M):
        try:
            obj = json.loads("\n".join(line[4:] for line in m.group(1).splitlines()))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def example_blocks() -> List[dict]:
    return [o for o in indented_json(read(ROOT / "docs" / "decisions-format.md")) if o.get("type")]


def design_example() -> Optional[dict]:
    for o in indented_json(read(ROOT / "docs" / "decisions-format.md")):
        if "themes" in o:
            return o
    return None


def check_examples() -> None:
    blocks = example_blocks()
    types = {b["type"] for b in blocks}
    missing = set(store.BLOCK_TYPES) - types
    try:
        store.validate_decisions({"blocks": blocks})
        ok = bool(blocks) and not missing
        detail = f"{len(blocks)} blocks, types {sorted(types)}" + (f", missing {sorted(missing)}" if missing else "")
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)
    record("OK" if ok else "FAIL", "decisions.json examples validate (every block type)", detail)
    design = design_example()
    tmp = Path(tempfile.mkdtemp(prefix="mockreview-docs-design-"))
    try:
        (tmp / "design.json").write_text(json.dumps(design))
        (tmp / "mock.css").write_text("")
        info = store.validate_design(tmp)
        record("OK", "design.json example validates", f"themes {sorted(info['themes'])}")
    except Exception as e:  # noqa: BLE001
        record("FAIL", "design.json example validates", str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_generated() -> None:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_docs_reference.py"), "--check"],
                          capture_output=True, text=True, timeout=300)
    last = (proc.stdout.strip().splitlines() or [proc.stderr.strip()])[-1]
    record("OK" if proc.returncode == 0 else "FAIL", "generated settings.md and cli.md are current", last)


# ---------------------------------------------------------------------------------------------- command syntax

def command_paths() -> List[List[str]]:
    import argparse
    parser = argparse.ArgumentParser()
    cli.add_commands(parser)
    out: List[List[str]] = []

    def walk(p, prefix):
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    out.append(prefix + [name])
                    walk(sub, prefix + [name])
    walk(parser, [])
    return out


def documented_commands() -> List[Tuple[str, str]]:
    """(file, command text) for every `hermes ...` span or indented line in the hand-written docs."""
    found: List[Tuple[str, str]] = []
    for p in doc_paths():
        text = read(p)
        rel = str(p.relative_to(ROOT))
        for m in re.finditer(r"`(hermes [^`]+)`", text):
            found.append((rel, m.group(1)))
        for line in text.splitlines():
            if re.match(r"^    hermes \w", line):
                found.append((rel, line.strip()))
    return found


def resolve(cmd: str, paths: List[List[str]]) -> Tuple[List[str], List[str]]:
    """Split a documented command into (argv prefix for --help, flags)."""
    toks = cmd.replace("`", "").split()
    flags = [re.split(r"[=|]", t)[0] for t in toks if t.startswith("--") and t != "--"]
    words = []
    for t in toks[1:]:
        if t.startswith(("-", "<", "/", "$")) or "=" in t or "." in t or not re.fullmatch(r"[a-z][a-z-]*", t):
            break
        words.append(t)
    if words and words[0] == "mockreview":
        sub: List[str] = []
        for w in words[1:]:
            if any(p[:len(sub) + 1] == sub + [w] for p in paths):
                sub.append(w)
            else:
                break
        return ["hermes", "mockreview"] + sub, flags
    return ["hermes"] + words[:2], flags


def check_command_syntax(env: Dict[str, str]) -> None:
    paths = command_paths()
    seen: Dict[str, Tuple[str, str]] = {}
    for rel, cmd in documented_commands():
        argv, flags = resolve(cmd, paths)
        if argv == ["hermes"]:
            continue
        seen.setdefault(" ".join(argv) + "|" + " ".join(sorted(flags)), (rel, cmd))
    help_cache: Dict[str, Tuple[int, str]] = {}
    failures: List[str] = []
    for key, (rel, cmd) in sorted(seen.items()):
        argv_s, flags_s = key.split("|")
        argv = argv_s.split()
        if argv_s not in help_cache:
            proc = subprocess.run(argv + ["--help"], env=env, capture_output=True, text=True, timeout=120)
            help_cache[argv_s] = (proc.returncode, proc.stdout + proc.stderr)
        rc, out = help_cache[argv_s]
        if rc != 0:
            failures.append(f"{rel}: `{cmd}` -> {argv_s} --help exit {rc}")
            continue
        for f in flags_s.split():
            if f != "--help" and f not in out:
                failures.append(f"{rel}: `{cmd}` -> {f} not in `{argv_s} --help`")
    record("FAIL" if failures else "OK",
           f"documented hermes commands and flags resolve ({len(seen)} distinct, {len(help_cache)} help pages)",
           "; ".join(failures))
    slash = set()
    for p in doc_paths():
        for m in re.finditer(r"`/mockreview ([a-z|<> _]+)`", read(p)):
            slash.update(w for w in re.split(r"[|\s]+", m.group(1)) if re.fullmatch(r"[a-z]+", w))
    src = read(ROOT / "__init__.py")
    bad = sorted(w for w in slash if f'cmd == "{w}"' not in src)
    record("FAIL" if bad else "OK", f"documented /mockreview subcommands exist ({sorted(slash)})", ", ".join(bad))


# ---------------------------------------------------------------------------------------------- live run

class Receiver(http.server.BaseHTTPRequestHandler):
    hits: List[Tuple[str, Dict[str, str], bytes]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Receiver.hits.append((self.path, dict(self.headers), body))
        out = json.dumps({"status": "accepted"}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def install_plugin(home: Path) -> None:
    plugin = home / "plugins" / "mock-review"
    plugin.mkdir(parents=True)
    for name in PLUGIN_FILES:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, plugin / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copyfile(src, plugin / name)


class Live:
    def __init__(self, home: Path):
        self.home = home
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MOCKREVIEW_")}
        self.env.update(HERMES_HOME=str(home), COLUMNS="80", NO_COLOR="1")
        self.output: List[str] = []

    def run(self, argv: List[str], expect: Tuple[int, ...] = (0,), label: Optional[str] = None,
            timeout: int = 180) -> subprocess.CompletedProcess:
        proc = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout)
        self.output.append(proc.stdout + proc.stderr)
        shown = " ".join(argv).replace(str(ROOT), ".")
        ok = proc.returncode in expect
        tail = "" if ok else "; " + (proc.stderr.strip()[-200:] or proc.stdout.strip()[-200:])
        record("OK" if ok else "FAIL", "ran: " + (label or shown), f"exit {proc.returncode}{tail}")
        return proc


def http_get(url: str, timeout: float = 5.0) -> Tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode()


def live_run(tmp_base: Path) -> None:
    home = Path(tempfile.mkdtemp(prefix="mockreview-docs-check-", dir=tmp_base))
    work = Path(tempfile.mkdtemp(prefix="mockreview-docs-work-"))
    procs: List[subprocess.Popen] = []
    recv = None
    try:
        install_plugin(home)
        L = Live(home)
        port, rport = free_port(), free_port()
        recv = http.server.ThreadingHTTPServer(("127.0.0.1", rport), Receiver)
        threading.Thread(target=recv.serve_forever, daemon=True).start()
        hook = f"http://127.0.0.1:{rport}/webhooks/mock-review"
        H = ["hermes", "mockreview"]

        L.run(["hermes", "--version"])
        L.run(["hermes", "plugins", "enable", "mock-review"])
        L.run(H + ["settings"])
        L.run(H + ["settings", "--json"])
        L.run(H + ["setup", "--dry-run", "--yes", "--hosting", "local", "--origin-platform", "telegram",
                   "--port", str(port)])
        L.run(H + ["setup", "--yes", "--hosting", "local", "--origin-platform", "telegram", "--port", str(port)],
              expect=(0, 1), label="hermes mockreview setup --yes ... (first run)")
        wl = L.run(["hermes", "webhook", "list"])
        record("OK" if "mock-review-telegram" in wl.stdout else "FAIL", "setup created the route mock-review-telegram")
        L.run(["hermes", "config", "set", "plugins.entries.mock-review.settings.webhook_url", hook],
              label="hermes config set ...settings.webhook_url <stand-in receiver>")

        # the server, in the foreground, as documented
        serve = subprocess.Popen(H + ["serve"], env=L.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
        procs.append(serve)
        up = False
        for _ in range(60):
            try:
                if http_get(f"http://127.0.0.1:{port}/healthz")[0] == 200:
                    up = True
                    break
            except OSError:
                time.sleep(0.5)
        record("OK" if up else "FAIL", f"ran: hermes mockreview serve (healthz answers on port {port})")

        rep = L.run(H + ["setup", "--yes", "--hosting", "local", "--origin-platform", "telegram", "--port", str(port)],
                    label="hermes mockreview setup --yes ... (second run, board now running)")
        record("OK" if "test review created" in rep.stdout else "FAIL", "second setup run created the test review",
               (rep.stdout.strip().splitlines() or [""])[-1][:120])

        L.run(H + ["owner"])
        L.run(H + ["design", "list"])
        profile = work / "profile"
        profile.mkdir()
        (profile / "design.json").write_text(json.dumps(design_example()))
        (profile / "mock.css").write_text("")
        L.run(H + ["design", "save", "acme", str(profile)])
        L.run(H + ["design", "show", "acme"])
        L.run(H + ["design", "list"], label="hermes mockreview design list (after save)")

        dec = work / "decisions.json"
        dec.write_text(json.dumps({"intro": "Check", "blocks": example_blocks()}))
        cr = L.run(H + ["create", "--title", "Docs check", "--platform", "telegram", "--chat-id", "check-chat",
                        "--mode", "local", "--design", "acme", "--decisions", str(dec)])
        rid = ""
        try:
            rid = json.loads(cr.stdout)["review_id"]
        except (ValueError, KeyError):
            record("FAIL", "create returned a review_id")
        if rid:
            record("OK", "create returned a review_id and url", json.loads(cr.stdout).get("url", ""))
            L.run(H + ["feedback", rid])
            L.run(H + ["add-round", rid, "--decisions", str(dec)])
            L.run(H + ["list"])
            L.run(H + ["list", "--all"])
            status, body = http_get(f"http://127.0.0.1:{port}/r/{rid}/state.json")
            record("OK" if status == 200 and json.loads(body)["id"] == rid else "FAIL",
                   "the board serves the review's state.json")
            L.run(H + ["notify-test", rid, "--to", hook + "-telegram"])
            sig = bool(Receiver.hits) and "X-Webhook-Signature-V2" in Receiver.hits[-1][1]
            record("OK" if sig else "FAIL", "notify-test delivered a signed request to the stand-in receiver")
            L.run(H + ["housekeeping"])
            L.run(H + ["poll"])
            L.run(H + ["close", rid])

        L.run([sys.executable, str(ROOT / "bin" / "mockreview"), "settings"], label="bin/mockreview settings")
        L.run(["hermes", "config", "set", "platforms.webhook.enabled", "true"])
        L.run(["hermes", "config", "set", "plugins.entries.mock-review.settings.host", "0.0.0.0"])
        L.run(["hermes", "config", "set", "plugins.entries.mock-review.settings.public_base",
               "https://machine.example.test"], label="hermes config set ...settings.public_base <example address>")
        L.run(["hermes", "config", "set", "HERENOW_API_KEY", "docs-check-placeholder"],
              label="hermes config set HERENOW_API_KEY <placeholder>")

        secret = ""
        env_file = home / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("MOCKREVIEW_WEBHOOK_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip("'\"")
        joined = "\n".join(L.output)
        record("OK" if secret and secret not in joined else "FAIL",
               f"webhook secret is in the home's .env and absent from all {len(L.output)} command outputs")
        mode = oct(env_file.stat().st_mode & 0o777) if env_file.exists() else "missing"
        record("OK" if mode == "0o600" else "FAIL", f".env permissions are {mode}")
        record("OK", "documented, not run (needs network, a live gateway or an external tool): hermes plugins install, "
                     "hermes gateway restart, hermes skills install, hermes mockreview publish, hermes mockreview adopt, "
                     "tailscale; their syntax is checked above")
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        if recv is not None:
            recv.shutdown()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        record("OK" if not home.exists() and not work.exists() else "FAIL", "removed the throwaway home and work dirs")


def main(argv: Optional[List[str]] = None) -> int:
    global REPO_SLUG
    import argparse
    ap = argparse.ArgumentParser(description="Check the docs against the tree.")
    ap.add_argument("--repo-slug", help="owner/repo whose own address is not an identifier (default: GITHUB_REPOSITORY "
                                        "or the origin remote)")
    REPO_SLUG = ap.parse_args(argv).repo_slug
    if shutil.which("hermes") is None:
        print("error: the hermes CLI is not on PATH", file=sys.stderr)
        return 2
    check_links_and_paths()
    check_identifiers_in_docs()
    check_examples()
    check_generated()
    base = settings.default_hermes_home()
    env = dict(os.environ, COLUMNS="80", NO_COLOR="1")
    probe = Path(tempfile.mkdtemp(prefix="mockreview-docs-help-", dir=base))
    try:
        install_plugin(probe)
        env["HERMES_HOME"] = str(probe)
        subprocess.run(["hermes", "plugins", "enable", "mock-review"], env=env, capture_output=True, timeout=120)
        check_command_syntax(env)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    live_run(base)
    fails = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(fails)} OK, {len(fails)} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
