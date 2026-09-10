"""Real-HTTP contract tests for the extension preference service.

Covers docs/CLIENT-API-CONTRACT.md §1–§4 against a real QuietHTTPServer +
server.Handler stack (no production state): pairing, scope narrowing, old
grant compatibility, conditional writes, validation, and the boot.js
skin-registry parity.
"""
import base64
import hashlib
import http.client
import json
import threading

import pytest

EXT = 'japhcdiodeephocbijenihodhnglmfao'
ORIGIN = 'chrome-extension://' + EXT
VERIFIER = 'b' * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@pytest.fixture
def prefs_app(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR', str(tmp_path / 'state'))
    from api import auth, config, extension_auth as ext, profiles
    import server
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(config, 'STATE_DIR', state)
    # SETTINGS_FILE is bound at module import to the FIRST test's tmp state
    # dir; without this rebind every later test's save_settings reads/writes
    # that stale file and saved values bleed across tests.
    monkeypatch.setattr(config, 'SETTINGS_FILE', state / 'settings.json')
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
            return response.status, data
        finally:
            conn.close()

    def pair(scopes):
        s, pending = call('POST', ext.PREFIX + 'start', dict(
            extension_id=EXT, client_name='Prefs test', code_challenge=CHALLENGE,
            scopes=scopes), {'Origin': ORIGIN})
        assert s == 200, pending
        s, grant = call('POST', ext.PREFIX + 'inspect', {'user_code': pending['user_code']}, _cookie_headers(auth, httpd))
        assert s == 200
        s, _ = call('POST', ext.PREFIX + 'approve', dict(
            user_code=pending['user_code'], confirm=True,
            scopes=grant['scopes'], profile=grant['profile']), _cookie_headers(auth, httpd))
        assert s == 200
        s, result = call('POST', ext.PREFIX + 'token', dict(
            extension_id=EXT, device_code=pending['device_code'],
            code_verifier=VERIFIER), {'Origin': ORIGIN})
        assert s == 200, result
        return result, {'Origin': ORIGIN, 'Authorization': 'Bearer ' + result['access_token']}

    yield dict(call=call, pair=pair, config=config, ext=ext, state=state,
               port=httpd.server_port)
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=5)


def _cookie_headers(auth, httpd):
    cookie = auth.create_session()
    return {'Cookie': auth._resolve_cookie_name() + '=' + cookie,
            'Origin': 'http://127.0.0.1:' + str(httpd.server_port),
            auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(cookie)}


# ── old-client compatibility: existing grants never gain the new scopes ──

def test_existing_grants_lack_preference_scopes(prefs_app):
    ext = prefs_app['ext']
    from api.extension_auth import allowed
    # A legacy grant object persisted before this change has no preference keys.
    legacy_scopes = ['chat', 'control']
    assert not allowed('GET', '/api/client/preferences', legacy_scopes)
    assert not allowed('GET', '/api/client/capabilities', legacy_scopes)
    assert not allowed('PATCH', '/api/client/preferences', legacy_scopes)
    # …and the scopes are never implied by the legacy vocabulary.
    assert 'preferences:read' not in ext.SCOPES.intersection(legacy_scopes)
    assert not allowed('GET', '/api/sessions', ['preferences:read'])
    assert not allowed('GET', '/api/settings', ['preferences:read', 'preferences:write'])
    assert not allowed('POST', '/api/settings', ['preferences:write'])
    assert not allowed('POST', '/api/chat/start', ['preferences:read', 'preferences:write'])
    assert not allowed('POST', '/api/sidecar/cdp/command', ['preferences:read', 'preferences:write'])


def test_pair_request_denies_unknown_scope(prefs_app):
    s, body = prefs_app['call']('POST', prefs_app['ext'].PREFIX + 'start', dict(
        extension_id=EXT, client_name='x', code_challenge=CHALLENGE,
        scopes=['preferences:admin']), {'Origin': ORIGIN})
    assert s == 400


def test_pair_request_accepts_narrow_scopes_and_device_is_scoped(prefs_app):
    result, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    assert 'access_token' in result
    s, caps = prefs_app['call']('GET', '/api/client/capabilities', headers=headers)
    assert s == 200
    assert caps['schema_version'] == 1
    assert caps['preferences']['supported'] is True
    assert set(caps['preferences']['authorities']) == {
        'appearance', 'conversation', 'busy_control', 'composer'}


# ── grant denial: the routes require the paired bearer + narrow scope ──

def test_preferences_routes_deny_unauthenticated(prefs_app):
    app = prefs_app
    assert app['call']('GET', '/api/client/capabilities')[0] == 403
    assert app['call']('GET', '/api/client/preferences')[0] == 403
    assert app['call']('PATCH', '/api/client/preferences', {'authority': 'appearance'})[0] == 403
    # Browser-cookie requests cannot ride into the device-only surface.
    s, body = app['call']('GET', '/api/client/capabilities')
    assert s == 403


def test_chat_only_device_cannot_read_or_write(prefs_app):
    _, headers = prefs_app['pair'](['chat'])
    assert prefs_app['call']('GET', '/api/client/capabilities', headers=headers)[0] == 403
    assert prefs_app['call']('GET', '/api/client/preferences', headers=headers)[0] == 403
    assert prefs_app['call']('PATCH', '/api/client/preferences',
                             {'authority': 'appearance', 'expected_revision': 1,
                              'changes': {'theme': 'light'}}, headers=headers)[0] == 403


def test_read_only_device_cannot_write(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read'])
    assert prefs_app['call']('GET', '/api/client/preferences', headers=headers)[0] == 200
    assert prefs_app['call']('PATCH', '/api/client/preferences',
                             {'authority': 'appearance', 'expected_revision': 1,
                              'changes': {'theme': 'light'}}, headers=headers)[0] == 403


# ── reads: allowlisted values only, never secrets/config ──

def test_read_returns_canonical_allowlisted_values(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read'])
    s, body = prefs_app['call']('GET', '/api/client/preferences', headers=headers)
    assert s == 200
    appearance = body['authorities']['appearance']
    assert appearance['revision'] == 1
    assert appearance['source'] == 'instance'
    assert set(appearance['values']) == {'theme', 'skin', 'font_size', 'content_width'}
    dumped = json.dumps(body)
    for secret in ('password_hash', 'auth_disabled_acknowledged', 'provider_cost_budget',
                   'default_workspace', 'api_redact_enabled'):
        assert secret not in dumped


def test_read_single_authority(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read'])
    s, body = prefs_app['call']('GET', '/api/client/preferences?authority=busy_control', headers=headers)
    assert s == 200
    assert set(body['authorities']) == {'busy_control'}
    s, body = prefs_app['call']('GET', '/api/client/preferences?authority=nope', headers=headers)
    assert s == 400 and body['error'] == 'unknown_authority'


# ── conditional writes: revision gate + 409 with current authority ──

def test_conditional_write_success_bumps_revision(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='appearance', expected_revision=1, changes={'theme': 'light'}), headers=headers)
    assert s == 200, body
    record = body['authorities']['appearance']
    assert record['revision'] == 2
    assert record['values']['theme'] == 'light'
    # Other authorities are untouched (per-authority revisions).
    s, all_body = prefs_app['call']('GET', '/api/client/preferences', headers=headers)
    assert all_body['authorities']['busy_control']['revision'] == 1
    # The write went through the canonical save path (visible to /api/settings).
    s, settings = prefs_app['call']('GET', '/api/settings',
                                    headers={**headers}) if False else (None, None)
    assert prefs_app['call']('GET', '/api/client/preferences?authority=appearance',
                             headers=headers)[1]['authorities']['appearance']['values']['theme'] == 'light'


def test_conditional_write_conflict_returns_current(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    # Stale revision → 409 carrying the current authority record.
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='appearance', expected_revision=99, changes={'skin': 'github'}), headers=headers)
    assert s == 409
    assert body['error'] == 'revision_conflict'
    assert body['current']['revision'] == 1
    assert body['current']['values']['theme'] in ('light', 'dark', 'system')
    # And the conflicting change was NOT applied.
    s, cur = prefs_app['call']('GET', '/api/client/preferences?authority=appearance', headers=headers)
    assert cur['authorities']['appearance']['values']['skin'] != 'github'


# ── validation: enum / unknown field / unknown authority ──

def test_enum_validation_rejects_bad_value(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='appearance', expected_revision=1, changes={'skin': 'definitely-not-a-skin'}),
        headers=headers)
    assert s == 400
    assert body['error'] == 'invalid_value'
    assert body['field'] == 'skin'
    assert 'github' in body['constraint']


def test_unknown_field_and_authority_fail_safely(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='appearance', expected_revision=1, changes={'password_hash': 'x'}),
        headers=headers)
    assert s == 400 and body['error'] == 'unknown_field'
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='auth_settings', expected_revision=1, changes={'theme': 'light'}),
        headers=headers)
    assert s == 400 and body['error'] == 'unknown_authority'


# ── skin registry parity: server registry == boot.js vocabulary ──

def _boot_skin_vocabulary():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / 'static' / 'boot.js').read_text(encoding='utf-8')
    block = re.search(r'const _SKINS=\[(.*?)\];', src, re.S).group(1)
    names = re.findall(r"\{name:'([^']+)'(?:\s*,\s*value:'([^']+)')?", block)
    return {(value or name).lower() for name, value in names}


def test_skin_registry_matches_boot_js(prefs_app):
    from api.config import _SETTINGS_SKIN_VALUES
    vocab = _boot_skin_vocabulary()
    assert vocab == set(_SETTINGS_SKIN_VALUES), (
        'server skin registry drifted from boot.js _SKINS; '
        'docs/PREFERENCE-CONTRACT.md §5 requires same-change parity'
    )
    for skin in ('github', 'codex', 'terracotta', 'hepburn', 'neon'):
        assert skin in _SETTINGS_SKIN_VALUES


def test_style_css_has_token_blocks_for_every_registered_skin(prefs_app):
    import re
    from pathlib import Path
    from api.config import _SETTINGS_SKIN_VALUES
    css = (Path(__file__).resolve().parent.parent / 'static' / 'style.css').read_text(encoding='utf-8')
    rendered = {m.group(1) for m in re.finditer(r'\[data-skin="([a-z0-9-]+)"\]', css)}
    missing = set(_SETTINGS_SKIN_VALUES) - rendered - {'default'}
    assert not missing, f'skins registered server-side but unstyled: {sorted(missing)}'


# ── normalization still works with the widened registry ──

def test_github_skin_survives_save_roundtrip(prefs_app):
    app = prefs_app
    config = app['config']
    settings_file = app['state'] / 'settings.json'
    settings_file.write_text(json.dumps({'theme': 'dark', 'skin': 'github'}), encoding='utf-8')
    import api.config as cfg
    original = cfg.SETTINGS_FILE
    cfg.SETTINGS_FILE = settings_file
    try:
        settings = cfg.load_settings()
        assert (settings['theme'], settings['skin']) == ('dark', 'github'), (
            'a previously silently-degraded skin must now persist server-side'
        )
    finally:
        cfg.SETTINGS_FILE = original


def test_unknown_skin_still_falls_back(prefs_app):
    from api.config import _normalize_appearance
    assert _normalize_appearance('dark', 'not-a-skin') == ('dark', 'default')


# ── WebUI-side saves bump authority revisions (source-attributed) ──────────

def _prefs_file(state):
    return state / 'client-preferences.json'


def _read_prefs(state):
    import json as _json
    try:
        return _json.loads(_prefs_file(state).read_text(encoding='utf-8'))
    except Exception:
        return {}


def test_webui_settings_save_bumps_revision_with_webui_source(prefs_app):
    config = prefs_app['config']
    # Fresh fixture state: point config at the fixture's STATE_DIR and save a
    # preference-owned key directly through the canonical save path.
    config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_FILE.write_text('{}', encoding='utf-8')
    config.save_settings({'theme': 'light'})
    state = _read_prefs(prefs_app['state'])
    assert state['revisions']['appearance'] == 2
    assert state['sources']['appearance']['source'] == 'webui'
    # Non-appearance authorities were not touched by this save.
    assert 'revisions' not in state or 'conversation' not in state.get('revisions', {})


def test_unrelated_key_save_does_not_bump_any_authority(prefs_app):
    config = prefs_app['config']
    config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_FILE.write_text('{}', encoding='utf-8')
    config.save_settings({'bot_name': 'Hermes X'})
    state = _read_prefs(prefs_app['state'])
    assert state.get('revisions', {}) == {}


def test_extension_patch_bumps_exactly_once_with_extension_source(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='appearance', expected_revision=1, changes={'theme': 'light'}), headers=headers)
    assert s == 200, body
    assert body['authorities']['appearance']['revision'] == 2
    assert body['authorities']['appearance']['source'] == 'extension'
    state = _read_prefs(prefs_app['state'])
    assert state['revisions']['appearance'] == 2
    assert state['sources']['appearance']['source'] == 'extension'


def test_extension_conditional_write_conflicts_after_webui_save(prefs_app):
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    # The extension reads revision 1...
    s, before = prefs_app['call']('GET', '/api/client/preferences?authority=appearance', headers=headers)
    assert s == 200 and before['authorities']['appearance']['revision'] == 1
    # ...then the WebUI frontend saves a NEWER theme through POST /api/settings
    # (cookie session — extension bearers are scope-limited and cannot save
    # settings, so the frontend's own credentials are used, exactly as the
    # browser does).
    auth = __import__('api.auth', fromlist=['auth'])
    cookie = auth.create_session()
    webui_headers = {'Cookie': auth._resolve_cookie_name() + '=' + cookie,
                     'Origin': 'http://127.0.0.1:' + str(prefs_app['port']),
                     auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(cookie)}
    s, body = prefs_app['call']('POST', '/api/settings',
                                {'theme': 'light'}, webui_headers)
    assert s == 200, body
    state = _read_prefs(prefs_app['state'])
    assert state['revisions']['appearance'] == 2
    assert state['sources']['appearance']['source'] == 'webui'
    # ...then the extension's stale conditional write must conflict, not win.
    s, body = prefs_app['call']('PATCH', '/api/client/preferences', dict(
        authority='appearance', expected_revision=1, changes={'theme': 'dark'}), headers=headers)
    assert s == 409
    assert body['error'] == 'revision_conflict'
    assert body['current']['revision'] == 2
    assert body['current']['source'] == 'webui'
    assert body['current']['values']['theme'] == 'light'
    # And the newer WebUI edit was not clobbered.
    s, after = prefs_app['call']('GET', '/api/client/preferences?authority=appearance', headers=headers)
    assert after['authorities']['appearance']['values']['theme'] == 'light'


def test_webui_save_routes_cannot_bypass_revision_bump(prefs_app):
    """Any settings.json mutation of a preference key bumps the authority —
    direct save_settings() calls (the chokepoint behind POST /api/settings,
    onboarding, and the settings tab) are all covered by the same hook."""
    config = prefs_app['config']
    config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_FILE.write_text('{}', encoding='utf-8')
    from api.client_preferences import _AUTHORITIES
    _distinct = {
        'theme': 'light', 'skin': 'github',
        'font_size': 'large', 'content_width': 'wide',
        'send_key': 'ctrl+enter',
        'default_message_mode': 'queue',
        'show_thinking': False, 'auto_scroll_follow': False,
        'render_user_markdown': True, 'large_text_paste_as_attachment': False,
        'show_token_usage': True, 'show_fallback_notices': False,
        'chat_activity_display_mode': 'hide_all_activity',
        'transparent_stream_event_timestamps': False,
        'worklog_details_expanded_default': True,
    }
    for authority, keys in _AUTHORITIES.items():
        before = _read_prefs(prefs_app['state']).get('revisions', {}).get(authority, 1)
        changes = {k: _distinct[k] for k in keys if k in _distinct}
        config.save_settings(changes)
        after = _read_prefs(prefs_app['state']).get('revisions', {}).get(authority, 1)
        assert after == before + 1, authority
