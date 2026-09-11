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


def test_embed_route_not_added_to_public_paths():
    """The embed route must NOT be in the auth-exempt PUBLIC_PATHS set."""
    from api.auth import PUBLIC_PATHS

    assert "/embed" not in PUBLIC_PATHS


def test_embed_route_denied_without_session_when_auth_enabled(monkeypatch, tmp_path):
    """With auth enabled and no valid session, /embed must not serve the shell
    (redirect to login, same as `/`) — no auth weakening for the embed route."""
    from api import auth as auth_mod

    monkeypatch.setenv(ENV_VAR, "chrome-extension://japhcdiodeephocbijenihodhnglmfao")
    monkeypatch.setattr(auth_mod, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth_mod, "parse_cookie", lambda handler: "")
    # ensure_trusted_auth_session with no cookie/session must not authorize.
    monkeypatch.setattr(auth_mod, "ensure_trusted_auth_session", lambda handler: None)

    from server import Handler as ServerHandler

    monkeypatch.setattr(
        ServerHandler, "do_GET",
        lambda self: None if not auth_mod.check_auth(self, urlparse(self.path)) else None,
    )

    class _Self:
        command = "GET"
        path = "/embed"
        headers = {}
        request = None
        client_address = ("127.0.0.1", 0)
        _req_t0 = 0.0

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            pass

        def end_headers(self):
            pass

        def _safe_webui_print(self, msg):
            pass

    s = _Self()
    s.path = "/embed"
    ServerHandler.do_GET(s)
    assert s.status == 302  # page redirect to login, same posture as `/`
