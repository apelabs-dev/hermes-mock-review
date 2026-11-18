"""The running board server's address, for another process that writes through it.

A stdio MCP server is a separate process: ``threads.reply`` there persists an event, but ``live.publish`` only reaches
subscriptions of its own process, so an open board never sees the frame. The board server therefore leaves a descriptor
next to the store it watches, and an MCP process with the same store routes its writes back over loopback with the
caller's personal access token. The server process then does the DB write and the live publish under its own publish
lock, exactly like a board request.

The descriptor is the only rendezvous: no fixed port is assumed (tests bind an ephemeral one). It never holds a token,
but it does hold one per-start secret, written into a file only its owner can read, and ``verify`` proves the listener
knows that secret before a personal access token is sent. A listener that merely bound the port named in a stale
descriptor cannot answer the challenge, so the token never goes to an impostor, and a descriptor that is a symlink, is
readable by another account or was not written by this account is never trusted at all.

HTTP goes through ``http.client``, never ``urllib``: no proxy from the environment is consulted and no redirect is
followed, so a personal access token never leaves loopback and a 30x answer is never mistaken for a write.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets as pysecrets
import stat
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from . import config, store

NAME = ".server.json"
SERVICE = "mock-review"
API = 1
USER_AGENT = "mockreview-mcp/2"
CHALLENGE_PATH = "/api/owner/challenge"
SECRET_BYTES = 32
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")


def new_secret() -> str:
    """One random secret per server start: the proof a live server can give and a port squatter cannot."""
    return pysecrets.token_urlsafe(SECRET_BYTES)


def mac(secret: str, nonce: str) -> str:
    """HMAC-SHA256 of the client's nonce under the descriptor's secret. The secret itself never leaves the host."""
    return hmac.new(secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _base_for(host: Any, port: int) -> str:
    h = str(host or "")
    if h in ("0.0.0.0", ""):
        h = "127.0.0.1"
    elif h == "::":
        h = "::1"
    return f"http://[{h}]:{int(port)}" if ":" in h else f"http://{h}:{int(port)}"


def _connection(base: str, timeout: float) -> http.client.HTTPConnection:
    parts = urllib.parse.urlsplit(base)
    if parts.scheme != "http" or not parts.hostname:
        raise OSError("not a loopback http base")
    return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)


def publish(server: Any) -> Path:
    """Write this server's descriptor into the store it owns, and hand the server its secret. Returns the path."""
    address = getattr(server, "server_address", None) or ("127.0.0.1", 0)
    host, port = str(address[0]), int(address[1])
    root = config.own_data_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / NAME
    secret = new_secret()
    body = {
        "api": API,
        "service": SERVICE,
        "pid": os.getpid(),
        "base": _base_for(host, port),
        "host": host,
        "port": port,
        "data_root": str(root),
        "started_at": time.time(),
        "secret": secret,
    }
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".tmp-server-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    setattr(server, "runtime_secret", secret)
    return path


def withdraw(server: Any) -> None:
    """Remove the descriptor, but only when it still names this process and this server's port."""
    address = getattr(server, "server_address", None) or ("", 0)
    try:
        port = int(address[1])
    except (TypeError, ValueError):
        return
    info = read(config.own_data_root())
    if not info or info.get("pid") != os.getpid() or info.get("port") != port:
        return
    try:
        (config.own_data_root() / NAME).unlink()
    except OSError:
        pass


def _trusted(info: "os.stat_result") -> bool:
    """A descriptor this account wrote itself: a regular file (``lstat`` never follows a symlink), unreadable by group
    and others, and, where the platform has ownership, owned by this user. Windows reports a synthetic mode and has no
    uid, so there only the file-type check applies."""
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.name != "posix":
        return True
    if info.st_mode & 0o077:
        return False
    getuid = getattr(os, "getuid", None)
    return getuid is None or info.st_uid == getuid()


def read(root: Optional[Path] = None) -> Optional[dict]:
    """The descriptor in ``root`` (default: the store this call writes to), or None for any error, bad shape, or a
    file this account did not write as itself."""
    path = (Path(root) if root is not None else config.data_root()) / NAME
    try:
        info = path.lstat()
    except OSError:
        return None
    if not _trusted(info):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("service") != SERVICE or data.get("api") != API:
        return None
    secret = data.get("secret")
    pid = data.get("pid")
    if not isinstance(secret, str) or len(secret) < 16 or not isinstance(pid, int) or isinstance(pid, bool):
        return None
    return data


def base(root: Optional[Path] = None) -> Optional[str]:
    """A validated loopback http base from the descriptor, or None."""
    info = read(root)
    if not info:
        return None
    value = info.get("base")
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if (parts.scheme != "http" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    if parts.hostname not in LOOPBACK_NAMES:
        return None
    return value.rstrip("/")


def _same_root(reported: Any, root: Path) -> bool:
    try:
        return Path(str(reported)).expanduser().resolve() == Path(root).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return False


def _prove(conn: http.client.HTTPConnection, info: dict, want: Path) -> bool:
    """Answer the descriptor's challenge on ``conn`` and check it the way ``verify`` does.

    The secret is never sent: the client picks a nonce and the server must answer HMAC(secret, nonce) as its own pid and
    store. Raises on transport or protocol errors; the caller decides whether that failed the proof.
    """
    secret, pid = info["secret"], info["pid"]
    nonce = pysecrets.token_hex(16)
    conn.request("GET", f"{CHALLENGE_PATH}?nonce={nonce}", headers={"User-Agent": USER_AGENT})
    response = conn.getresponse()
    raw = response.read(65536)
    if response.status != 200:
        return False
    answer = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(answer, dict) or answer.get("service") != SERVICE or answer.get("pid") != pid:
        return False
    if answer.get("nonce") != nonce or not _same_root(answer.get("data_root"), want):
        return False
    got = answer.get("mac")
    return isinstance(got, str) and hmac.compare_digest(got, mac(secret, nonce))


def verify(base: str, root: Optional[Path] = None, timeout: float = 1.5) -> bool:
    """True when ``base`` is the live mock-review server that wrote this store's descriptor.

    The secret is never sent: the client picks a nonce and the server must answer HMAC(secret, nonce) as its own pid and
    store. A listener that merely bound the port of a crashed server, or any other program on that port, cannot answer,
    so a personal access token never follows a false success.
    """
    want = Path(root) if root is not None else config.data_root()
    info = read(want)
    if not info:
        return False
    try:
        conn = _connection(base, timeout)
        try:
            return _prove(conn, info, want)
        finally:
            conn.close()
    except Exception:
        return False


def post(base: str, path: str, body: dict, token: str, timeout: float = 3.0,
         root: Optional[Path] = None) -> Optional[dict]:
    """POST JSON with a bearer token, proving the listener on the very connection that carries the write.

    The descriptor is read here too, the challenge is answered on that one connection, and the bearer goes out on the
    same socket only after every ``verify`` check holds; a missing descriptor, a failed proof or a connection the peer
    closed after the challenge returns None (store the write, never retry on a fresh connection). An HTTP error response
    still raises store.ReviewError; transport errors still propagate.
    """
    want = Path(root) if root is not None else config.data_root()
    info = read(want)
    if not info:
        return None
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    conn = _connection(base, timeout)
    try:
        try:
            proved = _prove(conn, info, want)
        except Exception:
            return None
        if not proved or conn.sock is None:       # a proof that no longer holds, or no socket left to write on
            return None
        conn.request("POST", path, body=data, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
        response = conn.getresponse()
        raw = response.read(4 * 1024 * 1024)
        status = response.status
    finally:
        conn.close()
    if status != 200:
        message = ""
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                message = payload["error"]
        except ValueError:
            pass
        raise store.ReviewError(message or f"the board server refused the write (HTTP {status})",
                                status if 400 <= status < 500 else 502)
    try:
        answer = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not answer.get("ok"):
        raise store.ReviewError("the board server returned an unexpected answer", 502)
    return answer
