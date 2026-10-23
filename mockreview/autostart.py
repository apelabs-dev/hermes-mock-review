"""The board server inside the Hermes gateway process (setting `autostart`, off by default).

The plugin registers gateway_factory() with ctx.register_platform_handler("webhook", ...). The gateway calls it when
its webhook platform connects (that platform only ever connects inside the gateway, and Send needs it anyway), so the
CLI, `hermes plugins validate` and Kanban workers never start anything. register() itself starts nothing.

What runs when it starts: one daemon thread "mockreview-board" (serve_forever on host:port) and the daemon
"mockreview-worker" thread (retries, here.now polls, housekeeping), both from server.py, shared with the foreground
`mockreview serve`. They stop when the process exits (one atexit handler shuts the server down cleanly). There is
deliberately no stop on plugin unload: a forced plugin rescan unloads and re-registers every plugin, and the gateway
never calls an already-wired factory again, so stopping there would leave no board until the gateway restarts.

The singleton lives here (not in the plugin's root __init__.py, which a rescan re-imports). In-process code never
installs signal handlers or configures logging: those belong to the gateway.
"""

from __future__ import annotations

import atexit
from contextvars import copy_context
import logging
import threading
from typing import Any, Optional

from . import config, owner, secrets, server, settings

log = logging.getLogger("mockreview.autostart")

THREAD_NAME = "mockreview-board"

STARTED = "started"
ALREADY_RUNNING = "already-running"
DISABLED = "disabled"
PORT_IN_USE = "port-in-use"
SKIPPED_KANBAN = "skipped-kanban-worker"
ERROR = "error"


def _serve(srv: Any) -> None:
    """The serving thread, in the profile scope of the home the server was made for (srv.owner_home). Requests and
    Worker ticks bind their own (server.py)."""
    with secrets.owner_scope(srv.owner_home):
        srv.serve_forever(poll_interval=0.5)


class BoardServer:
    """One bound board server serving from a daemon thread, plus its Worker."""

    def __init__(self) -> None:
        self._srv: Any = None
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[server.Worker] = None
        self.host = ""
        self.port = 0

    def start(self, host: str, port: int, worker: bool = True) -> None:
        """Bind now, in the caller's thread (OSError / SystemExit surface here), then serve in the background."""
        srv = server.make_server(host, port)
        self._srv, self.host, self.port = srv, host, srv.server_address[1]
        self._thread = threading.Thread(target=copy_context().run, args=(_serve, srv),
                                        name=THREAD_NAME, daemon=True)
        self._thread.start()
        if worker:
            self._worker = server.Worker()
            self._worker.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        return server.display_url(self.host, self.port) if self._srv is not None else ""

    def stop(self, timeout: float = 5.0) -> None:
        """Safe twice and when never started. shutdown() waits for serve_forever to finish, which never happens if
        it isn't running, so it is only called while the serving thread is alive, and from a helper thread so the
        wait is bounded."""
        srv, thread, worker = self._srv, self._thread, self._worker
        self._srv = self._thread = self._worker = None
        if worker is not None:
            worker.stop_event.set()
        if srv is not None:
            if thread is not None and thread.is_alive():
                t = threading.Thread(target=srv.shutdown, name=THREAD_NAME + "-stop", daemon=True)
                t.start()
                t.join(timeout)
            srv.server_close()
        if thread is not None:
            thread.join(timeout)
        if worker is not None:
            worker.join(timeout)


_lock = threading.Lock()
_board: Optional[BoardServer] = None
_atexit_registered = False


def _board_thread_alive() -> bool:
    """A board thread from another copy of this module (re-imported under another name) counts as running too."""
    return any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())


def running() -> Optional[BoardServer]:
    return _board if _board is not None and _board.running else None


def ensure_started(reason: str = "") -> str:
    """Start the in-process board if autostart is on and none runs yet. Never raises; returns a status."""
    global _board, _atexit_registered
    why = f" ({reason})" if reason else ""
    try:
        if owner.is_kanban_worker():
            log.debug("mock-review autostart skipped in a Kanban worker%s", why)
            return SKIPPED_KANBAN
        if not settings.get("autostart"):
            log.debug("mock-review autostart is off%s: no board server started in this process", why)
            return DISABLED
        with _lock:
            if running() is not None or _board_thread_alive():
                return ALREADY_RUNNING
            host, port = config.host(), config.port()
            where = server.display_url(host, port)
            board = BoardServer()
            try:
                board.start(host, port)
            except OSError as e:
                board.stop(0.5)
                if server.addr_in_use(e):
                    log.warning("mock-review autostart: %s is already in use, so the board server did not start in "
                                "this process. Another mock-review server is probably running; check `hermes "
                                "mockreview owner`, or change the port setting and restart the gateway.", where)
                    return PORT_IN_USE
                log.warning("mock-review autostart: cannot listen on %s (%s), so the board server did not start. "
                            "Check the host and port settings and restart the gateway.", where, e.strerror or e)
                return ERROR
            except SystemExit as e:   # make_server's host allow-list
                board.stop(0.5)
                log.warning("mock-review autostart: %s; the board server did not start.", e)
                return ERROR
            _board = board
            owner.mark_owner_process()
            if not _atexit_registered:
                atexit.register(stop_running, 2.0)
                _atexit_registered = True
        server.log_settings()
        log.info("mock-review board serving on %s inside this process%s (data: %s). Turn off with "
                 "autostart: false and restart the gateway.", board.url, why, config.data_root())
        return STARTED
    except Exception:
        log.exception("mock-review autostart failed%s", why)
        return ERROR


def stop_running(timeout: float = 5.0) -> None:
    """Stop the in-process board (the atexit path; tests). Safe when nothing runs."""
    global _board
    with _lock:
        board, _board = _board, None
    if board is not None:
        board.stop(timeout)
        owner.mark_owner_process(False)
        log.info("mock-review board server stopped")


def gateway_factory(native: Any = None, adapter: Any = None) -> str:
    """ctx.register_platform_handler("webhook", ...) target: (native, adapter) are ignored, the adapter is never
    touched. Runs at webhook connect time in the gateway; never raises into it."""
    try:
        return ensure_started("gateway webhook connect")
    except Exception:   # pragma: no cover - ensure_started already catches everything
        log.exception("mock-review autostart failed")
        return ERROR
