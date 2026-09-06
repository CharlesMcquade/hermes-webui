"""Real HTTP Handler integration tests; no production process or auth state."""
import base64
import hashlib
import http.client
import json
import os

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

EXT = 'japhcdiodeephocbijenihodhnglmfao'
ORIGIN = 'chrome-extension://' + EXT
VERIFIER = 'a' * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR', str(tmp_path / 'state'))
    from api import auth, config, extension_auth as ext, profiles
    import server
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(config, 'STATE_DIR', state)
    monkeypatch.setattr(auth, 'STATE_DIR', state)
    monkeypatch.setattr(auth, '_SESSIONS_FILE', state / '.sessions.json')
    monkeypatch.setattr(auth, '_sessions', {})
    monkeypatch.setattr(auth, 'is_auth_enabled', lambda: True)
    monkeypatch.setattr(profiles, '_active_profile', 'default')
    monkeypatch.setattr(profiles, '_is_isolated_profile_mode', lambda: False)
    httpd = server.QuietHTTPServer(('127.0.0.1', 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    cookie = auth.create_session()
    cookie_headers = {'Cookie': auth._resolve_cookie_name() + '=' + cookie,
                      'Origin': 'http://127.0.0.1:' + str(httpd.server_port),
                      auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(cookie)}

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=8)
        try:
            payload = json.dumps(body) if body is not None else None
            conn.request(method, path, payload, {'Content-Type': 'application/json', **(headers or {})})
            response = conn.getresponse()
            data = response.read().decode()
            try:
                data = json.loads(data)
            except ValueError:
                pass
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    def start(scopes=None):
        return call('POST', ext.PREFIX + 'start', dict(extension_id=EXT, client_name='Test device',
                    code_challenge=CHALLENGE, scopes=scopes or ['chat']), {'Origin': ORIGIN})

    def token(pair, verifier=VERIFIER):
        return call('POST', ext.PREFIX + 'token', dict(extension_id=EXT, device_code=pair['device_code'],
                    code_verifier=verifier), {'Origin': ORIGIN})

    def approve(pair):
        s, grant, _ = call('POST', ext.PREFIX + 'inspect', {'user_code': pair['user_code']}, cookie_headers)
        assert s == 200
        return call('POST', ext.PREFIX + 'approve', dict(user_code=pair['user_code'], confirm=True,
                    scopes=grant['scopes'], profile=grant['profile']), cookie_headers)

    def pair(scopes=None):
        s, pending, _ = start(scopes)
        assert s == 200
        assert approve(pending)[0] == 200
        s, result, _ = token(pending)
        assert s == 200
        return result, {'Origin': ORIGIN, 'Authorization': 'Bearer ' + result['access_token']}

    yield dict(call=call, start=start, token=token, approve=approve, pair=pair,
               cookie=cookie_headers, auth=auth, ext=ext, state=state, patch=monkeypatch,
               port=httpd.server_port)
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=5)


def test_pair_independent_and_revoke(app):
    result, headers = app['pair']()
    app['auth']._sessions.clear()  # logout/expiry cannot invalidate independent device
    status, profile, cors = app['call']('GET', '/api/profile/active', headers=headers)
    assert status == 200
    assert cors['Access-Control-Allow-Origin'] == ORIGIN
    assert 'Access-Control-Allow-Credentials' not in cors
    saved = (app['state'] / 'extension-auth.json').read_text()
    assert result['access_token'] not in saved
    if os.name != 'nt':
        assert (app['state'] / 'extension-auth.json').stat().st_mode & 0o777 == 0o600
    assert app['call']('POST', '/api/auth/extension/revoke', {}, headers)[0] == 200
    assert app['call']('GET', '/api/profile/active', headers=headers)[0] == 401


def test_originless_device_get_uses_bound_client_id(app):
    result, original = app['pair']()
    headers = {'Authorization': original['Authorization'], 'X-Hermes-Extension-Id': EXT}
    status, me, cors = app['call']('GET', '/api/auth/extension/me', headers=headers)
    assert status == 200
    assert me['device_id'] == result['device_id']
    assert me['profiles'] == ['default']
    assert 'Access-Control-Allow-Origin' not in cors
    assert app['call']('GET', '/api/profile/active', headers=headers)[0] == 200
    assert app['call']('GET', '/api/profile/active', headers={**headers, 'X-Hermes-Profile': 'other'})[0] == 403
    assert app['call']('GET', '/api/sessions?all_profiles=1', headers=headers)[0] == 403
    assert app['call']('POST', '/api/auth/extension/revoke', {}, headers)[0] == 200
    assert app['call']('GET', '/api/auth/extension/me', headers=headers)[0] == 401


@pytest.mark.parametrize('enabled', [True, False])
def test_client_id_requires_bearer_without_cookie_fallback(app, enabled):
    _, original = app['pair']()
    app['patch'].setattr(app['auth'], 'is_auth_enabled', lambda: enabled)
    for client_id in [EXT, '', 'b' * 32]:
        for cookie in [{}, {'Cookie': app['cookie']['Cookie']}]:
            headers = {'X-Hermes-Extension-Id': client_id, **cookie}
            status, data, _ = app['call']('GET', '/api/profile/active', headers=headers)
            assert (status, data['error']) == (401, 'bearer_required')
    for authorization in [original['Authorization'], 'Bearer invalid', '', 'Basic invalid']:
        headers = {'X-Hermes-Extension-Id': EXT, 'Authorization': authorization,
                   'Cookie': app['cookie']['Cookie']}
        assert app['call']('GET', '/api/profile/active', headers=headers)[0] == 401
    # An ordinary signed-in WebUI request remains unaffected.
    assert app['call']('GET', '/api/profile/active', headers=app['cookie'])[0] == 200


def test_originless_client_id_exact_validation_and_origin_precedence(app):
    _, original = app['pair']()
    bearer = {'Authorization': original['Authorization']}
    for client_id in [None, '', 'b' * 32, EXT.upper(), EXT[:-1], EXT + 'a',
                      'q' * 32, EXT + ' ', ORIGIN, EXT + ',' + EXT]:
        headers = bearer if client_id is None else {**bearer, 'X-Hermes-Extension-Id': client_id}
        status, data, _ = app['call']('GET', '/api/auth/extension/me', headers=headers)
        assert (status, data['error']) == (401, 'invalid_token')
    for origin in ['', 'null', 'https://evil.test', ORIGIN + '/',
                   'chrome-extension://' + 'b' * 32, ORIGIN + ', https://evil.test']:
        headers = {**bearer, 'Origin': origin, 'X-Hermes-Extension-Id': EXT}
        assert app['call']('GET', '/api/auth/extension/me', headers=headers)[0] == 401
    # With a valid Origin the bound Origin is authoritative, not the optional ID.
    assert app['call']('GET', '/api/auth/extension/me', headers={
        **original, 'X-Hermes-Extension-Id': 'b' * 32})[0] == 200


def test_public_pairing_still_requires_actual_origin_and_cors_is_scoped(app):
    for endpoint in ['start', 'token']:
        for origin in [None, '', 'https://evil.test']:
            headers = {'X-Hermes-Extension-Id': EXT}
            if origin is not None:
                headers['Origin'] = origin
            status, data, _ = app['call']('POST', '/api/auth/extension/' + endpoint, {}, headers)
            assert (status, data['error']) == (403, 'invalid_request')
    for origin in [ORIGIN, 'https://evil.test', '']:
        status, _, response = app['call']('OPTIONS', '/api/auth/extension/me', headers={
            'Origin': origin, 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'authorization,x-hermes-extension-id'})
        assert status == 200
        if origin == ORIGIN:
            assert 'X-Hermes-Extension-Id' in response['Access-Control-Allow-Headers']
            assert response['Access-Control-Allow-Origin'] == ORIGIN
            assert 'Access-Control-Allow-Credentials' not in response
        else:
            assert 'Access-Control-Allow-Origin' not in response


def test_pkce_pending_replay_and_concurrent_consume(app):
    _, pending, _ = app['start']()
    assert app['token'](pending, 'b' * 64)[1]['error'] == 'invalid_grant'
    assert app['token'](pending)[1]['error'] == 'authorization_pending'
    assert app['token'](pending)[1]['error'] == 'slow_down'
    assert app['approve'](pending)[0] == 200
    with app['ext'].store() as data:
        data['pending'][app['ext'].digest(pending['device_code'])]['last_poll'] = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: app['token'](pending), range(2)))
    assert sorted(r[0] for r in results) == [200, 400]
    assert app['token'](pending)[1]['error'] == 'invalid_grant'


@pytest.mark.parametrize('enabled', [True, False])
def test_invalid_bearer_never_cookie_or_disabled_fallback(app, enabled):
    app['patch'].setattr(app['auth'], 'is_auth_enabled', lambda: enabled)
    for headers in [dict(Origin=ORIGIN, Authorization='Bearer invalid'),
                    {**app['cookie'], 'Authorization': 'Bearer invalid'},
                    {'Origin': ORIGIN, **{'Cookie': app['cookie']['Cookie']}}]:
        assert app['call']('GET', '/api/profile/active', headers=headers)[0] == 401


def test_origin_scope_profile_default_deny(app):
    _, headers = app['pair']()
    for method, path in [('POST', '/api/settings'), ('GET', '/api/logs'),
                         ('GET', '/api/auth/extension/devices'), ('GET', '/api/new-future-route'),
                         ('PUT', '/api/session'), ('GET', '/static/extension-pair.js'),
                         ('POST', '/api/sidecar/cdp/register')]:
        assert app['call'](method, path, {} if method != 'GET' else None, headers)[0] == 403
    assert app['call']('GET', '/api/profile/active', headers={**headers, 'X-Hermes-Profile': 'other'})[0] == 403
    assert app['call']('GET', '/api/sessions?all_profiles=1', headers=headers)[0] == 403
    for origin in ['https://evil.test', ORIGIN + '/', 'chrome-extension://' + 'b' * 32]:
        assert app['call']('GET', '/api/profile/active', headers={**headers, 'Origin': origin})[0] == 401


def test_cookie_csrf_and_approval_regressions(app):
    _, pending, _ = app['start']()
    body = {'user_code': pending['user_code'], 'confirm': True, 'profile': 'default', 'scopes': ['chat']}
    for headers in [{**app['cookie'], 'Origin': 'https://evil.test'},
                    {'Cookie': app['cookie']['Cookie'], 'Origin': app['cookie']['Origin']}]:
        assert app['call']('POST', '/api/auth/extension/approve', body, headers)[0] == 403
        assert app['call']('POST', '/api/session/new', {}, headers)[0] == 403
    assert app['call']('POST', '/api/auth/extension/approve', {'user_code': pending['user_code']}, app['cookie'])[0] == 400
    assert app['approve'](pending)[0] == 200
    assert app['approve'](pending)[0] == 400
    assert app['call']('GET', '/extension-pair')[0] == 302
    status, html, _ = app['call']('GET', '/extension-pair', headers=app['cookie'])
    assert status == 200 and '__CSRF__' not in html


def test_expiry_limits_preflight_and_no_cookies(app):
    app['patch'].setattr(app['ext'], 'MAX_PER_IP', 1)
    _, pending, _ = app['start']()
    assert app['start']()[0] == 429
    with app['ext'].store() as data:
        data['pending'][app['ext'].digest(pending['device_code'])]['expires_at'] = 0
    assert app['token'](pending)[1]['error'] == 'invalid_grant'
    status, _, headers = app['call']('OPTIONS', '/api/auth/extension/start', headers={
        'Origin': ORIGIN, 'Access-Control-Request-Method': 'POST'})
    assert status == 200 and headers['Access-Control-Allow-Origin'] == ORIGIN
    _, _, headers = app['call']('OPTIONS', '/api/settings', headers={
        'Origin': 'https://evil.test', 'Access-Control-Request-Method': 'POST'})
    assert 'Access-Control-Allow-Origin' not in headers
    status, _, _ = app['call']('POST', '/api/auth/extension/start', {}, {**app['cookie'], 'Origin': ORIGIN})
    assert status == 403


def test_cdp_cross_device_ownership(app):
    _, a = app['pair'](['chat', 'control', 'cdp'])
    _, b = app['pair'](['chat', 'cdp'])
    s, rega, _ = app['call']('POST', '/api/sidecar/cdp/register', {'extension_id': EXT}, a)
    assert s == 200
    s, regb, _ = app['call']('POST', '/api/sidecar/cdp/register', {'extension_id': EXT}, b)
    assert s == 200 and rega['relay_id'] != regb['relay_id']
    for action in ['poll', 'respond', 'unregister', 'command']:
        assert app['call']('POST', '/api/sidecar/cdp/' + action,
                           {'relay_id': rega['relay_id'], 'method': 'Runtime.evaluate'}, b)[0] == 403
    s, listed, _ = app['call']('GET', '/api/sidecar/cdp/relays', headers=b)
    assert s == 200 and [r['relay_id'] for r in listed['relays']] == [regb['relay_id']]
    assert app['call']('POST', '/api/sidecar/cdp/unregister', {'relay_id': rega['relay_id']}, a)[0] == 200
    assert app['call']('POST', '/api/sidecar/cdp/unregister', {'relay_id': regb['relay_id']}, b)[0] == 200


def test_body_profile_and_keepalive_principal_reset(app):
    _, headers = app['pair']()
    assert app['call']('POST', '/api/session/new', {'profile': 'other'}, headers)[0] == 403
    conn = http.client.HTTPConnection('127.0.0.1', app['port'], timeout=5)
    try:
        conn.request('GET', '/api/profile/active', headers=headers)
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.request('GET', '/api/profile/active', headers={'Origin': ORIGIN})
        response = conn.getresponse()
        assert response.status == 401
        response.read()
    finally:
        conn.close()


def test_bound_cookie_grant_expiry_and_deny(app):
    auth = app['auth']
    app['patch'].setenv(auth._TRUSTED_AUTH_HEADER_ENV, 'X-Test-User')
    app['patch'].setenv(auth._TRUSTED_GROUPS_HEADER_ENV, 'X-Test-Groups')
    app['patch'].setenv(auth._TRUSTED_GROUP_PROFILE_MAP_ENV, '{"team":"restricted"}')
    app['cookie']['X-Test-User'] = 'tester'
    app['cookie']['X-Test-Groups'] = 'team'
    value = auth.create_session(auth_type='trusted', username='tester', bound_profile='restricted')
    app['cookie']['Cookie'] = auth._resolve_cookie_name() + '=' + value
    app['cookie'][auth.CSRF_HEADER_NAME] = auth.csrf_token_for_session(value)
    result, headers = app['pair']()
    status, profile, _ = app['call']('GET', '/api/profile/active', headers=headers)
    assert status == 200 and profile['name'] == 'restricted'
    assert app['call']('GET', '/api/profile/active', headers={**headers, 'X-Hermes-Profile': 'default'})[0] == 403
    with app['ext'].store() as data:
        data['devices'][app['ext'].digest(result['access_token'])]['expires_at'] = 0
    assert app['call']('GET', '/api/profile/active', headers=headers)[0] == 401
    _, pending, _ = app['start']()
    assert app['call']('POST', '/api/auth/extension/deny', {'user_code': pending['user_code']}, app['cookie'])[0] == 200
    assert app['token'](pending)[1]['error'] == 'invalid_grant'


def test_frontend_contract_me_alias_and_token_without_id(app):
    _, pending, _ = app['start'](['chat', 'controls', 'cdp'])
    assert app['approve'](pending)[0] == 200
    s, token, _ = app['call']('POST', '/api/auth/extension/token',
        {'device_code': pending['device_code'], 'code_verifier': VERIFIER}, {'Origin': ORIGIN})
    assert s == 200 and token['scopes'] == ['cdp', 'chat', 'control']
    headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + token['access_token']}
    s, me, _ = app['call']('GET', '/api/auth/extension/me', headers=headers)
    assert s == 200 and me['profiles'] == ['default'] and 'access_token' not in me
    assert app['call']('POST', '/api/auth/extension/revoke', {}, headers)[0] == 200
    assert app['call']('GET', '/api/auth/extension/me', headers=headers)[0] == 401
    s, devices, _ = app['call']('GET', '/api/auth/extension/devices', headers=app['cookie'])
    assert s == 200 and devices['devices'] == []


def test_durability_failure_never_announces_grant(app):
    _, pending, _ = app['start']()
    assert app['approve'](pending)[0] == 200
    def fail_replace(*args):
        raise OSError('simulated disk failure')
    app['patch'].setattr(app['ext'].os, 'replace', fail_replace)
    status, payload, _ = app['token'](pending)
    assert status == 500 and 'access_token' not in payload


@pytest.mark.parametrize('ending', ['revoke', 'expire'])
def test_http_sse_stops_and_unsubscribes(app, ending):
    import queue
    from api import routes
    result, headers = app['pair']()
    events = queue.Queue()
    cleaned = threading.Event()
    class Stream:
        def subscribe(self):
            return events
        def unsubscribe(self, subscriber):
            assert subscriber is events
            cleaned.set()
    app['patch'].setitem(routes.STREAMS, 'auth-test', Stream())
    app['patch'].setattr(routes, '_stream_id_visible_to_request_profile', lambda *args: True)
    app['patch'].setattr(routes, '_sse_replay_run_journal_gap_checked', lambda *args, **kw: (False, None))
    app['patch'].setattr(routes, '_SSE_HEARTBEAT_INTERVAL_SECONDS', 0.05)
    conn = http.client.HTTPConnection('127.0.0.1', app['port'], timeout=5)
    try:
        conn.request('GET', '/api/chat/stream?stream_id=auth-test', headers=headers)
        response = conn.getresponse()
        assert response.status == 200
        assert response.readline() == b': heartbeat\n'
        assert response.readline() == b'\n'
        if ending == 'revoke':
            assert app['call']('POST', app['ext'].PREFIX + 'revoke', {}, headers)[0] == 200
        else:
            with app['ext'].store() as state:
                state['devices'][app['ext'].digest(result['access_token'])]['expires_at'] = 0
        assert cleaned.wait(3), 'Revoked stream remained subscribed'
        assert b'secret' not in response.read()
    finally:
        conn.close()


@pytest.mark.parametrize('ending', ['revoke', 'expire'])
def test_stream_rechecks_grant(app, ending):
    import io
    from types import SimpleNamespace
    result, headers = app['pair']()
    ext = app['ext']
    with ext.store(write=False) as state:
        principal = dict(state['devices'][ext.digest(result['access_token'])])
    raw = io.BytesIO()
    handler = SimpleNamespace(headers=headers, _extension_principal=principal,
                              command='GET', path='/api/chat/stream', close_connection=False)
    writer = ext.AuthorizedStreamWriter(raw, handler)
    writer.write(b': heartbeat\n\n')
    if ending == 'revoke':
        assert app['call']('POST', ext.PREFIX + 'revoke', {}, headers)[0] == 200
    else:
        with ext.store() as state:
            state['devices'][ext.digest(result['access_token'])]['expires_at'] = 0
    with pytest.raises(BrokenPipeError):
        writer.write(b'event: secret\n\n')
    assert raw.getvalue() == b': heartbeat\n\n'
    assert handler.close_connection


def test_bearer_media_is_session_scoped(app, tmp_path):
    from types import SimpleNamespace
    from urllib.parse import urlencode
    from api import models, upload
    _, headers = app['pair']()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    app['patch'].setattr(upload, 'STATE_DIR', app['state'])
    own = SimpleNamespace(profile='default', workspace=str(workspace))
    other = SimpleNamespace(profile='other', workspace=str(workspace))
    sessions = {'own': own, 'other': other}
    app['patch'].setattr(models, 'get_session', lambda sid, **kw: sessions[sid])
    files = {}
    for sid in sessions:
        root = upload._session_attachment_dir(sid)
        root.mkdir(parents=True)
        files[sid] = root / 'picture.png'
        files[sid].write_text(sid)
    outside = tmp_path / 'unrelated.png'
    outside.write_text('private')
    app['patch'].setenv('MEDIA_ALLOWED_ROOTS', str(tmp_path))
    picture = workspace / 'image.png'
    picture.write_text('workspace')
    secret = app['state'] / 'config.yaml'
    secret.write_text('private')
    def read(path, sid=None, auth=headers):
        query = {'path': str(path)}
        if sid:
            query['session_id'] = sid
        return app['call']('GET', '/api/media?' + urlencode(query), headers=auth)
    assert read(files['own'])[0:2] == (200, 'own')
    assert read(files['other'])[0] == 403
    assert read(files['other'], 'own')[0] == 403
    assert read(outside, 'own')[0] == 403
    assert read(secret, 'own')[0] == 403
    assert read(picture)[0] == 403
    assert read(picture, 'own')[0:2] == (200, 'workspace')
    assert read(picture, 'missing')[0] == 403
    if os.name != 'nt':  # Windows symlinks may require elevated privileges.
        symlink = workspace / 'escape.png'
        symlink.symlink_to(outside)
        assert read(symlink, 'own')[0] == 403
    # Existing ordinary cookie media semantics are not tightened by this change.
    assert read(outside, auth=app['cookie'])[0:2] == (200, 'private')


def test_windows_lock_and_permission_fallback(app, tmp_path):
    from types import SimpleNamespace
    ext = app['ext']
    calls = []
    app['patch'].setattr(ext, 'fcntl', None)
    app['patch'].setattr(ext, 'msvcrt', SimpleNamespace(LK_LOCK=1,
        locking=lambda *args: calls.append(args)), raising=False)
    app['patch'].delattr(ext.os, 'fchmod', raising=False)
    with ext.store() as state:
        state['windows_test'] = True
    assert calls and calls[0][1:] == (1, 1)
    assert json.loads((app['state'] / 'extension-auth.json').read_text())['windows_test']


@pytest.mark.parametrize('foreign_cli', [False, True])
def test_session_detail_hides_other_profile_metadata(app, foreign_cli):
    from types import SimpleNamespace
    from api import routes
    _, headers = app['pair']()
    def lookup(sid, **kwargs):
        if foreign_cli:
            raise KeyError(sid)
        return SimpleNamespace(profile='private-profile', messages=[{'content': 'private-content'}])
    app['patch'].setattr(routes, 'get_session', lookup)
    app['patch'].setattr(routes, '_lookup_cli_session_metadata', lambda sid: {'profile': 'private-profile'})
    status, payload, _ = app['call']('GET', '/api/session?session_id=foreign&messages=1', headers=headers)
    assert status == 404
    assert 'private-profile' not in json.dumps(payload) and 'private-content' not in json.dumps(payload)
    # Cookie deep links still offer profile switching rather than pretending deletion.
    status, payload, _ = app['call']('GET', '/api/session?session_id=foreign&messages=1', headers=app['cookie'])
    assert status == 409 and payload['profile'] == 'private-profile'


def test_identity_and_global_events_scope(app):
    from api.extension_auth import allowed, SCOPES
    assert not allowed('GET', '/api/sessions/events', SCOPES)
    assert allowed('POST', '/api/background', {'chat'})
    assert not allowed('POST', '/api/sidecar/cdp/command', {'chat', 'control'})
    _, headers = app['pair']()
    assert app['call']('GET', '/api/sidecar/identity', headers=headers)[0] in (200, 404)


def test_independent_new_session_and_readback(app, tmp_path):
    _, headers = app['pair'](['chat', 'control'])
    app['auth']._sessions.clear()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    assert app['call']('POST', '/api/workspaces/add', {'path': str(workspace)}, headers)[0] == 200
    s, created, _ = app['call']('POST', '/api/session/new', {'workspace': str(workspace)}, headers)
    assert s == 200, created
    sid = created['session']['session_id']
    s, fetched, _ = app['call']('GET', '/api/session?session_id=' + sid, headers=headers)
    assert s == 200 and fetched['session']['session_id'] == sid
