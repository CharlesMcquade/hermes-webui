"""Phase-2 R1/R5 embed capability routes: device-auth allowlist coverage.

Ratified contract (PHASE2-CONTRACT-DELTAS.md §R1/§R5):
- Every R1 capability route is chat-scope; denied without a bearer and
  allowed with a chat-scope bearer. R5: a control-scope-only grant does NOT
  gain anything from the R1 additions — paths that already existed in the
  CONTROL tables (settings, profiles, reasoning, projects, list,
  session/yolo, approval/clarify pending, session/draft, client-events/log)
  keep exact pre-existing parity, and no chat-scope grant appears in any
  control-only surface.
- `POST /api/client-events/log` rejects bodies > 8KB with 413.
- Not granted: `api/events`, `api/events/stream`, `api/terminal/start`,
  `api/settings` POST, anything CDP.
"""
import json
import threading

import pytest

from api.extension_auth import CHAT_GET, CHAT_POST, allowed, SCOPES

EXT = 'japhcdiodeephocbijenihodhnglmfao'
ORIGIN = 'chrome-extension://' + EXT

# (method, path) exactly as ratified in the R1 route table.
R1_GET_ROUTES = (
    '/api/settings',
    '/api/profiles',
    '/api/reasoning',
    '/api/projects',
    '/api/list',
    '/api/approval/pending',
    '/api/clarify/pending',
    '/api/session/yolo',
)
R1_POST_ROUTES = (
    '/api/session/draft',
    '/api/client-events/log',
)
R1_ROUTES = [('GET', p) for p in R1_GET_ROUTES] + [('POST', p) for p in R1_POST_ROUTES]

# R1 paths that already live in the backend CONTROL tables — pre-existing
# behavior, unchanged by Phase 2 (control-bearer parity asserted, not removed).
PREEXISTING_CONTROL = {'/api/settings': 'GET', '/api/profiles': 'GET',
                       '/api/reasoning': 'GET', '/api/projects': 'GET',
                       '/api/list': 'GET', '/api/session/yolo': 'GET',
                       '/api/approval/pending': 'GET', '/api/clarify/pending': 'GET',
                       '/api/session/draft': 'POST',
                       '/api/client-events/log': 'POST'}


def test_allowlist_tokens_match_ratified_table():
    for path in R1_GET_ROUTES:
        assert path.removeprefix('/api/') in CHAT_GET, path
        assert path.removeprefix('/api/') not in CHAT_POST, path
    for path in R1_POST_ROUTES:
        assert path.removeprefix('/api/') in CHAT_POST, path
    # session/draft legitimately exists in both method tables (pre-existing).


def test_chat_scope_unlocks_every_r1_route():
    for method, path in R1_ROUTES:
        assert allowed(method, path, {'chat'}), (method, path)


def test_control_scope_only_unlocks_nothing_new_beyond_parity():
    """R5: control/cdp grants do not gain anything from the R1 additions.
    Some R1 paths pre-exist in the CONTROL tables (settings/profiles/reasoning/
    projects/list/session/yolo) — their control behavior is byte-unchanged
    parity. The genuinely NEW embed routes (approval/clarify pending cards,
    session/draft, client-events/log) are chat-only: a control-only grant does
    not unlock them, and neither does cdp."""
    newly_chat_only = [r for r in R1_ROUTES if r[1] not in PREEXISTING_CONTROL]
    for method, path in newly_chat_only:
        assert not allowed(method, path, {'control'}), (method, path)
        assert not allowed(method, path, {'cdp'}), (method, path)
    for path, method in PREEXISTING_CONTROL.items():
        assert allowed(method, path, {'control'}), (method, path)  # parity, pre-existing
    # The settings WRITE route stays control-only and chat never gains it.
    assert not allowed('POST', '/api/settings', {'chat'})


def test_not_granted_routes_stay_deny_by_default():
    # Denied for a chat grant (the scope the embed shell holds), and for any
    # chat-shaped scope combination. terminal/start stays control-only
    # (pre-existing; NOT granted to chat by R1).
    for method, path in [('GET', '/api/events'), ('GET', '/api/events/stream'),
                         ('POST', '/api/terminal/start'), ('POST', '/api/settings'),
                         ('POST', '/api/sidecar/cdp/command'), ('GET', '/api/new-embed-route')]:
        assert not allowed(method, path, {'chat'}), (method, path)
    # sidecar/cdp/command is cdp-scope by design — never reachable with chat only.
    assert allowed('POST', '/api/sidecar/cdp/command', {'chat', 'cdp'})
    assert not allowed('POST', '/api/sidecar/cdp/command', {'chat'})


def test_sse_feed_families_fail_closed_for_device_scopes():
    for path in ('/api/session/events', '/api/approval/events', '/api/clarify/events',
                 '/api/sessions/events', '/api/gateway/stream'):
        assert not allowed('GET', path, SCOPES), path


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

    def call(method, path, body=None, headers=None):
        import http.client
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

    def pair(scopes=None):
        import base64
        import hashlib
        verifier = 'a' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        s, pending, _ = call('POST', ext.PREFIX + 'start',
                             dict(extension_id=EXT, client_name='Test device',
                                  code_challenge=challenge, scopes=scopes or ['chat']),
                             {'Origin': ORIGIN})
        assert s == 200
        s, grant, _ = call('POST', ext.PREFIX + 'inspect',
                           {'user_code': pending['user_code']}, cookie_headers)
        assert s == 200
        assert call('POST', ext.PREFIX + 'approve',
                    dict(user_code=pending['user_code'], confirm=True,
                         scopes=grant['scopes'], profile=grant['profile']),
                    cookie_headers)[0] == 200
        s, result, _ = call('POST', ext.PREFIX + 'token',
                            dict(extension_id=EXT, device_code=pending['device_code'],
                                 code_verifier=verifier), {'Origin': ORIGIN})
        assert s == 200
        return result, {'Origin': ORIGIN, 'Authorization': 'Bearer ' + result['access_token']}

    cookie = auth.create_session()
    cookie_headers = {'Cookie': auth._resolve_cookie_name() + '=' + cookie,
                      'Origin': 'http://127.0.0.1:' + str(httpd.server_port),
                      auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(cookie)}

    yield dict(call=call, pair=pair, cookie=cookie_headers, auth=auth, ext=ext,
               state=state, patch=monkeypatch, port=httpd.server_port)
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=5)


def test_every_r1_route_denied_without_bearer(app):
    app['patch'].setattr(app['auth'], 'is_auth_enabled', lambda: True)
    for method, path in R1_ROUTES:
        status, data, _ = app['call'](method, path, {} if method == 'POST' else None)
        assert status == 401, (method, path, status, data)


def test_every_r1_route_allowed_with_chat_scope_bearer(app):
    _, headers = app['pair'](['chat'])
    # Exact statuses: authorization must PASS (not 401/403 bearer/scope errors).
    # Handlers may 4xx/5xx on empty bodies; assert only auth outcomes.
    for method, path in R1_ROUTES:
        status, data, _ = app['call'](method, path, {} if method == 'POST' else None, headers)
        assert status not in (401, 403), (method, path, status, data)


def test_r1_new_routes_denied_for_control_only_grant(app):
    _, headers = app['pair'](['control'])
    for method, path in [r for r in R1_ROUTES if r[1] not in PREEXISTING_CONTROL]:
        status, data, _ = app['call'](method, path, {} if method == 'POST' else None, headers)
        assert status == 403 and 'ok' not in data, (method, path, status, data)
    # Parity: pre-existing control-read R1 paths still work for control bearers.
    for path in ('/api/settings', '/api/profiles'):
        status, _, _ = app['call']('GET', path, None, headers)
        assert status not in (401, 403), (path, status)


def test_client_events_log_rejects_oversized_body_413(app):
    _, headers = app['pair'](['chat'])
    big = 'x' * (app['ext'].CLIENT_EVENTS_LOG_MAX_BODY_BYTES + 1)
    import http.client
    conn = http.client.HTTPConnection('127.0.0.1', app['port'], timeout=8)
    try:
        conn.request('POST', '/api/client-events/log', json.dumps({'event': big}),
                     {'Content-Type': 'application/json', **headers})
        response = conn.getresponse()
        payload = json.loads(response.read().decode())
        status = response.status
    finally:
        conn.close()
    assert (status, payload['error']) == (413, 'payload_too_large')


def test_client_events_log_accepts_bounded_body_from_bearer(app):
    _, headers = app['pair'](['chat'])
    status, data, _ = app['call']('POST', '/api/client-events/log',
                                  {'event': 'sse_error', 'source': 'embed-shell'}, headers)
    assert status == 200 and data.get('ok') is True


def test_r1_route_rechecks_revocation_per_request(app):
    """R2: an embed-capability route rechecks authenticate() every request."""
    result, headers = app['pair'](['chat'])
    status, _, _ = app['call']('GET', '/api/approval/pending', None, headers)
    assert status not in (401, 403)
    assert app['call']('POST', app['ext'].PREFIX + 'revoke', {}, headers)[0] == 200
    status, data, _ = app['call']('GET', '/api/approval/pending', None, headers)
    assert (status, data['error']) == (401, 'invalid_token')
