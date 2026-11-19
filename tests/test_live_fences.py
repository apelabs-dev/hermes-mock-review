"""Retained round-7 schedules: transport fencing and concurrent close completion (card E4, round 8)."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import select
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from authkit import account, peer, signed_in
from conftest import http
from mockreview import db, live, sse, threads, users
from test_live import Stream, clean_live
from test_share_dialog import add_rounds, join, owner_post
from test_live_barriers import next_frame_or_eof
from test_threads_scope import next_persisted_or_eof


def open_stream(server, rid, headers):
    try:
        return Stream(server, f'/r/{rid}/events', headers)
    finally:
        db.close_all()


@pytest.mark.parametrize('prune', [False, True])
@pytest.mark.parametrize('overlap', [False, True])
def test_concurrent_restrictions_wait_for_socket_fence(server, review, tmp_path, peer, monkeypatch, overlap, prune):
    rid = review['id']
    owner = users.owner_user()
    add_rounds(review, tmp_path, 2)
    threads.create(rid, f"user:{owner['id']}", owner['name'], 'owner', 'PRIVATE ROUND THREE HELD BEFORE WRITE',
                   round_n=3, anchor={'block':'a','x':.2,'y':.3})
    link = users.share('test', rid, 'create', 'viewer', round='latest')
    guest = join(server, link['path'])
    entered_emit, release_emit = threading.Event(), threading.Event()
    entered_close, release_close = threading.Event(), threading.Event()
    original_emit, original_close = sse._emit, sse.Output.close
    selected = []

    def hold_emit(handler, frame):
        if 'event: thread.created\n' in frame and not selected:
            selected.append(handler.sse_output)
            entered_emit.set()
            assert release_emit.wait(8)
        return original_emit(handler, frame)

    def hold_close(output):
        if overlap and selected and output is selected[0] and not entered_close.is_set():
            entered_close.set()
            assert release_close.wait(8)
        return original_close(output)

    monkeypatch.setattr(sse, '_emit', hold_emit)
    monkeypatch.setattr(sse.Output, 'close', hold_close)
    stream = None
    wire = None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            opened = pool.submit(open_stream, server, rid, signed_in(guest, False))
            try:
                stream = opened.result(timeout=3)
                assert entered_emit.wait(3)
                if overlap:
                    first = pool.submit(owner_post, server, f'/r/{rid}/api/share', {'action':'update','round':'2'}, peer)
                    assert entered_close.wait(3)
                    assert not first.done(), 'first restriction is still completing its socket fence'
                    if prune:
                        # Ordinary seat/presence/publish paths prune closed subscriptions, even though this one's
                        # transport close is not done. A later closer must not lose its completion obligation.
                        live.REGISTRY.count(rid)
                second = pool.submit(owner_post, server, f'/r/{rid}/api/share', {'action':'update','round':'1'}, peer)
                try:
                    status, _, body = second.result(timeout=.25)
                except TimeoutError:
                    # A correct implementation may wait for the first close rather than finish its fence itself.
                    release_close.set()
                    status, _, body = second.result(timeout=3)
                assert status == 200, body
                status, _, body = http('GET', f'{server}/r/{rid}/state.json', headers=signed_in(guest, False))
                assert status == 200 and [r['n'] for r in json.loads(body)['rounds']] == [1]
                release_emit.set()
                wire = next_frame_or_eof(stream)
                while not wire.get('eof') and wire.get('event') != 'thread.created':
                    wire = next_frame_or_eof(stream)
                print('OVERLAPPING_CLOSE', json.dumps({'overlap':overlap,'success_state':[1],
                      'output_closed_at_wire':selected[0].closed,'wire':wire}))
            finally:
                release_emit.set()
                release_close.set()
            if overlap:
                assert first.result(timeout=4)[0] == 200
    finally:
        release_emit.set()
        release_close.set()
        if stream:
            stream.close()
        live.reset()
    assert wire.get('eof'), 'second successful restriction skipped an unfinished close and leaked hidden round 3'


@pytest.mark.parametrize('revocation', ['logout', 'repin', 'unrelated'])
def test_revocation_after_final_auth_before_registration(server, review, tmp_path, peer, monkeypatch, revocation):
    rid = review['id']
    add_rounds(review, tmp_path, 2)
    link = users.share('test', rid, 'create', 'viewer')
    guest = join(server, link['path'])
    unrelated = account(review_id=rid, role='viewer', name='Other account')
    entered, release = threading.Event(), threading.Event()
    original = live.REGISTRY.subscribe
    def delayed_final_auth(*args, **kwargs):
        admit = kwargs['admit']
        def held_admit():
            allowed = admit()
            assert allowed
            entered.set()
            assert release.wait(6)
            return allowed
        kwargs['admit'] = held_admit
        return original(*args, **kwargs)
    monkeypatch.setattr(live.REGISTRY, 'subscribe', delayed_final_auth)
    stream = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            opening = pool.submit(open_stream, server, rid, signed_in(guest, False))
            try:
                assert entered.wait(3)
                if revocation == 'repin':
                    assert owner_post(server, f'/r/{rid}/api/share', {'action':'update','round':'1'}, peer)[0] == 200
                else:
                    subject = guest if revocation == 'logout' else unrelated
                    assert http('POST', server+'/api/logout', {}, headers=signed_in(subject))[0] == 200
            finally:
                release.set()
            stream = opening.result(timeout=4)
        wire = next_frame_or_eof(stream)
        assert wire.get('eof')
        assert live.REGISTRY.count(rid) == 0
        monkeypatch.setattr(live.REGISTRY, 'subscribe', original)
        if revocation == 'unrelated':
            # One unrelated epoch change over-refuses only the in-flight admission; a reconnect can take a seat.
            stream.close()
            stream = Stream(server, f'/r/{rid}/events', signed_in(guest, False))
            assert stream.until('hello')
            assert live.REGISTRY.count(rid) == 1
        print('FINAL_AUTH_REGISTRATION',revocation,'EOF; seats0; unrelated reconnect succeeds' if revocation=='unrelated' else 'EOF; seats0')
    finally:
        release.set()
        if stream:
            stream.close()
        live.reset()


# A loopback writer cannot be stalled by socket buffers alone. Windows completes a blocking send from the user
# buffer, and its receive window (auto-tuned on the accepted socket) grew past the old 8 MiB frame while the 4 KiB
# SO_RCVBUF on the listener was not inherited by the accepted end, so the send finished instead of blocking. The
# fill below is the stall instead: the pipe is pushed full before it is handed over, and full means the socket is
# still unwritable once the bytes already in flight have settled into the peer's receive queue.
FILL_CAP = 64 * 1024 * 1024
FULL_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, 10035}      # 10035 is WSAEWOULDBLOCK on Windows
SETTLE_SECONDS = .2      # time for bytes the kernel already accepted to reach the peer's receive queue


def writable(sock, timeout=0.0):
    """True when the kernel would accept at least one more byte on ``sock`` now (or within ``timeout``)."""
    return bool(select.select([], [sock], [], timeout)[1])


def fill_pipe(sock):
    """Send until the pipe is full and stays full, so a blocking writer on this socket cannot make progress.

    A refused send is not proof by itself: bytes the kernel has already accepted are still in flight to the peer,
    and room reappears as they land. The pipe counts as full only if the socket is still unwritable
    ``SETTLE_SECONDS`` later; if room appeared, the fill keeps going. Returns the bytes the pipe took. A pipe that
    still had room raises instead of being handed over: a test that believes its peer is stalled only because a
    send it never watched "should" block is exactly what failed on Windows.
    """
    chunk = b'x' * 65536
    sent = 0
    sock.setblocking(False)
    try:
        while sent < FILL_CAP:
            try:
                sent += sock.send(chunk)
            except OSError as e:
                if e.errno not in FULL_ERRNOS:
                    raise
                if writable(sock, SETTLE_SECONDS):
                    continue    # in-flight bytes landed and made room again; keep filling
                return sent
    finally:
        sock.setblocking(True)
    raise AssertionError(
        'the loopback pipe accepted %d bytes without ever refusing one, so it cannot be handed over full '
        '(%s, SO_RCVBUF %r, SO_SNDBUF %r)' % (
            sent, sys.platform,
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)))


def stalled_pair():
    """A connected loopback pair whose pipe is already full: the reader never reads and a writer cannot send.

    ``socketpair()`` is not portable enough for this: on Windows it is TCP over a listener the test does not control,
    and the receive window auto-tunes. The small buffers below let ``fill_pipe`` finish in a few KiB, but they are
    not the stall: the fill is, so the pair is stalled by construction on every OS. The client end is the stream's
    connection; the accepted end is the peer. Returns the bytes the pipe took before it stayed refused.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client.connect(listener.getsockname())
        except BaseException:
            client.close()
            raise
        try:
            peer, _ = listener.accept()
        except BaseException:
            client.close()
            raise
    finally:
        listener.close()
    try:
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)   # an accepted socket need not inherit it
        filled = fill_pipe(client)
    except BaseException:
        client.close()
        peer.close()
        raise
    return client, peer, filled


@pytest.mark.parametrize('socket_timeout', [None, 30], ids=['blocking', 'timeout'])
def test_actual_stalled_socket_is_aborted_without_blocking_other_streams(socket_timeout):
    left, right, filled = stalled_pair()
    if socket_timeout is not None:
        left.settimeout(socket_timeout)       # the production handler mode: StreamRequestHandler.setup()
        assert left.gettimeout() == socket_timeout
    handover_writable = writable(left, .05)
    assert not handover_writable, 'the pipe must still be full (unwritable) when the writer starts'
    output = sse.Output()
    entered = threading.Event()
    class Writer:
        def write(self, data):
            entered.set()
            left.sendall(data)
        def flush(self):
            pass
    output.bind(SimpleNamespace(connection=left,wfile=Writer()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(output.emit, 'data: '+('x' * (8 * 1024 * 1024))+'\n\n')
        try:
            assert entered.wait(2)
            time.sleep(.05)
            assert not pending.done(), 'peer must actually be stalled before closure'
            start = time.monotonic()
            output.close()
            elapsed = time.monotonic()-start
            assert pending.result(timeout=1) is False
            assert elapsed < 1
            assert output.emit('data: AFTER SUCCESS\n\n') is False
            print('STALLED_SOCKET_FILLED_BYTES',filled)
            print('STALLED_SOCKET_WRITABLE_AT_HANDOVER',handover_writable)
            print('STALLED_SOCKET_CLOSE_SECONDS',elapsed)
        finally:
            left.close()
            right.close()


def test_close_closes_the_descriptor_under_a_read_stream_and_survives_teardown():
    """A real handler keeps `rfile = connection.makefile('rb', -1)`, which holds a `socket._io_refs` reference: a
    plain `connection.close()` only marks the socket closed and defers the descriptor. The fence must still wake the
    parked send AND release the descriptor, and the handler's own teardown must run cleanly against the dead object."""
    sock, peer, filled = stalled_pair()
    rfile = sock.makefile('rb', -1)
    wfile = sock.makefile('wb', -1)
    assert sock._io_refs >= 1, 'the handler must hold a read stream, as http.server always does'
    output = sse.Output()
    entered = threading.Event()
    class Writer:
        def write(self, data):
            entered.set()
            wfile.write(data)
        def flush(self):
            wfile.flush()
    output.bind(SimpleNamespace(connection=sock,wfile=Writer()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(output.emit, 'data: '+('x' * (8 * 1024 * 1024))+'\n\n')
        try:
            assert entered.wait(2)
            time.sleep(.05)
            assert not pending.done(), 'peer must actually be stalled before closure'
            start = time.monotonic()
            output.close()
            elapsed = time.monotonic()-start
            assert pending.result(timeout=1) is False
            assert elapsed < 1
            assert sock.fileno() == -1, 'the descriptor must be closed, not merely shut down'
            assert output.emit('data: AFTER SUCCESS\n\n') is False
            # http.server's finish() closes both file objects; socketserver's shutdown_request() then calls
            # shutdown/close and absorbs the OSError the invalidated connection raises. None of that may escape.
            rfile.close()
            wfile.close()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            print('READ_STREAM_FILLED_BYTES',filled)
            print('READ_STREAM_CLOSE_SECONDS',elapsed)
        finally:
            for stream in (rfile, wfile, sock, peer):
                try:
                    stream.close()
                except OSError:
                    pass
