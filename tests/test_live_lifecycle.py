"""Independent real-HTTP SSE policy-change barriers; hooks only hold existing scheduling boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import pytest
from authkit import account, peer, signed_in
from conftest import http
from mockreview import db, live, threads, users
from test_live import Stream, clean_live
from test_share_dialog import add_rounds, join, owner_post
from test_threads_scope import next_persisted_or_eof


@pytest.mark.parametrize('initial', ['latest', '3'])
@pytest.mark.parametrize('phase', ['registration', 'replay', 'midreplay', 'live'])
@pytest.mark.parametrize('restrict', [True, False])
def test_policy_change_is_a_barrier_during_handshake_and_replay(server, review, tmp_path, peer, monkeypatch,
                                                               initial, phase, restrict):
    rid = review['id']
    owner = users.owner_user()
    add_rounds(review, tmp_path, 2)
    t = threads.create(rid, f"user:{owner['id']}", owner['name'], 'owner', 'PRIVATE ROUND THREE BEFORE REPIN',
                       round_n=3, anchor={'block':'a', 'x':.2, 'y':.3})
    link = users.share('review', rid, 'create', 'viewer', round=initial)
    guest = join(server, link['path'])
    headers = signed_in(guest, False)
    if phase == 'registration':
        headers['Last-Event-ID'] = str(live.latest(rid))
    entered, release = threading.Event(), threading.Event()
    observed = []
    method = 'subscribe' if phase == 'registration' else ('_visible' if phase in ('midreplay','live') else 'stream')
    original = getattr(live.REGISTRY, method)

    def hold(*args, **kwargs):
        if phase in ('midreplay','live') and args[1] != 'thread.replied':
            return original(*args, **kwargs)
        entered.set()
        assert release.wait(6), 'reviewer scheduling barrier timed out'
        if phase == 'replay':
            observed.append({'sub_closed_before_replay':args[1].closed})
        elif phase in ('midreplay','live'):
            observed.append({'sub_closed_before_visibility_returns':args[0].closed})
        return original(*args, **kwargs)

    monkeypatch.setattr(live.REGISTRY, method, hold)
    if phase == 'midreplay':
        status, _, body = owner_post(server, f"/r/{rid}/api/threads/{t['id']}/reply",
                                    {'text':'SECOND REPLAY SECRET'}, peer)
        assert status == 200, body
    def open_stream():
        try:
            return Stream(server, f'/r/{rid}/events', headers)
        finally:
            db.close_all()
    s = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(open_stream)
            try:
                if phase == 'live':
                    s = future.result(timeout=4)
                    assert s.until('hello')
                    assert s.until('thread.created')
                    status, _, body = owner_post(server, f"/r/{rid}/api/threads/{t['id']}/reply",
                                                {'text':'QUEUED LIVE SECRET'}, peer)
                    assert status == 200, body
                assert entered.wait(4), 'request never reached the held boundary'
                change = {'action':'update', 'round':'1'} if restrict else {'action':'update', 'round':initial}
                status, _, body = owner_post(server, f'/r/{rid}/api/share', change, peer)
                assert status == 200, body
                status, _, body = http('GET', f'{server}/r/{rid}/state.json', headers=signed_in(guest,False))
                assert status == 200, body
                state = json.loads(body)
                if restrict:
                    assert [r['n'] for r in state['rounds']] == [1]
                if phase == 'registration':
                    # Produced only after the owner has received success: no network-buffer ambiguity.
                    status, _, body = owner_post(server, f"/r/{rid}/api/threads/{t['id']}/reply",
                                                {'text':'NEW SECRET AFTER SUCCESSFUL REPIN'}, peer)
                    assert status == 200, body
            finally:
                release.set()
            s = future.result(timeout=6)
        first = next_persisted_or_eof(s)
        event = next_persisted_or_eof(s) if phase == 'midreplay' and not first.get('eof') else first
        print('LIFECYCLE', json.dumps({'phase':phase, 'initial':initial, 'restrict':restrict,
              'state_rounds':[r['n'] for r in state['rounds']], 'observed':observed, 'wire':event}))
        if restrict:
            assert event.get('eof') or 'PRIVATE ROUND THREE' not in event.get('data',''), \
                'successful re-pin must bar hidden replay/late subscription delivery'
            assert 'NEW SECRET AFTER SUCCESSFUL REPIN' not in event.get('data','')
        else:
            assert not event.get('eof') and 'PRIVATE ROUND THREE' in event.get('data',''), \
                'no-op updates must preserve allowed replay and live delivery'
    finally:
        release.set()
        if s is not None:
            s.close()
        live.reset()


def test_admission_and_emission_barriers_are_race_free(server, review, tmp_path, peer, monkeypatch):
    """Force both critical interleavings with events, then race publish against close and subscribe."""
    rid = review["id"]
    owner = users.owner_user()
    add_rounds(review, tmp_path, 2)                     # rounds 1, 2, 3
    hidden = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "HIDDEN ROUND THREE",
                            round_n=3, anchor={"block": "a", "x": .2, "y": .3})
    visible = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "VISIBLE ROUND ONE",
                             round_n=1, anchor={"block": "a", "x": .2, "y": .3})
    link = users.share("review", rid, "create", "viewer", round="latest")
    guest = join(server, link["path"])
    headers = signed_in(guest, False)

    # close-first at the registry: admission sees the bumped epoch. It must not register, lease presence or take
    # a member's seat, so all 24 member seats and the owner's one are still available afterwards.
    reg = live.Registry()
    stale = reg.epoch("barrier")
    reg.close_guests("barrier")
    refused = reg.subscribe("barrier", "guest:1", "Guest", "guest", guest_generation=1, epoch=stale)
    assert refused.closed, "a stale epoch must return a closed subscription"
    assert reg.count("barrier") == 0, "a stale epoch must not register"
    assert reg.presence("barrier") == {"people": [], "count": 0}, "a stale epoch must not lease presence"
    for i in range(live.MAX_STREAMS - live.RESERVED_FOR_OWNER):
        reg.subscribe("barrier", f"user:{i + 2}", "Member", "user")
    reg.subscribe("barrier", "user:1", "Owner", "user", owner=True)
    assert reg.count("barrier") == live.MAX_STREAMS, "a refused admission must not consume a seat"
    reg.reset()

    # close-first over HTTP: the request captures the epoch, then the policy changes, then admission runs.
    entered, release = threading.Event(), threading.Event()
    real_subscribe = live.REGISTRY.subscribe

    def held_subscribe(*args, **kwargs):
        entered.set()
        assert release.wait(6), "admission barrier timed out"
        return real_subscribe(*args, **kwargs)

    def open_stream():
        try:
            return Stream(server, f"/r/{rid}/events", headers)
        finally:
            db.close_all()

    monkeypatch.setattr(live.REGISTRY, "subscribe", held_subscribe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(open_stream)
        assert entered.wait(4), "the request never reached admission"
        status, _, body = owner_post(server, f"/r/{rid}/api/share", {"action": "update", "round": "1"}, peer)
        assert status == 200, body
        release.set()
        s = future.result(timeout=6)
    try:
        assert s.status == 200, s.status
        assert next_persisted_or_eof(s).get("eof"), "stale authorization must write nothing and EOF"
    finally:
        s.close()
    assert live.REGISTRY.count(rid) == 0
    assert live.REGISTRY.presence(rid) == {"people": [], "count": 0}
    status, _, body = http("GET", f"{server}/r/{rid}/state.json", headers=signed_in(guest, False))
    assert status == 200, body
    assert [r["n"] for r in json.loads(body)["rounds"]] == [1]

    # The reconnect computes the new scope and works.
    s2 = Stream(server, f"/r/{rid}/events", headers)
    try:
        assert s2.until("hello") is not None
        status, _, body = owner_post(server, f"/r/{rid}/api/threads/{visible['id']}/reply",
                                     {"text": "VISIBLE AFTER RECONNECT"}, peer)
        assert status == 200, body
        frame = s2.until("thread.replied", timeout=4)
        assert frame is not None and "VISIBLE AFTER RECONNECT" in frame["data"], frame
        assert "HIDDEN ROUND THREE" not in frame["data"], frame
    finally:
        s2.close()

    # admission-first: registration succeeds, then the closer runs while admission is still returning.
    before = live.REGISTRY.count(rid)
    entered2, release2 = threading.Event(), threading.Event()

    def registered_then_hold(*args, **kwargs):
        sub = real_subscribe(*args, **kwargs)
        entered2.set()
        assert release2.wait(6), "close barrier timed out"
        return sub

    monkeypatch.setattr(live.REGISTRY, "subscribe", registered_then_hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(open_stream)
        assert entered2.wait(4), "the subscription was never registered"
        assert live.REGISTRY.count(rid) == before + 1, "registration must happen before the close"
        status, _, body = owner_post(server, f"/r/{rid}/api/share", {"action": "update", "round": "latest"}, peer)
        assert status == 200, body
        status, _, body = owner_post(server, f"/r/{rid}/api/threads/{hidden['id']}/reply",
                                     {"text": "HIDDEN REPLY AFTER CLOSE"}, peer)
        assert status == 200, body
        release2.set()
        s = future.result(timeout=6)
    try:
        assert s.status == 200, s.status
        assert next_persisted_or_eof(s).get("eof"), "a closed subscription must write nothing"
    finally:
        s.close()
    assert live.REGISTRY.count(rid) == 0

    # publish during close: every thread completes within its join, the member stream gets both events once, in order.
    member = account(review_id=rid, role="viewer", name="Member control")
    m = Stream(server, f"/r/{rid}/events",
               dict(signed_in(member, False), **{"Last-Event-ID": str(live.latest(rid))}))
    racing = []
    try:
        assert m.until("hello") is not None
        barrier = threading.Barrier(3)
        errors = []

        def publish_loop():
            try:
                barrier.wait(4)
                for n in (1, 2):
                    live.publish(rid, "thread.status", "", "system", {"n": n})
            except BaseException as exc:
                errors.append(repr(exc))

        def close_loop():
            try:
                barrier.wait(4)
                live.REGISTRY.close_guests(rid)
            except BaseException as exc:
                errors.append(repr(exc))

        def subscribe_loop():
            try:
                barrier.wait(4)
                racing.append(live.REGISTRY.subscribe(rid, "guest:race", "Race guest", "guest",
                                                      guest_generation=1, epoch=live.REGISTRY.epoch(rid)))
            except BaseException as exc:
                errors.append(repr(exc))

        workers = [threading.Thread(target=fn) for fn in (publish_loop, close_loop, subscribe_loop)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(6)
        assert not any(w.is_alive() for w in workers), "publish/close/subscribe deadlocked"
        assert not errors, errors
        frames = [m.until("thread.status", timeout=4) for _ in (0, 1)]
        assert all(frames), frames
        assert [json.loads(f["data"])["n"] for f in frames] == [1, 2], frames
        assert m.until("thread.status", timeout=0.5) is None, "the member stream saw an event twice"
    finally:
        for sub in racing:
            sub.close()
        m.close()
