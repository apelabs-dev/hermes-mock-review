"""Live board updates: the SSE subscription registry, presence leases, typing leases and the event publisher.

One `Registry` per process. It owns nothing that must survive a restart: subscriptions are open sockets, presence and
typing are leases in memory. The persisted event log lives in SQLite (`db.events`, migration 3), so a reconnecting
client can replay what it missed with `Last-Event-ID` and refetch on `reset`.

Threads publish through `publish()`; the registry persists the event before it takes the registry lock, then offers it
to every queue with `put_nowait`. A stream that cannot keep up is marked slow and closed instead of blocking a writer.
A subscription may carry a round filter (`allow_round`): a pinned viewer guest's stream then drops the frames of other
rounds, still advancing its cursor past them, so a reconnect neither replays them nor stalls.
The closer installed on `streams` ends the streams of signed-out sessions, regenerated or stopped share links and
share links whose guest read policy changed.

A per-review policy epoch, kept in memory, closes the admission race. A request captures the epoch *before* it reads
its one read scope and passes it to `subscribe`, which validates it under `_lock` in the same critical section that
would register the subscription. Every closer that ends guest streams after a policy change bumps that review's epoch
under `_lock` before it collects its targets, so a request either registers first (and the closer then closes it) or
is refused and the client reconnects: an obsolete authorization can never become an active subscription.

A second, process-wide revision covers what an epoch per review cannot: a sign-out. A session can be ended before the
request ever names a review, so `close_sessions` bumps the revision as well, and a request snapshots it before it
re-reads its authorization. The server re-reads that authorization from the request's own cookies through `admit`,
called here at the last moment before registration; the revision is compared under `_lock` after the re-read, so a
revocation landing anywhere in between refuses the request instead of registering an obsolete principal.

Closure is also effective at the socket: `Subscription.close()` fences the connection's `sse.Output`, so a stream
closed mid-handshake, mid-replay, during a visibility evaluation or with a frame already dequeued (or already inside
the write path) writes nothing. Completion is linearizable per subscription: a closer that finds a close already
started never returns before that close's fence (it completes or waits for it), and `_live_subs` keeps a
closed-but-unfenced subscription so pruning cannot lose the obligation to finish that fence. On its slow-consumer path
the publisher only stops a subscription (closed flag, drained queue, sentinel) and never waits for a socket; the
stream's own loop fences in its `finally`, and a restriction closer fences or waits for it.

Lock order: `_publish_lock` -> `_lock`, never the reverse. `publish()` takes `_publish_lock` and calls `_fanout`,
which takes `_lock`; the epoch bump and the epoch check both run under `_lock` with no I/O inside it, and `_offer`
stays `put_nowait`. No registry lock is ever held across a socket write or `sse.Output.close()`.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import db, sse, streams

log = logging.getLogger("mockreview.live")

QUEUE_MAX = 200
MAX_STREAMS = 25
RESERVED_FOR_OWNER = 1
MAX_OWNER_STREAMS = 5
PRESENCE_TTL = 45.0
PRESENCE_EVERY = 20.0
TYPING_TTL = 5.0
TYPING_EVERY = 2.0
KEEP_EVENTS = 500
LOOP_WAIT = 0.5
RECHECK_EVERY = 5.0  # seconds between access re-reads on a live stream
_SENTINEL = object()
PIN_REQUESTS = "pin.requests"

THREAD_KINDS = frozenset({"thread.created", "thread.replied", "thread.status", "thread.deleted",
                          "comment.edited", "comment.deleted", "mention", "typing", PIN_REQUESTS})


def event_round(kind: str, data: Any) -> Optional[int]:
    """The round an event belongs to, from its own payload (the client's own shape, no extra query)."""
    if not isinstance(data, dict):
        return None
    thread = data.get("thread")
    if isinstance(thread, dict) and thread.get("round") is not None:
        try:
            return int(thread["round"])
        except (TypeError, ValueError):
            return None
    r = data.get("round")
    if isinstance(r, bool):
        return None
    if isinstance(r, int):
        return r
    if isinstance(r, str) and r.isdecimal():
        return int(r)
    return None


class TooManyStreams(Exception):
    pass


class Subscription:
    """One open SSE response. `queue` carries (id or None, kind, data) tuples and one sentinel at close."""

    def __init__(self, review_id: str, actor: str, name: str, kind: str, session_hash: str = "",
                 guest_generation: Optional[int] = None, owner: bool = False,
                 allow_round: Optional[Callable[[Optional[int]], bool]] = None,
                 recheck: Optional[Callable[[], bool]] = None):
        self.id = uuid.uuid4().hex
        self.review_id = review_id
        self.actor = actor
        self.name = name
        self.kind = kind
        self.session_hash = session_hash
        self.guest_generation = guest_generation
        self.owner = owner
        self.allow_round = allow_round
        self.recheck = recheck
        self.queue: "queue.Queue[Any]" = queue.Queue(maxsize=QUEUE_MAX)
        # The write path of this socket. `close()` fences it, so a closer running on another thread (a sign-out, a
        # revoked link) stops the bytes themselves, not only the loop that would like to send them.
        self.output = sse.Output()
        self.closed = False
        self.slow = False
        self.last_presence = 0.0

    @property
    def fenced(self) -> bool:
        """True once this subscription's transport fence has completed (see `sse.Output.close`)."""
        return self.output.fenced

    def stop(self) -> None:
        """Stop this subscription without touching the socket.

        The publisher's slow-consumer path uses this: a publisher holds `_publish_lock` and must never wait for a
        socket. The stream's own loop fences in its `finally` (`close`), and a restriction closer that still sees this
        subscription completes or waits for the fence. Idempotent: the closed flag, the drain and the sentinel may all
        run again.
        """
        self.closed = True
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass

    def close(self) -> None:
        """End this subscription: stop the loop, then fence the socket.

        A frame the loop has already dequeued (or is inside `sse.Output.emit` with) writes nothing after this returns.
        A closer that finds the close already started does not return early: it reaches `sse.Output.close` too, which
        completes or waits for the in-flight fence. A second successful restriction therefore cannot skip it.
        """
        self.stop()
        self.output.close()


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # One writer at a time, across the insert *and* its fan-out. A stream's cursor only moves forward, so an event
        # that reached a subscriber's queue out of id order would be dropped by the newer id's floor: assigning the id
        # and delivering it are one step here, which keeps every queue in commit order. Enqueueing is put_nowait, so a
        # slow consumer never holds this lock.
        self._publish_lock = threading.Lock()
        self._subs: Dict[str, List[Subscription]] = {}
        self._presence: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._typing: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._typing_sent: Dict[Tuple[str, str, str], float] = {}
        # Per-review policy epoch. The epoch bump and the admission check both run under `_lock` with no I/O.
        self._epochs: Dict[str, int] = {}
        # Process-wide revocation revision. Every closer bumps it under `_lock`; a request snapshots it before it
        # re-reads its authorization and admission compares it under `_lock`. This is what a per-review epoch cannot
        # do for a sign-out: the request's session may be revoked before it ever names the review it wants.
        self._revision = 0

    # ---- subscriptions -------------------------------------------------
    def epoch(self, review_id: str) -> int:
        """The review's current policy epoch. Capture it *before* computing a request's read scope: a policy change
        that commits in between then bumps this epoch, so admission refuses the stale request (safe over-rejection,
        the client reconnects). Capturing the scope first and the epoch second could admit the old scope."""
        with self._lock:
            return self._epochs.get(review_id, 0)

    def _bump_epoch(self, review_id: str) -> None:
        """Invalidate in-flight admissions for this review. Caller holds `_lock`."""
        self._epochs[review_id] = self._epochs.get(review_id, 0) + 1

    def revision(self) -> int:
        """The current revocation revision.

        Capture it *before* re-reading a request's authorization, for the same reason as `epoch()`: a revocation that
        commits after this read bumps the revision, and admission refuses the request. A per-review epoch alone
        cannot cover a sign-out that lands before the request reaches its review.
        """
        with self._lock:
            return self._revision

    def _revoke(self) -> None:
        """Invalidate every in-flight admission, in any review. Caller holds `_lock`."""
        self._revision += 1

    def _live_subs(self, review_id: str) -> List[Subscription]:
        # A subscription that is closed but not yet fenced is retained: a later closer for this review must be able to
        # complete or wait for that fence, so pruning may not lose it. It counts towards the seat cap until then
        # (over-refusal is safe) and is dropped here on the first pass after its fence completed.
        subs = [s for s in self._subs.get(review_id, []) if not s.closed or not s.fenced]
        if subs:
            self._subs[review_id] = subs
        else:
            self._subs.pop(review_id, None)
        return subs

    @staticmethod
    def _refused(review_id: str, actor: str, name: str, kind: str, session_hash: str,
                 guest_generation: Optional[int], owner: bool,
                 allow_round: Optional[Callable[[Optional[int]], bool]]) -> Subscription:
        """A refusal shaped like a stream: the route still answers 200 text/event-stream and the body is a clean EOF,
        but nothing was registered, counted, leased as presence or written."""
        sub = Subscription(review_id, actor, name, kind, session_hash, guest_generation, owner, allow_round)
        sub.close()
        return sub

    def subscribe(self, review_id: str, actor: str, name: str, kind: str, session_hash: str = "",
                  guest_generation: Optional[int] = None, owner: bool = False,
                  allow_round: Optional[Callable[[Optional[int]], bool]] = None,
                  epoch: Optional[int] = None, revision: Optional[int] = None,
                  admit: Optional[Callable[[], bool]] = None,
                  recheck: Optional[Callable[[], bool]] = None) -> Subscription:
        """Register one live seat, or return a refused subscription.

        `admit` re-reads the request's authorization (the caller's database work, so it runs outside `_lock`) at the
        last moment before registration: a session signed out or a share link revoked after the request
        authenticated therefore cannot take a seat. `revision` and `epoch` were snapshotted before that read and are
        compared here, inside the same critical section that registers, so a revocation landing between the check and
        the registration refuses the request too. Either way the request gets a clean EOF and reconnects.
        """
        if admit is not None and not admit():
            return self._refused(review_id, actor, name, kind, session_hash, guest_generation, owner, allow_round)
        stale = False
        with self._lock:
            stale_epoch = epoch is not None and epoch != self._epochs.get(review_id, 0)
            stale = stale_epoch or (revision is not None and revision != self._revision)
            if not stale:
                subs = self._live_subs(review_id)
                if len(subs) >= MAX_STREAMS:
                    raise TooManyStreams()
                if owner:
                    if sum(1 for s in subs if s.owner) >= MAX_OWNER_STREAMS:
                        raise TooManyStreams()
                elif sum(1 for s in subs if not s.owner) >= MAX_STREAMS - RESERVED_FOR_OWNER:
                    raise TooManyStreams()
                sub = Subscription(review_id, actor, name, kind, session_hash, guest_generation, owner,
                                   allow_round, recheck=recheck)
                subs.append(sub)
                self._subs[review_id] = subs
                self._presence[(review_id, actor, session_hash or sub.id)] = {
                    "actor": actor, "name": name, "kind": kind, "at": time.monotonic()}
        if stale:
            # Obsolete authorization: never register it, count it, lease presence or broadcast presence. The route
            # still answers 200 text/event-stream, and `stream` returns before writing because it is closed, so the
            # client sees a clean EOF and reconnects. Built outside `_lock`: closing an output is not done under it.
            return self._refused(review_id, actor, name, kind, session_hash, guest_generation, owner, allow_round)
        self._broadcast_presence(review_id)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            subs = [s for s in self._subs.get(sub.review_id, []) if s is not sub]
            if subs:
                self._subs[sub.review_id] = subs
            else:
                self._subs.pop(sub.review_id, None)
            for key in [k for k in self._presence if k[0] == sub.review_id and k[1] == sub.actor
                        and k[2] in (sub.session_hash, sub.id)]:
                self._presence.pop(key, None)
        self._broadcast_presence(sub.review_id)

    def close_subscription(self, sub: Subscription) -> None:
        sub.close()

    def count(self, review_id: str, owner: Optional[bool] = None) -> int:
        with self._lock:
            subs = self._live_subs(review_id)
            return len([s for s in subs if owner is None or s.owner == owner])

    def subscriptions(self, review_id: str) -> List[Subscription]:
        with self._lock:
            return list(self._live_subs(review_id))

    # ---- presence ------------------------------------------------------
    def _people(self, review_id: str) -> List[Dict[str, str]]:
        cutoff = time.monotonic() - PRESENCE_TTL
        people: Dict[str, Dict[str, str]] = {}
        for (rid, actor, _sid), lease in list(self._presence.items()):
            if rid != review_id:
                continue
            if lease["at"] < cutoff:
                self._presence.pop((rid, actor, _sid), None)
                continue
            people[actor] = {"actor": actor, "name": lease["name"], "kind": lease["kind"]}
        return sorted(people.values(), key=lambda p: (p["name"].lower(), p["actor"]))

    def presence(self, review_id: str) -> Dict[str, Any]:
        with self._lock:
            people = self._people(review_id)
        return {"people": people, "count": len(people)}

    def _presence_frame(self, review_id: str) -> Optional[Tuple[None, str, Dict[str, Any]]]:
        with self._lock:
            people = self._people(review_id)
            subs = self._live_subs(review_id)
            if not subs:
                return None
            now = time.monotonic()
            for s in subs:
                s.last_presence = now
            payload = {"people": people, "count": len(people)}
        return (None, "presence", payload)

    def _broadcast_presence(self, review_id: str, force: bool = True) -> None:
        if not force:
            with self._lock:
                subs = self._live_subs(review_id)
                if not subs or any(time.monotonic() - s.last_presence < PRESENCE_EVERY for s in subs):
                    return
        frame = self._presence_frame(review_id)
        if frame is None:
            return
        with self._lock:
            subs = self._live_subs(review_id)
        for sub in subs:
            self._offer(sub, frame)

    def refresh(self, sub: Subscription) -> None:
        """Keep this stream's lease fresh; runs from the stream's own loop."""
        with self._lock:
            self._presence[(sub.review_id, sub.actor, sub.session_hash or sub.id)] = {
                "actor": sub.actor, "name": sub.name, "kind": sub.kind, "at": time.monotonic()}
        self._broadcast_presence(sub.review_id, force=False)

    # ---- typing --------------------------------------------------------
    def typing(self, review_id: str, thread_id: str, actor: str, name: str,
               round_n: Optional[int] = None) -> bool:
        key = (review_id, thread_id, actor)
        now = time.monotonic()
        with self._lock:
            last = self._typing_sent.get(key, 0.0)
            if now - last < TYPING_EVERY:
                return False
            self._typing_sent[key] = now
            self._typing[key] = {"name": name, "at": now}
            for k in [k for k, v in self._typing.items() if now - v["at"] > TYPING_TTL]:
                self._typing.pop(k, None)
        payload: Dict[str, Any] = {"thread": thread_id, "actor": actor, "name": name,
                                   "until": time.time() + TYPING_TTL}
        if round_n is not None:
            payload["round"] = round_n
        self.publish_ephemeral(review_id, "typing", payload)
        return True

    def typing_people(self, review_id: str, thread_id: str) -> List[Dict[str, str]]:
        now = time.monotonic()
        with self._lock:
            return [{"actor": k[2], "name": v["name"]} for k, v in self._typing.items()
                    if k[0] == review_id and k[1] == thread_id and now - v["at"] <= TYPING_TTL]

    # ---- publishing ----------------------------------------------------
    def publish(self, review_id: str, kind: str, thread_id: str, actor: str, data: Dict[str, Any]) -> int:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        t = time.time()
        with self._publish_lock:      # id assignment and delivery are one step: see __init__
            with db.tx() as conn:
                seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE review_id = ?",
                                       (review_id,)).fetchone()["n"])
                cur = conn.execute("INSERT INTO events (review_id, seq, kind, thread_id, actor, data, at) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (review_id, seq, kind, thread_id or "", actor or "system", payload, t))
                event_id = int(cur.lastrowid)
                conn.execute("DELETE FROM events WHERE review_id = ? AND id NOT IN "
                             "(SELECT id FROM events WHERE review_id = ? ORDER BY id DESC LIMIT ?)",
                             (review_id, review_id, KEEP_EVENTS))
            self._fanout(review_id, (event_id, kind, data))
        return event_id

    def publish_ephemeral(self, review_id: str, kind: str, data: Dict[str, Any]) -> None:
        self._fanout(review_id, (None, kind, data))

    def _offer(self, sub: Subscription, item: Any) -> None:
        if sub.closed:
            return
        try:
            sub.queue.put_nowait(item)
        except queue.Full:
            sub.slow = True
            sub.stop()      # no fence here: this runs on the publisher thread, inside `_publish_lock`

    def _fanout(self, review_id: str, item: Any) -> None:
        with self._lock:
            subs = list(self._live_subs(review_id))
        for sub in subs:
            self._offer(sub, item)

    def latest(self, review_id: str) -> int:
        row = db.connect().execute("SELECT MAX(id) AS id FROM events WHERE review_id = ?", (review_id,)).fetchone()
        return int(row["id"] or 0)

    def replay(self, review_id: str, last: int) -> Tuple[bool, List[Dict[str, Any]], int]:
        """(needs_reset, rows to replay, latest id). A cursor older than the retained window means refetch."""
        rows = [dict(r) for r in db.connect().execute(
            "SELECT id, seq, kind, data FROM events WHERE review_id = ? ORDER BY id", (review_id,)).fetchall()]
        latest = int(rows[-1]["id"]) if rows else 0
        if last and (latest == 0 or last > latest):
            return True, [], latest
        if rows and int(rows[0]["seq"]) > 1 and last and last < int(rows[0]["id"]):
            return True, [], latest
        return False, [r for r in rows if int(r["id"]) > last], latest

    def _visible(self, sub: Subscription, kind: str, data: Any) -> bool:
        allow = sub.allow_round
        if allow is None:
            return True
        r = event_round(kind, data)
        if r is None:
            return kind not in THREAD_KINDS
        return bool(allow(r))

    # ---- the stream loop ----------------------------------------------
    def stream(self, handler: Any, sub: Subscription, hello: Dict[str, Any], replay: List[Dict[str, Any]],
               reset: bool = False, latest: int = 0, interval: Optional[float] = None,
               announce: Optional[int] = None, last: int = 0) -> None:
        """Write hello, the replay (or reset) and then live frames.

        Closure is checked at every emission point, not only on entry: a subscription closed during the handshake,
        during a visibility evaluation or after a frame was dequeued writes nothing. The cursor/floor semantics are
        unchanged: `floor` only moves forward, rows at or below it are dropped, and hidden rows advance it.
        """
        stream = sse.Stream(handler, interval=interval, output=sub.output)
        floor = last

        def emit(event_id: Optional[int], kind: str, data: Any) -> bool:
            nonlocal floor
            if sub.closed:
                return False
            if event_id is not None:
                if event_id <= floor:
                    return True
                floor = event_id
            visible = self._visible(sub, kind, data)
            if sub.closed:
                return False
            return not visible or stream.write(json.dumps(data, ensure_ascii=False), event=kind, id=event_id)

        def emit_rows(rows: Iterable[Any]) -> bool:
            for row in rows:
                try:
                    data = json.loads(row["data"] or "{}")
                except ValueError:
                    data = {}
                if not emit(int(row["id"]), row["kind"], data):
                    return False
            return True

        def emit_reset(latest_id: int, revision: int) -> bool:
            nonlocal floor
            floor = latest_id
            return not sub.closed and stream.write(json.dumps({"rev": revision}), event="reset")

        def catch_up() -> bool:
            needs_reset, rows, tip = self.replay(sub.review_id, floor)
            return emit_reset(tip, 0) if needs_reset else emit_rows(rows)

        try:
            if sub.closed:
                return
            if not stream.write(json.dumps(hello, ensure_ascii=False), event="hello"):
                return
            if reset:
                if not emit_reset(latest, latest if announce is None else announce):
                    return
            elif not emit_rows(replay):
                return
            if sub.closed:
                return
            presence = self.presence(sub.review_id)
            if sub.closed:
                return
            stream.write(json.dumps(presence, ensure_ascii=False), event="presence")
            next_recheck = time.monotonic() + RECHECK_EVERY
            while not sub.closed:
                # An expired grant has no mutation to hook: re-read access on a tick (never per frame, which would be
                # a database read per frame per subscriber). The break runs the finally below, fencing the socket so
                # the browser's EventSource reconnects and the server refuses it.
                if sub.recheck is not None and time.monotonic() >= next_recheck:
                    next_recheck = time.monotonic() + RECHECK_EVERY
                    if not sub.recheck():
                        break
                    # A standalone server and Hermes can share a store. The other process writes the same
                    # durable log but cannot enqueue into our registry. Pick up its writes on the existing tick.
                    if self.latest(sub.review_id) != floor and not catch_up():
                        break
                item = None
                try:
                    item = sub.queue.get(timeout=LOOP_WAIT)
                except queue.Empty:
                    pass
                if sub.closed or item is _SENTINEL:
                    break
                if item is not None:
                    event_id, kind, data = item
                    if event_id is not None and event_id > floor + 1:
                        # Fill any gap before advancing the cursor past a different process's earlier write.
                        if not catch_up():
                            break
                    if not emit(event_id, kind, data):
                        break
                self.refresh(sub)
                if not stream.tick():
                    break
        except Exception:
            log.exception("live stream for %s ended with an error", sub.review_id)
        finally:
            sub.close()
            self.unsubscribe(sub)

    # ---- closer and tests ---------------------------------------------
    def close_sessions(self, session_ids: Iterable[str]) -> None:
        ids = {str(i) for i in session_ids if i}
        if not ids:
            return
        with self._lock:
            # A sign-out is a revocation like any other: an admission in flight, wherever it is, must not register
            # the ended session afterwards. The revision covers that; the seat already taken is closed below.
            self._revoke()
            targets = [s for subs in self._subs.values() for s in subs if s.session_hash in ids]
        for sub in targets:
            sub.close()

    def close_link(self, review_id: str, generation: int) -> None:
        with self._lock:
            self._bump_epoch(review_id)
            self._revoke()
            targets = [s for s in self._subs.get(review_id, []) if not s.owner and s.guest_generation is not None
                       and int(s.guest_generation) <= int(generation)]
        for sub in targets:
            sub.close()

    def close_guests(self, review_id: str) -> None:
        with self._lock:
            self._bump_epoch(review_id)
            self._revoke()
            targets = [s for s in self._subs.get(review_id, []) if not s.owner and s.kind == "guest"]
        for sub in targets:
            sub.close()

    def close_actor(self, actor: str) -> None:
        if not actor:
            return
        with self._lock:
            self._revoke()
            # An identity-signed stream has no session hash, so it can only be found by its actor.
            targets = [s for subs in self._subs.values() for s in subs if s.actor == actor]
        for sub in targets:
            sub.close()

    def reset(self) -> None:
        with self._lock:
            self._revoke()
            subs = [s for group in self._subs.values() for s in group]
            self._subs.clear()
            self._presence.clear()
            self._typing.clear()
            self._typing_sent.clear()
            self._epochs.clear()
        for sub in subs:
            sub.close()


REGISTRY = Registry()


def install() -> None:
    """Install the registry's closer on `streams` (idempotent; re-asserted at every server start and subscribe)."""
    streams.set_closer(REGISTRY)


def reset() -> None:
    REGISTRY.reset()
    install()


def publish(review_id: str, kind: str, thread_id: str, actor: str, data: Dict[str, Any]) -> int:
    return REGISTRY.publish(review_id, kind, thread_id, actor, data)


def publish_ephemeral(review_id: str, kind: str, data: Dict[str, Any]) -> None:
    REGISTRY.publish_ephemeral(review_id, kind, data)


def typing(review_id: str, thread_id: str, actor: str, name: str, round_n: Optional[int] = None) -> bool:
    return REGISTRY.typing(review_id, thread_id, actor, name, round_n)


def latest(review_id: str) -> int:
    return REGISTRY.latest(review_id)


def replay(review_id: str, last: int) -> Tuple[bool, List[Dict[str, Any]], int]:
    return REGISTRY.replay(review_id, last)


def presence(review_id: str) -> Dict[str, Any]:
    return REGISTRY.presence(review_id)
