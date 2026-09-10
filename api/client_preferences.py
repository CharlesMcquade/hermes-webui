"""Extension device-grant client-preferences routes (contract: docs/CLIENT-API-CONTRACT.md).

Serves `GET /api/client/capabilities` and `GET`/`PATCH /api/client/preferences`
for paired extension devices holding the narrow `preferences:read` /
`preferences:write` scopes (never implied by `chat`/`control`/`cdp`).

Deliberately a small, allowlisted surface — not a general config mirror:

- Values come from the canonical WebUI settings store (api/config.py
  `load_settings` / `save_settings`); enumerations and the skin registry are
  derived from that module so a registry change fails a parity test instead of
  silently drifting.
- Writes are conditional: the caller supplies the authority-domain revision it
  read; a mismatch returns 409 with the current record (no last-writer-wins).
- Reads never write. Unlike the WebUI tab's boot.js reconciliation, a client
  preference read is an authoritative refresh point only.
- Forbidden keys (password hash, auth acknowledgements, provider config, YAML)
  are unreachable: the authority allowlist names every key that can be served.
"""
import re
import threading
import time

from api import config

SCHEMA_VERSION = 1

# Per-authority allowlists. Every key here must exist in _SETTINGS_DEFAULTS;
# anything not listed can never be read or written through this service.
_AUTHORITIES = {
    'appearance': ('theme', 'skin', 'font_size', 'content_width'),
    'conversation': (
        'show_thinking', 'auto_scroll_follow', 'render_user_markdown',
        'large_text_paste_as_attachment', 'show_token_usage',
        'show_fallback_notices', 'chat_activity_display_mode',
        'transparent_stream_event_timestamps', 'worklog_details_expanded_default',
    ),
    'busy_control': ('default_message_mode',),
    'composer': ('send_key',),
}

# Forbidden keys are unreachable by construction (they are not in the
# allowlists above); the explicit set documents the boundary and the parity
# test asserts no future allowlist row can admit them.
_FORBIDDEN_KEYS = frozenset((
    'password_hash', 'auth_disabled_acknowledged', 'provider_cost_budget',
    'default_workspace', 'dashboard_plugins', 'onboarding_completed',
    'simplified_tool_calling', 'default_model', 'virtualize_transcript_optin',
))

_EXTENSION_AUTHORITY = 'client/preferences'
_EXTENSION_CAPS_AUTHORITY = 'client/capabilities'

_PREFS_LOCK = threading.RLock()


def _authority_for_key(key):
    for authority, keys in _AUTHORITIES.items():
        if key in keys:
            return authority
    return None


def _allowlist():
    return {a: list(keys) for a, keys in _AUTHORITIES.items()}


def _enums():
    enums = {}
    for keys in _AUTHORITIES.values():
        for key in keys:
            if key in config._SETTINGS_ENUM_VALUES:
                enums[key] = sorted(config._SETTINGS_ENUM_VALUES[key])
            elif key == 'theme':
                enums[key] = sorted(config._SETTINGS_THEME_VALUES)
            elif key == 'skin':
                enums[key] = sorted(config._SETTINGS_SKIN_VALUES)
    return enums


def _validate(authority, changes):
    """Validate a change set. Returns (canonical, error) — error is a response
    body dict or None."""
    if not isinstance(changes, dict) or not changes:
        return None, {'error': 'invalid_value', 'field': authority,
                      'constraint': 'changes must be a non-empty object'}
    allowed_keys = set(_AUTHORITIES[authority])
    enums = _enums()
    defaults = config._SETTINGS_DEFAULTS
    canonical = {}
    for key, value in changes.items():
        if key not in allowed_keys:
            return None, {'error': 'unknown_field', 'field': key}
        if key in ('theme', 'skin'):
            if not isinstance(value, str) or not value.strip():
                return None, {'error': 'invalid_value', 'field': key,
                              'constraint': 'non-empty string'}
            theme_value, skin_value = value, None
            if key == 'theme':
                current = config.load_settings()
                skin_value = current.get('skin')
            else:
                current = config.load_settings()
                theme_value = current.get('theme')
            theme, skin = config._normalize_appearance(theme_value, skin_value)
            if value.strip().lower() not in enums[key]:
                return None, {'error': 'invalid_value', 'field': key,
                              'constraint': sorted(enums[key])}
            canonical[key] = value.strip().lower()
            continue
        if key in enums and value not in enums[key]:
            return None, {'error': 'invalid_value', 'field': key,
                          'constraint': sorted(enums[key])}
        if key in config._SETTINGS_INT_RANGES:
            if isinstance(value, bool) or not isinstance(value, int):
                return None, {'error': 'invalid_value', 'field': key,
                              'constraint': 'integer'}
            low, high = config._SETTINGS_INT_RANGES[key]
            if not low <= value <= high:
                return None, {'error': 'invalid_value', 'field': key,
                              'constraint': [low, high]}
        if key in config._SETTINGS_BOOL_KEYS:
            if not isinstance(value, bool):
                return None, {'error': 'invalid_value', 'field': key,
                              'constraint': 'boolean'}
        if not isinstance(defaults.get(key), bool) and key in config._SETTINGS_BOOL_KEYS:
            pass
        canonical[key] = value
    return canonical, None


def _record(authority, settings, revisions, sources):
    keys = _AUTHORITIES[authority]
    values = {}
    for key in keys:
        value = settings.get(key, config._SETTINGS_DEFAULTS.get(key))
        if key in ('theme', 'skin'):
            value = settings.get(key)
        values[key] = value
    return {
        'revision': revisions.get(authority, 1),
        'updated_at': sources.get(authority, {}).get('updated_at', 0.0),
        'source': sources.get(authority, {}).get('source', 'instance'),
        'values': values,
    }


def _load_revisions():
    path = config.STATE_DIR / 'client-preferences.json'
    try:
        import json
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_revisions(data):
    import json
    path = config.STATE_DIR / 'client-preferences.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    from api.config import _atomic_write_settings_text
    _atomic_write_settings_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def get_capabilities():
    from api.updates import WEBUI_VERSION
    return {
        'schema_version': SCHEMA_VERSION,
        'server': {'webui_version': WEBUI_VERSION, 'agent_version': WEBUI_VERSION},
        'preferences': {
            'supported': True,
            'schema_version': SCHEMA_VERSION,
            'authorities': sorted(_AUTHORITIES),
            'allowlist': _allowlist(),
            'enums': _enums(),
        },
        'busy_control': {
            'steer': True,
            'queue': 'browser_local',
            'stop': True,
            'stop_and_send': True,
            'exact_run_precondition': False,
            'mutation_idempotency': 'none',
        },
        'themes': {
            'descriptor_registry': False,
            'registry_version': 0,
            'export_etag': '',
        },
    }


def get_preferences(authority=None):
    if authority is not None and authority not in _AUTHORITIES:
        return None, {'error': 'unknown_authority', 'authority': authority}
    with _PREFS_LOCK:
        state = _load_revisions()
        revisions = state.get('revisions', {})
        sources = state.get('sources', {})
    settings = config.load_settings()
    if authority is not None:
        authorities = {authority: _record(authority, settings, revisions, sources)}
    else:
        authorities = {a: _record(a, settings, revisions, sources) for a in _AUTHORITIES}
    return {
        'schema_version': SCHEMA_VERSION,
        'authorities': authorities,
    }, None


def patch_preferences(authority, expected_revision, changes):
    """Conditional partial update. Returns (body, status)."""
    if authority not in _AUTHORITIES:
        return {'error': 'unknown_authority', 'authority': authority}, 400
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        return {'error': 'invalid_request', 'field': 'expected_revision',
                'constraint': 'integer'}, 400
    canonical, error = _validate(authority, changes)
    if error is not None:
        return error, 400
    with _PREFS_LOCK:
        state = _load_revisions()
        revisions = state.setdefault('revisions', {})
        sources = state.setdefault('sources', {})
        current_revision = revisions.get(authority, 1)
        if expected_revision != current_revision:
            settings = config.load_settings()
            return {
                'error': 'revision_conflict',
                'current': _record(authority, settings, revisions, sources),
            }, 409
        settings = config.load_settings()
        merged = dict(settings)
        merged.update(canonical)
        if 'theme' in canonical or 'skin' in canonical:
            theme = merged.get('theme') if 'theme' in canonical else None
            skin = merged.get('skin') if 'skin' in canonical else None
            if theme is None:
                theme = merged.get('theme')
            if skin is None:
                skin = merged.get('skin')
            merged['theme'], merged['skin'] = config._normalize_appearance(
                theme, skin,
            )
        for key in canonical:
            merged.pop('_set_password', None)
            merged.pop('_clear_password', None)
        saved = config.save_settings(merged)
        forbidden = _FORBIDDEN_KEYS & set(canonical)
        assert not forbidden
        revisions[authority] = current_revision + 1
        sources[authority] = {'updated_at': time.time(), 'source': 'instance'}
        state['revisions'] = revisions
        state['sources'] = sources
        _save_revisions(state)
        return {
            'schema_version': SCHEMA_VERSION,
            'authorities': {
                authority: _record(authority, saved, revisions, sources),
            },
        }, 200
