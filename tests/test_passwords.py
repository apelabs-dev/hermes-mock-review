"""Password gates use mocked here.now HTTP only; intent survives failed requests."""
import io
import json
import math
import re
import subprocess
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_plugin
from mockreview import api, cli, config, herenow, passwords, settings, store
from test_plugin import FakeCtx, _decisions


@pytest.fixture
def published(env, variants, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "herenow_publish_script", lambda: tmp_path / "publish.sh")
    def run(argv, **kw):
        calls.append(("publish", argv, kw))
        return SimpleNamespace(returncode=0, stderr="publish_result.slug=test-site-ab12\n"
            "publish_result.site_url=https://test-site-ab12.here.now/\npublish_result.auth_mode=authenticated\n")
    monkeypatch.setattr(herenow.subprocess, "run", run)
    def remote(method, path, **kw):
        calls.append((method, path, kw))
        return {}
    monkeypatch.setattr(herenow, "_api", remote)
    spec = [store.parse_variant_spec(v) for v in variants]
    data = store.create_review("Protected review", "", {"platform": "discord", "chat_id": "test-chat"},
                               "herenow", spec)
    return data, calls


def test_generation_entropy_and_independent_secret_choices(monkeypatch):
    words = Path(passwords.__file__).with_name("password_words.txt").read_text(encoding="utf-8").splitlines()
    assert len(words) == len(set(words)) == 7776
    assert all(re.fullmatch('[a-z-]+', w) for w in words)
    assert math.log2(len(words)) * 4 > 51
    choices = []
    monkeypatch.setattr(passwords.secrets, "choice", lambda seq: choices.append(seq) or seq[len(choices)])
    value = passwords.generate()
    assert len(choices) == 4 and value == ' '.join(words[i] for i in range(1, 5))


def test_default_publish_reassert_and_no_browser_or_list_leak(published, tmp_path):
    data, calls = published
    rid = data['id']
    value = data['herenow']['password']
    assert len(value.split()) == 4
    assert store.review_url(data) == ''
    data = herenow.publish(rid)
    assert calls[1] == ('PATCH', '/publish/test-site-ab12/metadata', {'body': {'password': value}})
    assert data['herenow']['password_applied'] is True
    assert herenow.password_result(data)['password'] == value
    assert value not in json.dumps(store.public_state(data))
    assert value not in json.dumps(api.list_reviews())
    assert value not in json.dumps(api.review_link(rid))
    site = herenow.build_site(data, tmp_path / 'site')
    assert all(value.encode() not in p.read_bytes() for p in site.rglob('*') if p.is_file())
    herenow.publish(rid)
    assert [c[0] for c in calls] == ['publish', 'PATCH', 'publish', 'PATCH']
    assert '--slug' in calls[2][1]
    assert value not in str(calls[0][1:])  # neither helper argv nor environment


@pytest.mark.parametrize('failure', ['http', 'timeout', 'invalid-json'])
def test_failed_gate_keeps_slug_flags_and_sanitizes_then_retries(published, monkeypatch, caplog, failure):
    data, calls = published
    rid, value = data['id'], data['herenow']['password']
    def remote(*a, **kw):
        if failure == 'http':
            raise herenow.HereNowError(value)
        if failure == 'timeout':
            raise TimeoutError(value)
        raise ValueError(value)
    monkeypatch.setattr(herenow, '_api', remote)
    with pytest.raises(herenow.HereNowError) as err:
        herenow.publish(rid)
    assert value not in str(err.value) and value not in caplog.text
    failed = store.load(rid)
    hn = failed['herenow']
    assert hn['slug'] == 'test-site-ab12' and hn['password'] == value
    assert hn['password_status'] == 'error' and hn['password_applied'] is None
    assert store.review_url(failed) == '' and api.list_reviews()[0]['url'] == ''
    assert herenow.password_result(failed)['password'] is None
    monkeypatch.setattr(herenow, '_api', lambda *a, **kw: {})
    assert herenow.publish(rid)['herenow']['password_applied'] is True
    assert '--slug' in calls[-1][1]


def test_toggle_off_failure_retry_then_on_keeps_or_rotates_password(published, monkeypatch):
    data, calls = published
    rid = data['id']
    original = herenow.publish(rid)['herenow']['password']
    assert api.password(rid, True)['password'] == original
    def fail(*a, **kw):
        raise OSError('remote body')
    monkeypatch.setattr(herenow, '_api', fail)
    with pytest.raises(herenow.HereNowError):
        api.password(rid, False)
    hn = store.load(rid)['herenow']
    assert hn['password_enabled'] is False and hn['password'] == original
    assert hn['password_applied'] is None
    removals = []
    monkeypatch.setattr(herenow, '_api', lambda *a, **kw: removals.append((a, kw)) or {})
    data = herenow.publish(rid)  # reconciles the failed removal
    assert removals == [(('PATCH', '/publish/test-site-ab12/metadata'), {'body': {'password': None}})]
    assert data['herenow']['password'] is None and data['herenow']['password_applied'] is False
    herenow.publish(rid)  # normal off republish does not remove gates
    assert len(removals) == 1
    assert api.password(rid, True)['password'] != original


def test_legacy_review_stays_public_until_explicit_toggle(published):
    data, calls = published
    data['herenow'] = {'slug': None, 'site_url': None}
    store.save(data['id'], data)
    result = herenow.publish(data['id'])
    assert [c[0] for c in calls] == ['publish']
    assert herenow.password_result(result) == {'password': None, 'password_enabled': False, 'password_status': 'legacy'}
    assert api.password(data['id'], True)['password']


def test_unpublished_toggle_saves_intent_closed_and_local_refused(published, review):
    data, _ = published
    with pytest.raises(herenow.HereNowError, match='publish the review'):
        api.password(data['id'], False)
    assert store.load(data['id'])['herenow']['password_enabled'] is False
    with pytest.raises(store.ReviewError, match='open herenow'):
        api.password(review['id'], True)
    data['status'] = 'closed'
    store.save(data['id'], data)
    with pytest.raises(store.ReviewError, match='open herenow'):
        api.password(data['id'], True)
    with pytest.raises(store.ReviewError, match='open herenow'):
        herenow.publish(data['id'])


def test_http_password_contract_auth_json_and_error_redaction(monkeypatch):
    requests = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, *a): return b'{}'
    def remote(req, **kw):
        requests.append(req)
        return Reply()
    monkeypatch.setattr(herenow.urllib.request, 'urlopen', remote)
    for value in ['synthetic-gate-value', None]:
        herenow._api('PATCH', '/publish/test-site/metadata', body={'password': value})
        req = requests[-1]
        assert req.full_url == config.HERENOW_API + '/publish/test-site/metadata'
        assert req.get_method() == 'PATCH' and json.loads(req.data) == {'password': value}
        assert req.get_header('Content-type') == 'application/json'
        assert req.get_header('Authorization') == 'Bearer test-public-key'
    def fail(req, **kw):
        raise urllib.error.HTTPError(req.full_url, 403, 'synthetic-gate-value', {}, io.BytesIO(b'synthetic-gate-value'))
    monkeypatch.setattr(herenow.urllib.request, 'urlopen', fail)
    with pytest.raises(herenow.HereNowError, match='HTTP 403') as err:
        herenow._api('PATCH', '/publish/test-site/metadata', body={'password': 'synthetic-gate-value'})
    assert 'synthetic-gate-value' not in str(err.value)


@pytest.mark.parametrize('enabled', [True, False])
def test_setting_cli_tool_and_round_results(published, tmp_path, monkeypatch, capsys, enabled):
    _, _calls = published
    monkeypatch.setenv('MOCKREVIEW_HERENOW_PASSWORD', str(not enabled).lower())
    dec = str(_decisions(tmp_path))
    args = ['create', '--title', 'CLI gate', '--platform', 'discord', '--chat-id', 'test-chat',
            '--mode', 'herenow', '--decisions', dec, '--password' if enabled else '--no-password']
    assert cli.main(args) == 0
    res = json.loads(capsys.readouterr().out)
    assert res['password_enabled'] is enabled
    assert bool(res['password']) is enabled
    rid = res['review_id']
    assert cli.main(['publish', rid]) == 0
    assert json.loads(capsys.readouterr().out)['password'] == res['password']
    assert api.add_round(rid, decisions=dec)['password'] == res['password']
    assert cli.main(['password', rid, 'off']) == 0
    assert json.loads(capsys.readouterr().out)['password'] is None
    ctx = FakeCtx({'herenow_password': enabled})
    plugin = load_plugin()
    plugin.register(ctx)
    monkeypatch.delenv('MOCKREVIEW_HERENOW_PASSWORD')
    tool = ctx.tools['mock_review_create']
    assert tool['schema']['parameters']['properties']['password']['type'] == 'boolean'
    result = json.loads(tool['handler']({'title': 'Tool gate', 'mode': 'herenow', 'password': not enabled,
        'origin_platform': 'discord', 'origin_chat_id': 'test-chat', 'decisions': dec}))
    assert result['password_enabled'] is not enabled
    result = json.loads(ctx.commands['mockreview']['handler']('password ' + rid + ' on'))
    assert result['password']
    # A new review with no call override captures the config choice.
    created = api.create('Config gate', {'platform': 'discord', 'chat_id': 'test-chat'}, mode='herenow', decisions=dec)
    assert created['password_enabled'] is enabled
    monkeypatch.setenv('MOCKREVIEW_HERENOW_PASSWORD', str(not enabled).lower())
    assert settings.get('herenow_password') is not enabled
    assert store.load(created['review_id'])['herenow']['password_enabled'] is enabled


def test_concurrent_publish_toggle_and_close_are_serialized(published, monkeypatch):
    data, calls = published
    rid = data['id']
    entered, release, toggling = threading.Event(), threading.Event(), threading.Event()
    real_run = herenow.subprocess.run
    def slow_publish(*a, **kw):
        entered.set()
        assert release.wait(10)
        return real_run(*a, **kw)
    monkeypatch.setattr(herenow.subprocess, 'run', slow_publish)
    errors = []
    def run(fn):
        try: fn()
        except BaseException as e: errors.append(e)
    publisher = threading.Thread(target=run, args=(lambda: herenow.publish(rid),))
    def toggle():
        toggling.set()
        api.password(rid, False)
    toggler = threading.Thread(target=run, args=(toggle,))
    publisher.start()
    assert entered.wait(10)
    toggler.start()
    assert toggling.wait(10)
    release.set()
    publisher.join(15); toggler.join(15)
    assert not publisher.is_alive() and not toggler.is_alive() and not errors
    assert [c[0] for c in calls] == ['publish', 'PATCH', 'PATCH']
    assert calls[-1][2] == {'body': {'password': None}}
    assert store.load(rid)['herenow']['password_applied'] is False
    # Closing still archives/deletes, with the same operation lock.
    monkeypatch.setattr(herenow, 'fetch_records', lambda slug: [])
    assert api.close(rid)['site_deleted']
    assert calls[-1][0] == 'DELETE'
    with pytest.raises(store.ReviewError):
        herenow.publish(rid)


@pytest.mark.parametrize('enabled', [True, False])
def test_interrupted_patch_reconciles_on_publish(published, monkeypatch, enabled):
    data, _ = published
    rid = data['id']
    herenow.publish(rid)
    def interrupt(*a, **kw):
        raise SystemExit('simulated interruption')
    monkeypatch.setattr(herenow, '_api', interrupt)
    with pytest.raises(SystemExit):
        api.password(rid, enabled)
    pending = store.load(rid)
    assert pending['herenow']['password_status'] == 'pending'
    assert pending['herenow']['password_applied'] is None
    assert store.review_url(pending) == ''
    patched = []
    monkeypatch.setattr(herenow, '_api', lambda *a, **kw: patched.append(kw['body']) or {})
    result = herenow.publish(rid)
    assert patched == [{'password': pending['herenow']['password'] if enabled else None}]
    assert result['herenow']['password_status'] == 'applied'
    assert result['herenow']['password_applied'] is enabled


def test_password_patch_failure_can_still_close_and_delete(published, monkeypatch):
    data, calls = published
    rid = data['id']
    def remote(method, path, **kw):
        calls.append((method, path, kw))
        if method == 'PATCH':
            raise herenow.HereNowError('synthetic failure')
        return {}
    monkeypatch.setattr(herenow, '_api', remote)
    with pytest.raises(herenow.HereNowError):
        herenow.publish(rid)
    result = api.close(rid)
    assert result['status'] == 'closed' and result['site_deleted']
    assert calls[-1][:2] == ('DELETE', '/publish/test-site-ab12')
