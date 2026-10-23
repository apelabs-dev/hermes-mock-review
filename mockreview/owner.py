"""Board ownership: the running board server owns storage and wake-ups.

A review is only useful when the server that serves the board, polls here.now and POSTs the wake-up can *find* it.
The server reads one store (``data_dir`` / ``$HERMES_HOME/workspace/mock-reviews``) and resolves its signing
secret through Hermes. Workers may use a different home, so tools verify the owning store before publishing.

What this module does:

* ``describe()`` (server side, ``GET /api/owner``, loopback only): where the server's store is and whether its wake
  route is usable. No secret ever leaves the server.
* ``resolve()`` (client side): ask the server who owns storage, then decide where THIS call may write:
  its own store (the server watches it), the server's store (a Kanban worker, or ``owner: service``), or refuse.
* ``bound()``: run a block with ``config.data_root()`` pointing at the server's store.
* ``confirm_visible()``: after creating, ask the server to load the board before anything is published.
* ``adopt()``: move one already-created review from a private store into the watched store without losing a byte
  of feedback, refusing on any collision.

Nothing here sends a wake-up. Only the server process delivers (``service.notify_round`` refuses in a bound call).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config, notify, settings, store
from .store import ReviewError

SERVICE = "mock-review"
API = 1
OWNER_PATH = "/api/owner"
RECOVERY_NOTE = (
    "RECOVERED DELIVERY, NOT A NEW REQUEST. This review was created in a private store that nothing watched, and "
    "was moved into the watched store so its pending wake-up could be delivered. The reviewer's feedback in this round "
    "was already read by Hermes before this message. It carries no new user instruction: do not rebuild the round, "
    "do not add a round, do not republish. If you act at all, post one line pointing at the existing reply.")

_owner_process = False


class OwnerError(ReviewError):
    """The board cannot be created or extended safely. The message says what to fix."""


def mark_owner_process(on: bool = True) -> None:
    """The board server calls this: it IS the owner, so it never asks itself."""
    global _owner_process
    _owner_process = on


def is_owner_process() -> bool:
    return _owner_process


def is_kanban_worker() -> bool:
    return bool(os.environ.get("HERMES_KANBAN_TASK"))


def default_owner_root() -> Path:
    """Default store in the active Hermes home."""
    return config.hermes_home() / "workspace" / "mock-reviews"


def _same(a: Any, b: Any) -> bool:
    try:
        return Path(str(a)).expanduser().resolve() == Path(str(b)).expanduser().resolve()
    except OSError:
        return False


# ---------------------------------------------------------------- server side

def wake_status() -> Dict[str, Any]:
    """Report signing readiness only; route creation and delivery are checked by setup."""
    secret, err = notify.load_secret()
    return {"route": config.route_name(), "ready": bool(secret), "secret": "set" if secret else "not-set",
            "error": err or None, "url": config.webhook_url()}


def describe() -> Dict[str, Any]:
    return {"ok": True, "service": SERVICE, "api": API, "pid": os.getpid(),
            "data_root": str(config.own_data_root()), "hermes_home": str(config.hermes_home()),
            "wake": wake_status()}


# ---------------------------------------------------------------- client side

def probe(timeout: float = 2.0) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """Ask the board server who owns storage.

    ('ok', info, '')            an owner-API server answered
    ('legacy', None, why)       a server answered /healthz but predates /api/owner (it cannot report its store)
    ('unreachable', None, why)  nothing answered
    """
    base = config.local_base()
    try:
        with urllib.request.urlopen(base + OWNER_PATH, timeout=timeout) as r:
            info = json.loads(r.read(65536).decode("utf-8", "replace"))
        if isinstance(info, dict) and info.get("service") == SERVICE and isinstance(info.get("data_root"), str):
            return "ok", info, ""
        return "unreachable", None, f"{base}{OWNER_PATH} did not answer like a mock-review server"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=timeout) as r:
                    if r.status == 200:
                        return "legacy", None, f"the server on {base} predates {OWNER_PATH}"
            except Exception:
                pass
        return "unreachable", None, f"{base}{OWNER_PATH} -> HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "unreachable", None, f"cannot reach the board server on {base}: {getattr(e, 'reason', e)}"


@dataclass
class Context:
    data_root: Path                      # the store this call reads and writes
    bound: bool = False                  # True: the server's store, not this process's own
    state: str = "self"                  # owner | ok | legacy | unreachable | self
    wake: Dict[str, Any] = field(default_factory=dict)
    why: str = ""
    owner_root: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state, "data_root": str(self.data_root), "bound_to_service": self.bound,
                               "wake_route": {"route": self.wake.get("route"), "ready": bool(self.wake.get("ready"))}}
        if self.wake.get("error"):
            out["wake_route"]["error"] = self.wake["error"]
        if self.why:
            out["note"] = self.why
        return out

    def require_wake(self) -> None:
        if not self.wake.get("ready"):
            raise OwnerError(
                "no wake route: Send would store feedback but never wake Hermes. "
                f"{self.wake.get('error') or 'the owning service cannot use route ' + repr(self.wake.get('route'))}. "
                "Fix the subscription (`hermes webhook subscribe`), or for a board nobody has to be woken by pass "
                "wake=false / --no-wake (or set wake_required: false).", 409)


def _local_wake() -> Dict[str, Any]:
    return wake_status()


def resolve() -> Context:
    """Decide where this call writes. Raises OwnerError when no safe store exists. Sends nothing."""
    own = config.own_data_root()
    if _owner_process:
        return Context(own, False, "owner", wake_status())
    mode = settings.get("owner")
    if mode == "self":
        return Context(own, False, "self", _local_wake(), "owner: self, no server check")
    state, info, why = probe()
    explicit = settings.resolve_one("data_dir")[1] != "default"
    if state == "ok":
        assert info is not None
        wake = info.get("wake")
        if not isinstance(wake, dict):
            wake = {"ready": False, "error": "owner reported no wake status"}
        if _same(info["data_root"], own):
            return Context(own, False, "ok", wake, owner_root=info["data_root"])
        if mode == "service" or (is_kanban_worker() and not explicit):
            return Context(Path(info["data_root"]).expanduser(), True, "ok", wake,
                           "created in the board server's store (this process's own store is not watched)",
                           owner_root=info["data_root"])
        raise OwnerError(
            f"this process's store {own} is not the one the board server watches ({info['data_root']}), so a board "
            "created here could never be served or woken. "
            + ("data_dir is set explicitly, so it is not redirected. " if explicit else "")
            + "Run your own server on your own port, or set plugins.entries.mock-review.settings.owner to "
              "'service' to create in the server's store, or 'self' to accept an unwatched store.", 409)
    if state == "legacy":
        raise OwnerError(f"{why}; upgrade the board server before creating a review.", 503)
    if mode == "service":
        raise OwnerError(f"owner: service, but {why}. Start the board server (run `hermes mockreview serve`, or set "
                         "autostart: true and restart the gateway), then retry.", 503)
    default_root = default_owner_root()
    if not _same(own, default_root):
        raise OwnerError(
            f"{why}, and this process's store {own} is private to this profile, so the server could not find a board "
            "created here either. Start the board server (run `hermes mockreview serve`, or set "
            "plugins.entries.mock-review.settings.autostart: true and restart the gateway) and retry.", 503)
    note = (why + "; this is the default store a server would watch, so creating is allowed, but "
            "the board will not load until the server runs") if state == "unreachable" else \
        (why + "; its store could not be verified, but this is the default store it watches")
    return Context(own, False, state, _local_wake(), note)


def resolve_soft() -> Context:
    """Like resolve(), for operations that must still work on a plain machine: fall back to this process's own store."""
    try:
        return resolve()
    except OwnerError as e:
        return Context(config.own_data_root(), False, "self", {"ready": False, "error": str(e)}, str(e))


@contextlib.contextmanager
def bound(ctx: Context) -> Iterator[Context]:
    """Point config.data_root() at the server's store for the block when the context says so."""
    if not ctx.bound:
        yield ctx
        return
    tok = config._bound_root.set(ctx.data_root)
    try:
        yield ctx
    finally:
        config._bound_root.reset(tok)


def is_bound() -> bool:
    return config.bound_root() is not None


def confirm_visible(review_id: str, ctx: Context, timeout: float = 4.0) -> bool:
    """Ask the owning server to load the board. True = it can. Skipped (None-ish True) when no server is involved."""
    if ctx.state not in ("ok", "legacy"):
        return True
    url = f"{config.local_base()}/r/{review_id}/state.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            st = json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "replace"))
        return isinstance(st, dict) and st.get("id") == review_id
    except (urllib.error.URLError, OSError, ValueError):
        return False


def discard_created(review_id: str) -> None:
    """Roll back a review this call just created (never anything else): the whole id directory."""
    d = store.review_dir(review_id)
    with contextlib.suppress(OSError):
        shutil.rmtree(d)


# ---------------------------------------------------------------- adopt (recover a review from a private store)

def _tree_digest(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = "symlink:" + os.readlink(p)
        elif p.is_file():
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[rel] = h.hexdigest()
        else:
            out[rel] = "dir"
    return out


def _identity(d: Dict[str, Any]) -> Dict[str, Any]:
    hn = d.get("herenow") or {}
    return {"id": d.get("id"), "token": d.get("token"), "slug": hn.get("slug")}


def _pending_submission(d: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    for r in reversed(d.get("rounds") or []):
        sub = r.get("submission")
        if sub and r.get("feedback"):
            return r["n"], sub.get("seq", 1)
    return None


def adopt_target(explicit: Optional[str] = None) -> Tuple[Path, str]:
    """Where adopt() writes: --to, else the running server's reported store, else the default store a server watches.
    ``owner: self`` means this process's own store (tests, own server)."""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            raise ReviewError("target store must be an absolute path")
        return p, "explicit"
    if settings.get("owner") == "self" or _owner_process:
        return config.own_data_root(), "own store"
    state, info, why = probe()
    if state == "ok" and info:
        return Path(info["data_root"]).expanduser(), "reported by the running board server"
    return default_owner_root(), f"default watched store ({why})"


def adopt(src: str, backup_dir: str, note: Optional[str] = None, target: Optional[str] = None,
          dry_run: bool = False) -> Dict[str, Any]:
    """Move one review directory into the watched store, byte for byte, with a backup and collision checks.

    Order: validate -> collision checks -> backup tarball -> copy to a dot-prefixed staging dir (invisible to the
    server) -> verify every file hash -> annotate review.json (wake_note, adopted) -> atomic rename into the store ->
    remove the source. Re-running after a crash is safe: an identical review already in the store is reported,
    never overwritten, and a different one with the same id is a collision.
    """
    srcp = Path(src).expanduser()
    if not srcp.is_absolute():
        raise ReviewError("source must be an absolute path")
    if srcp.is_symlink() or not srcp.is_dir():
        raise ReviewError(f"source is not a plain directory: {srcp}")
    try:
        sdata = json.loads((srcp / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReviewError(f"source has no readable review.json: {e}")
    rid = store.validate_id(sdata.get("id"))
    if srcp.name != rid:
        raise ReviewError(f"directory name {srcp.name!r} does not match review id {rid!r}")
    root, why_target = adopt_target(target)
    if _same(root, srcp.parent):
        raise ReviewError("source is already inside the target store")
    ctx = Context(root, True, "adopt")
    with bound(ctx):
        if root.resolve() in srcp.resolve().parents:
            raise ReviewError("source is inside the target store")
        dest = root / rid
        if dest.exists():
            try:
                ddata = json.loads((dest / "review.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                ddata = {}
            if _identity(ddata) == _identity(sdata):
                return {"adopted": False, "already_in_store": True, "review_id": rid, "dest": str(dest),
                        "source_left_in_place": str(srcp),
                        "note": "an identical review (same id, token, here.now slug) is already in the store; "
                                "nothing was changed or removed"}
            raise ReviewError(f"collision: {dest} already holds a different review "
                              f"(identity {_identity(ddata)} vs {_identity(sdata)}). Nothing was changed.", 409)
        for other in store.list_ids():
            try:
                od = store.load(other)
            except (ReviewError, ValueError, OSError):
                continue
            oid, sid = _identity(od), _identity(sdata)
            if oid["token"] and oid["token"] == sid["token"]:
                raise ReviewError(f"collision: review {other} in the store has the same submit token", 409)
            if oid["slug"] and sid["slug"] and oid["slug"] == sid["slug"]:
                raise ReviewError(f"collision: review {other} in the store is published at the same here.now slug", 409)
        before = _tree_digest(srcp)
        pend = _pending_submission(sdata)
        if dry_run:
            return {"adopted": False, "dry_run": True, "review_id": rid, "source": str(srcp), "dest": str(dest),
                    "target": why_target, "files": len(before), "collisions": None,
                    "pending": {"round": pend[0], "seq": pend[1]} if pend else None,
                    "source_identity": {"id": rid, "slug": _identity(sdata)["slug"]}}
        bdir = Path(backup_dir).expanduser()
        bdir.mkdir(parents=True, exist_ok=True)
        os.chmod(bdir, 0o700)
        tar_path = bdir / f"{rid}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(str(srcp), arcname=rid)
        os.chmod(tar_path, 0o600)
        with tarfile.open(tar_path, "r:gz") as tf:
            n_members = sum(1 for m in tf.getmembers() if m.isfile())
        if n_members != sum(1 for v in before.values() if v not in ("dir",) and not v.startswith("symlink:")):
            raise ReviewError(f"backup verification failed ({n_members} files archived); source untouched", 500)
        stage = root / f".adopt-{rid}.tmp"
        if stage.exists():
            shutil.rmtree(stage)
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(srcp, stage, symlinks=True)
        if _tree_digest(stage) != before:
            shutil.rmtree(stage, ignore_errors=True)
            raise ReviewError("staged copy differs from the source; nothing was moved", 500)
        adopted_at = store.now_iso()
        ndata: Dict[str, Any] = {}
        if pend:
            ndata["wake_note"] = {"round": pend[0], "seq": pend[1], "text": (note or RECOVERY_NOTE).strip()}
        ndata.update(sdata)
        ndata["adopted"] = {"from": str(srcp), "at": adopted_at, "backup": str(tar_path)}
        store.atomic_write(stage / "review.json", json.dumps(ndata, indent=2, ensure_ascii=False))
        os.rename(stage, dest)                 # atomic: the server sees all of it or none of it
        shutil.rmtree(srcp)
        after = _tree_digest(dest)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return {"adopted": True, "review_id": rid, "dest": str(dest), "backup": str(tar_path),
            "files": len(before), "changed_files": changed, "wake_note": bool(pend),
            "pending": {"round": pend[0], "seq": pend[1]} if pend else None,
            "source_removed": str(srcp), "target": why_target}
