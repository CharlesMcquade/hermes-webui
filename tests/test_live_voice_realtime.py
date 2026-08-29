"""Live voice (OpenAI Realtime WebRTC) — backend + frontend wiring tests."""

import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
VOICE_JS = (ROOT / "static" / "voice_live.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")

from api import voice_live


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)


class _FakeHandler:
    def __init__(self, body=None, command="POST", client=("1.2.3.4", 1)):
        self.command = command
        self.client_address = client
        self._body = json.dumps(body or {}).encode()
        self.headers = {"Content-Length": str(len(self._body)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(self._body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


def _last_json(handler):
    raw = handler.wfile.getvalue()
    return json.loads(raw.decode()) if raw else None


# ── backend validation ──────────────────────────────────────────────


def test_sdp_endpoint_rejects_get():
    h = _FakeHandler(command="GET")
    voice_live.handle_voice_live_sdp(h)
    assert h.status == 405


def test_sdp_endpoint_requires_offer(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "sk-test")
    h = _FakeHandler(body={"sdp": "not-an-sdp"})
    voice_live.handle_voice_live_sdp(h)
    assert h.status == 400


def test_sdp_endpoint_503_without_key(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "")
    monkeypatch.setattr(voice_live, "_rate_limited", lambda c: False)
    h = _FakeHandler(body={"sdp": "v=0\r\no=- 0 0 IN IP4 127.0.0.1"})
    voice_live.handle_voice_live_sdp(h)
    assert h.status == 503


def test_sdp_endpoint_rate_limited(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "sk-test")
    voice_live._RATE_LAST.clear()
    h1 = _FakeHandler(body={"sdp": "not-valid"})  # fails at sdp check before rate
    voice_live.handle_voice_live_sdp(h1)
    # two rapid valid-shaped requests: second must hit 429 (first fails later at network,
    # but consumes the rate slot first)
    import types

    def _fake_urlopen(*a, **k):
        raise OSError("no network in test")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    ha = _FakeHandler(body={"sdp": "v=0\r\ntest"})
    voice_live.handle_voice_live_sdp(ha)
    assert ha.status == 502
    hb = _FakeHandler(body={"sdp": "v=0\r\ntest"})
    voice_live.handle_voice_live_sdp(hb)
    assert hb.status == 429


def test_voice_allowlist_clamps():
    assert "marin" in voice_live.ALLOWED_VOICES
    assert voice_live.DEFAULT_VOICE in voice_live.ALLOWED_VOICES


def test_session_config_is_server_side():
    # The realtime session gets exactly one tool: ask_verity, defined server-side.
    assert voice_live.ASK_VERITY_TOOL["name"] == "ask_verity"
    assert voice_live.ASK_VERITY_TOOL["type"] == "function"
    assert "ask_verity" in voice_live.VOICE_INSTRUCTIONS


def test_capability_endpoint(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "sk-test")
    h = _FakeHandler(command="GET")
    voice_live.handle_voice_live_capability(h)
    assert _last_json(h) == {"available": True}
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "")
    h2 = _FakeHandler(command="GET")
    voice_live.handle_voice_live_capability(h2)
    assert _last_json(h2) == {"available": False}


# ── route wiring ─────────────────────────────────────────────────────


def test_routes_wired():
    assert '"/api/voice/live/sdp"' in ROUTES_PY
    assert '"/api/voice/live/capability"' in ROUTES_PY


# ── frontend wiring ─────────────────────────────────────────────────


def test_index_has_button_and_script():
    assert 'id="btnLiveVoice"' in INDEX_HTML
    assert "static/voice_live.js?v=__WEBUI_VERSION__" in INDEX_HTML


def test_voice_js_uses_backend_and_agent_bridge():
    assert "api/voice/live/sdp" in VOICE_JS
    assert "api/voice/live/capability" in VOICE_JS
    assert "ask_verity" in VOICE_JS
    assert "api/chat" in VOICE_JS
    assert "X-Hermes-CSRF-Token" in VOICE_JS
    # never touches OpenAI directly or embeds a key
    assert "api.openai.com" not in VOICE_JS
    assert "sk-" not in VOICE_JS


def test_css_live_voice_rules():
    assert ".live-voice-btn.live-voice-active" in STYLE_CSS


def test_i18n_keys_all_locales():
    assert len(re.findall(r"live_voice_start:", I18N_JS)) == 15
    assert len(re.findall(r"live_voice_stop:", I18N_JS)) == 15
    assert len(re.findall(r"live_voice_connecting:", I18N_JS)) == 15
