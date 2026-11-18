"""Admission and emission barriers: a revoked credential cannot take a seat, and a closed stream writes no byte.

Every case here runs real HTTP and SQLite; the monkeypatched hooks only widen an existing scheduling boundary
(`REGISTRY.epoch`, `REGISTRY.subscribe`, `sse._emit`) so the two interleavings the board must survive are forced
instead of raced for. The live-stream layer, review rounds 6 and 7.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest
from authkit import account, peer, signed_in  # noqa: F401
from conftest import http
from mockreview import db, live, sse, threads, users
from test_live import Stream, clean_live  # noqa: F401
from test_share_dialog import add_rounds, join, owner_post
from test_threads_scope import next_persisted_or_eof


def next_frame_or_eof(stream, timeout=4):
    """One parsed frame, or {'eof': True} for a genuine socket close, never silence."""
    end = time.monotonic() + timeout
    fields, data = {}, []
    while time.monotonic() < end:
        raw = stream._readline(end)
        assert raw is not None, 'expected actual frame or EOF, not silence'
        if raw == b'':
            return {'eof': True}
        line = raw.decode().rstrip('\r\n')
        if not line:
            if fields:
                fields['data'] = '\n'.join(data)
                return fields
            continue
        key, sep, value = line.partition(':')
        if sep:
            if key == 'data':
                data.append(value.lstrip())
            elif key:
                fields[key] = value.lstrip()
    raise AssertionError('no frame or EOF')


def open_async(server, rid, headers):
    try:
        return Stream(server, f'/r/{rid}/events', headers)
    finally:
        db.close_all()


@pytest.mark.parametrize('initial', ['latest', '3'])
@pytest.mark.parametrize('phase', ['hello', 'reset', 'presence', 'replay', 'live'])
@pytest.mark.parametrize('restrict', [True, False])
def test_restriction_linearizes_with_actual_emit(server, review, tmp_path, peer, monkeypatch,  # noqa: F811
                                                initial, phase, restrict):
    """Hold one frame inside the real write path, re-pin the link, then let the write go: a restricted stream must
    already be closed at the socket, so the held frame and every later one write nothing. A no-op update keeps it."""
    rid = review['id']
    owner = users.owner_user()
    add_rounds(review, tmp_path, 2)
    thread = threads.create(rid, f"user:{owner['id']}", owner['name'], 'owner', 'PRIVATE THREE BEFORE RESTRICTION',
                            round_n=3, anchor={'block': 'a', 'x': .2, 'y': .3})
    link = users.share('test', rid, 'create', 'viewer', round=initial)
    guest = join(server, link['path'])
    headers = signed_in(guest, False)
    if phase == 'reset':
        headers['Last-Event-ID'] = str(live.latest(rid) + 999)
    elif phase == 'live':
        headers['Last-Event-ID'] = str(live.latest(rid))
    target = {'hello': 'hello', 'reset': 'reset', 'presence': 'presence',
              'replay': 'thread.created', 'live': 'thread.replied'}[phase]
    entered, release = threading.Event(), threading.Event()
    original = sse._emit
    held = []

    def delayed_actual_emit(handler, frame):
        if f'event: {target}\n' in frame and not held:
            held.append(frame)
            entered.set()
            assert release.wait(6), 'actual emission schedule timed out'
        return original(handler, frame)

    monkeypatch.setattr(sse, '_emit', delayed_actual_emit)
    stream = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(open_async, server, rid, headers)
            try:
                stream = future.result(timeout=4)
                if phase == 'live':
                    assert stream.until('hello')
                    status, _, body = owner_post(server, f"/r/{rid}/api/threads/{thread['id']}/reply",
                                                {'text': 'PRIVATE DEQUEUED LIVE THREE'}, peer)
                    assert status == 200, body
                assert entered.wait(4), 'never reached actual emit boundary'
                change = {'action': 'update', 'round': '1' if restrict else initial}
                status, _, body = owner_post(server, f'/r/{rid}/api/share', change, peer)
                assert status == 200, body
                active = live.REGISTRY._subs.get(rid, [])
                assert len(active) == 1
                closed = active[0].closed
                assert closed == restrict
                status, _, body = http('GET', f'{server}/r/{rid}/state.json', headers=signed_in(guest, False))
                assert status == 200, body
                expected_rounds = [1] if restrict else ([1, 2, 3] if initial == 'latest' else [3])
                assert [r['n'] for r in json.loads(body)['rounds']] == expected_rounds
            finally:
                release.set()
            stream = future.result(timeout=6)
        # The held target frame was not on the wire when restriction returned 200.
        event = next_frame_or_eof(stream)
        while not event.get('eof') and event.get('event') != target:
            event = next_frame_or_eof(stream)
        print('ACTUAL_EMIT', json.dumps({'initial': initial, 'phase': phase, 'restrict': restrict,
                                         'closed_at_success': closed, 'wire': event}))
        if restrict:
            assert event.get('eof'), 'a frame entered actual socket emission only after successful restriction'
        else:
            assert event.get('event') == target, 'no-op update must preserve emission'
    finally:
        release.set()
        if stream:
            stream.close()
        live.reset()


@pytest.mark.parametrize('kind', ['guest', 'member'])
@pytest.mark.parametrize('phase', ['before_epoch', 'before_subscribe'])
@pytest.mark.parametrize('revoke', [True, False])
def test_signed_out_inflight_request_cannot_become_live(server, review, peer, monkeypatch,  # noqa: F811
                                                       kind, phase, revoke):
    """A request authorized before a sign-out, delayed at admission, must get EOF, not the next private comment.
    The unaffected member's stream keeps receiving, and the no-revoke control still admits."""
    rid = review['id']
    owner = users.owner_user()
    thread = threads.create(rid, f"user:{owner['id']}", owner['name'], 'owner', 'OLD ORDINARY COMMENT',
                            anchor={'block': 'a', 'x': .2, 'y': .3})
    if kind == 'guest':
        link = users.share('test', rid, 'create', 'viewer')
        subject = join(server, link['path'])
    else:
        subject = account(review_id=rid, role='viewer', name='Device member')
    control = account(review_id=rid, role='viewer', name='Unaffected member')
    control_stream = Stream(server, f'/r/{rid}/events', dict(signed_in(control, False),
                              **{'Last-Event-ID': str(live.latest(rid))}))
    assert control_stream.until('hello')
    entered, release = threading.Event(), threading.Event()
    method = 'epoch' if phase == 'before_epoch' else 'subscribe'
    original = getattr(live.REGISTRY, method)

    def delayed_admission(*args, **kwargs):
        entered.set()
        assert release.wait(6), 'auth/admission schedule timed out'
        return original(*args, **kwargs)

    monkeypatch.setattr(live.REGISTRY, method, delayed_admission)
    headers = dict(signed_in(subject, False), **{'Last-Event-ID': str(live.latest(rid))})
    stream = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(open_async, server, rid, headers)
            try:
                assert entered.wait(4)
                if revoke:
                    status, _, body = http('POST', server + '/api/logout', {}, headers=signed_in(subject))
                    assert status == 200, body
                    assert users.session(subject['session']) is None
                    fresh_status, _, _ = http('GET', f'{server}/r/{rid}/api/threads', headers=signed_in(subject, False))
                    assert fresh_status != 200, 'fresh request must no longer be authorized'
                else:
                    status, _, body = http('GET', server + '/api/me', headers=signed_in(subject, False))
                    assert status == 200, body
                    fresh_status = 200
                status, _, body = owner_post(server, f"/r/{rid}/api/threads/{thread['id']}/reply",
                                            {'text': 'NEW SECRET AFTER LOGOUT SUCCESS'}, peer)
                assert status == 200, body
            finally:
                release.set()
            stream = future.result(timeout=6)
        unaffected = next_persisted_or_eof(control_stream)
        assert 'NEW SECRET AFTER LOGOUT SUCCESS' in unaffected.get('data', '')
        event = next_persisted_or_eof(stream)
        print('SESSION_ADMISSION', json.dumps({'kind': kind, 'phase': phase, 'revoke': revoke,
              'fresh_status': fresh_status, 'unaffected_member': 'received', 'wire': event}))
        if revoke:
            assert event.get('eof'), 'a successfully signed-out device received a newly posted private comment'
        else:
            assert 'NEW SECRET AFTER LOGOUT SUCCESS' in event.get('data', '')
    finally:
        release.set()
        if stream:
            stream.close()
        control_stream.close()
        live.reset()


@pytest.mark.parametrize('action', ['regenerate', 'stop', 'members', 'noop'])
def test_guest_auth_before_epoch_cannot_outlive_link_revocation(server, review, peer,  # noqa: F811
                                                               monkeypatch, action):
    """Guest authorization read before the epoch, then the link is regenerated, stopped or narrowed to members:
    the delayed admission must get EOF. A no-op update must still deliver."""
    rid = review['id']
    owner = users.owner_user()
    thread = threads.create(rid, f"user:{owner['id']}", owner['name'], 'owner', 'OLD LINK COMMENT',
                            anchor={'block': 'a', 'x': .2, 'y': .3})
    link = users.share('test', rid, 'create', 'viewer')
    guest = join(server, link['path'])
    entered, release = threading.Event(), threading.Event()
    original = live.REGISTRY.epoch

    def after_auth_before_epoch(*args, **kwargs):
        entered.set()
        assert release.wait(6)
        return original(*args, **kwargs)

    monkeypatch.setattr(live.REGISTRY, 'epoch', after_auth_before_epoch)
    stream = None
    headers = dict(signed_in(guest, False), **{'Last-Event-ID': str(live.latest(rid))})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(open_async, server, rid, headers)
            try:
                assert entered.wait(4)
                body = {'action': action} if action in ('stop', 'regenerate') else \
                       ({'action': 'update', 'audience': 'members'} if action == 'members' else
                        {'action': 'update', 'round': 'latest'})
                status, _, result = owner_post(server, f'/r/{rid}/api/share', body, peer)
                assert status == 200, result
                fresh, _, _ = http('GET', f'{server}/r/{rid}/api/threads', headers=signed_in(guest, False))
                assert fresh == 200 if action == 'noop' else fresh in (401, 404)
                status, _, result = owner_post(server, f"/r/{rid}/api/threads/{thread['id']}/reply",
                                              {'text': 'NEW SECRET AFTER LINK REVOCATION'}, peer)
                assert status == 200, result
            finally:
                release.set()
            stream = future.result(timeout=6)
        event = next_persisted_or_eof(stream)
        print('LINK_ADMISSION', json.dumps({'action': action, 'fresh_status': fresh, 'wire': event}))
        if action == 'noop':
            assert 'NEW SECRET AFTER LINK REVOCATION' in event.get('data', '')
        else:
            assert event.get('eof'), 'revoked guest auth captured before epoch still receives new private data'
    finally:
        release.set()
        if stream:
            stream.close()
        live.reset()
