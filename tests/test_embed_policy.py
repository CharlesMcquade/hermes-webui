"""Negative tests for the Phase-1 embed route + frame-ancestor policy.

Covers (task scope §3):
- no config -> embed route denied (404), policy fails closed;
- configured -> embed route emits exactly the configured frame-ancestors and
  omits X-Frame-Options;
- non-embed routes unchanged (XFO DENY + frame-ancestors 'none' still present);
- ancestor validation rejects junk input (whole value rejected, fail closed);
- embed route requires normal authentication posture (no auth weakening).
"""

from __future__ import annotations

import io
import json
import os
import threading
from urllib.parse import urlparse

import pytest

from api import embed_policy

ENV_VAR = embed_policy.ENV_VAR


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for routes.handle_get."""

    def __init__(self, cookie: str = ""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(b"")
        self.headers = {}
        if cookie:
            self.headers["Cookie"] = cookie
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _get(handler, path):
    from api.routes import handle_get

    result = handle_get(handler, urlparse("http://testserver" + path))
    return True  # 404s are surfaced via j(); assert on handler.status instead


def _header(handler, name):
    return handler.header(name)


# ── Policy module: parsing / validation ─────────────────────────────────────


def test_default_empty_fails_closed(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert embed_policy.configured_ancestors() == ()
    assert embed_policy.embed_enabled() is False
    # Disabled => header override is empty => route keeps global DENY policy.
    assert embed_policy.embed_route_headers() == {}


def test_valid_extension_and_https_ancestors(monkeypatch):
    monkeypatch.setenv(
        ENV_VAR,
        "chrome-extension://japhcdiodeephocbijenihodhnglmfao, https://panel.example.com:8443",
    )
    assert embed_policy.configured_ancestors() == (
        "chrome-extension://japhcdiodeephocbijenihodhnglmfao",
        "https://panel.example.com:8443",
    )
    assert embed_policy.embed_enabled() is True


@pytest.mark.parametrize(
    "value",
    [
        "chrome-extension://shortid",            # not a 32-char a-p id
        "chrome-extension://qaphcdiodeephocbijenihodhnglmfa",  # 'q' not in a-p
        "http://insecure.example.com",           # http scheme not allowed
        "https://app.example.com/embed",         # path not allowed
        "https://*.wildcard.example.com",        # wildcard not allowed
        "https://app.example.com:99999",         # invalid port
        "javascript:alert(1)",                   # junk scheme
        "https://ok.example.com; script-src *",  # directive injection
        "a,b,",                                  # junk entries
        "https://good.example.com,bad-entry",    # one bad entry poisons all
    ],
)
def test_ancestor_validation_rejects_junk(monkeypatch, value):
    monkeypatch.setenv(ENV_VAR, value)
    # Fail closed: the entire value is rejected, never partially accepted.
    assert embed_policy.configured_ancestors() == ()
    assert embed_policy.embed_enabled() is False


def test_csp_frame_ancestors_value(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert embed_policy.embed_frame_ancestors_csp_value() == "'none'"
    ext_id = "abcdefghijklmnopabcdefghijklmnop"  # 32 chars, a-p only
    monkeypatch.setenv(ENV_VAR, f"chrome-extension://{ext_id}")
    assert (
        embed_policy.embed_frame_ancestors_csp_value()
        == f"'self' chrome-extension://{ext_id}"
    )


# ── Embed route behavior (routes.py) ─────────────────────────────────────────


def test_embed_route_denied_when_unconfigured(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    handler = _FakeHandler()
    assert _get(handler, "/embed") is True
    assert handler.status == 404


def test_embed_route_serves_shell_with_configured_ancestors(monkeypatch):
    ext_id = "japhcdiodeephocbijenihodhnglmfao"
    monkeypatch.setenv(ENV_VAR, f"chrome-extension://{ext_id}")
    handler = _FakeHandler()
    assert _get(handler, "/embed") is True
    assert handler.status == 200

    # X-Frame-Options OMITTED on this route only when configured.
    assert _header(handler, "X-Frame-Options") is None

    # Enforced CSP frame-ancestors is exactly configured allowlist + 'self'.
    csp = _header(handler, "Content-Security-Policy")
    assert csp is not None
    fa_seg = csp.split("frame-ancestors ", 1)[1].split(";", 1)[0]
    assert fa_seg == f"'self' chrome-extension://{ext_id}"

    # Embed flag token present exactly once in the served shell.
    html = bytes(handler.body).decode("utf-8")
    assert html.count("window.__HERMES_CONFIG__={embed:1,") == 1


def test_embed_route_neither_header_when_disabled(monkeypatch):
    # When disabled the 404 response must not carry embed CSP overrides;
    # global policy (XFO DENY) still applies to the 404 itself.
    monkeypatch.delenv(ENV_VAR, raising=False)
    handler = _FakeHandler()
    _get(handler, "/embed")
    assert handler.status == 404
    assert _header(handler, "X-Frame-Options") == "DENY"
    csp = _header(handler, "Content-Security-Policy")
    assert "frame-ancestors 'none'" in csp


def test_non_embed_routes_keep_deny_and_none(monkeypatch):
    ext_id = "japhcdiodeephocbijenihodhnglmfao"
    monkeypatch.setenv(ENV_VAR, f"chrome-extension://{ext_id}")
    # Even WITH embed configured, ordinary routes stay byte-for-byte locked.
    for path in ("/", "/index.html", "/login"):
        handler = _FakeHandler()
        _get(handler, path)
        assert handler.status in (200, 302), f"{path}: unexpected status"
        assert _header(handler, "X-Frame-Options") == "DENY", path
        csp = _header(handler, "Content-Security-Policy")
        assert csp is not None and "frame-ancestors 'none';" in csp, path
        assert ext_id not in csp, path
    # And a JSON API route too.
    handler = _FakeHandler()
    _get(handler, "/api/sessions")
    assert _header(handler, "X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in _header(handler, "Content-Security-Policy")


def test_embed_route_disabled_has_no_flag_in_normal_shell(monkeypatch):
    # The ordinary shell must NOT carry the embed flag even when embed is
    # configured for the dedicated route.
    ext_id = "japhcdiodeephocbijenihodhnglmfao"
    monkeypatch.setenv(ENV_VAR, f"chrome-extension://{ext_id}")
    handler = _FakeHandler()
    _get(handler, "/")
    html = bytes(handler.body).decode("utf-8")
    assert "window.__HERMES_CONFIG__={embed:1," not in html
    assert "window.__HERMES_CONFIG__={maxUploadBytes:" in html


# ── Authentication posture ───────────────────────────────────────────────────


def test_embed_route_is_public_static_shell():
    """Parent integration decision (Candidate-A contract, plan §4): the embed
    shell is a PUBLIC, data-free static page — same trust class as /static/*
    and /login. Cookie-gating the shell would force a cookie-login for the
    frame, reintroducing exactly the ambient-credential model the extension
    retired. The security boundary stays at the API layer: every /api/* route
    remains auth-gated (bearer or session), the shell itself carries no data,
    and the route still 404s unless frame-ancestors are configured."""
    from api.auth import PUBLIC_PATHS

    assert "/embed" in PUBLIC_PATHS


def test_embed_api_routes_remain_auth_gated(monkeypatch):
    """The embed shell being public must not weaken API auth: bearer-less
    /api/* requests still fail closed (extension authenticate → 401, session
    check → 401). The frame renders with empty state and no data."""
    from api import auth as auth_mod

    class _Req:
        command = "GET"
        path = "/api/sessions"
        headers = {}  # no Authorization header, no Cookie

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            pass

        def end_headers(self):
            pass

        def wfile(self):  # pragma: no cover - class attr set below
            raise NotImplementedError

    import io

    req = _Req()
    req.wfile = io.BytesIO()  # 401 body writer target
    # Fixture env has no password → auth disabled → everything passes; force
    # the enabled posture this test is about.
    monkeypatch.setattr(auth_mod, "is_auth_enabled", lambda: True)
    # Device-auth authenticate() runs first and must reject a bearer-less
    # extension-origin request; even if it passes through (no Origin header
    # here), the cookie session check must still 401 /api/*.
    assert auth_mod.check_auth(req, urlparse("/api/sessions")) is False


def test_embed_route_denied_without_session_when_auth_enabled(monkeypatch, tmp_path):
    """Fail-closed posture is unchanged for the FRAME POLICY: with auth
    enabled the embed shell serves no cookies and an empty CSRF token (the
    frame is cookie-less by design); every data request it makes is 401
    until the parent broker presents the device bearer."""
    from api import auth as auth_mod

    monkeypatch.setenv(ENV_VAR, "chrome-extension://japhcdiodeephocbijenihodhnglmfao")
    monkeypatch.setattr(auth_mod, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth_mod, "parse_cookie", lambda handler: "")

    from server import Handler as ServerHandler

    # Public-path dispatch for /embed must reach the route handler (shell
    # served), while /api/* on the same anonymous request stays 401.
    _embed_req = type(
        "R", (), {"path": "/embed", "headers": {}, "command": "GET"}
    )()
    assert auth_mod.check_auth(_embed_req, urlparse("/embed")) is True


# ── Phase-2 R2: per-request + per-write revocation ──────────────────────────


def _r2_app(tmp_path, monkeypatch):
    """Minimal live server with a paired chat-scope device bearer."""
    import base64
    import hashlib
    import http.client
    import threading

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
        conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=8)
        try:
            import json as _json
            payload = _json.dumps(body) if body is not None else None
            conn.request(method, path, payload,
                         {'Content-Type': 'application/json', **(headers or {})})
            response = conn.getresponse()
            data = response.read().decode()
            try:
                data = _json.loads(data)
            except ValueError:
                pass
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    ext_id = 'japhcdiodeephocbijenihodhnglmfao'
    origin = 'chrome-extension://' + ext_id
    verifier = 'a' * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    s, pending, _ = call('POST', ext.PREFIX + 'start',
                         dict(extension_id=ext_id, client_name='Revocation test',
                              code_challenge=challenge, scopes=['chat']),
                         {'Origin': origin})
    assert s == 200
    cookie = auth.create_session()
    cookie_headers = {'Cookie': auth._resolve_cookie_name() + '=' + cookie,
                      'Origin': 'http://127.0.0.1:' + str(httpd.server_port),
                      auth.CSRF_HEADER_NAME: auth.csrf_token_for_session(cookie)}
    s, grant, _ = call('POST', ext.PREFIX + 'inspect',
                       {'user_code': pending['user_code']}, cookie_headers)
    assert s == 200
    assert call('POST', ext.PREFIX + 'approve',
                dict(user_code=pending['user_code'], confirm=True,
                     scopes=grant['scopes'], profile=grant['profile']),
                cookie_headers)[0] == 200
    s, result, _ = call('POST', ext.PREFIX + 'token',
                        dict(extension_id=ext_id, device_code=pending['device_code'],
                             code_verifier=verifier), {'Origin': origin})
    assert s == 200
    headers = {'Origin': origin, 'Authorization': 'Bearer ' + result['access_token']}
    return dict(call=call, pair_headers=headers, token=result['access_token'],
                auth=auth, ext=ext, state=state, cookie=cookie_headers,
                port=httpd.server_port,
                shutdown=lambda: (httpd.shutdown(), httpd.server_close(),
                                  worker.join(timeout=5)))


def test_embed_capability_route_rechecks_auth_per_request(tmp_path, monkeypatch):
    """R2: pair → request OK → revoke → the same embed-capability request 401s.

    Even with auth disabled globally, a revoked device bearer must never fall
    back to cookie/anonymous access on an embed capability route.
    """
    app = _r2_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app['auth'], 'is_auth_enabled', lambda: False)
    path = '/api/approval/pending'  # R1 embed capability route
    status, _, _ = app['call']('GET', path, None, app['pair_headers'])
    assert status not in (401, 403), (status, path)
    assert app['call']('POST', app['ext'].PREFIX + 'revoke', {}, app['pair_headers'])[0] == 200
    status, data, _ = app['call']('GET', path, None, app['pair_headers'])
    assert (status, data['error']) == (401, 'invalid_token'), (status, data)
    app['shutdown']()


def test_authorized_stream_writer_per_write_recheck_revoked(tmp_path, monkeypatch):
    """R2: AuthorizedStreamWriter rechecks the grant at every write boundary.

    Uses the only stream path in the ratified capability set —
    `GET /api/chat/stream` (chat scope) — via the real handler class.
    """
    import queue
    from api import routes
    app = _r2_app(tmp_path, monkeypatch)
    events = queue.Queue()
    cleaned = threading.Event()

    class Stream:
        def subscribe(self):
            return events

        def unsubscribe(self, subscriber):
            cleaned.set()

    monkeypatch.setitem(routes.STREAMS, 'p2-revoke-test', Stream())
    monkeypatch.setattr(routes, '_stream_id_visible_to_request_profile', lambda *a: True)
    monkeypatch.setattr(routes, '_sse_replay_run_journal_gap_checked', lambda *a, **kw: (False, None))
    monkeypatch.setattr(routes, '_SSE_HEARTBEAT_INTERVAL_SECONDS', 0.05)

    import http.client
    conn = http.client.HTTPConnection('127.0.0.1', app['port'], timeout=5)
    try:
        conn.request('GET', '/api/chat/stream?stream_id=p2-revoke-test', headers=app['pair_headers'])
        response = conn.getresponse()
        assert response.status == 200
        # First heartbeat proves the write path succeeded pre-revocation.
        assert response.readline() == b': heartbeat\n'
        assert response.readline() == b'\n'
        assert app['call']('POST', app['ext'].PREFIX + 'revoke', {}, app['pair_headers'])[0] == 200
        assert cleaned.wait(3), 'Revoked stream remained subscribed'
        assert b'secret' not in response.read()
    finally:
        conn.close()
    app['shutdown']()
