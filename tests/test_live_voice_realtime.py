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
    assert "api/voice/live/ask" in VOICE_JS
    assert "api/voice/live/turn" in VOICE_JS
    assert "api/voice/live/connect" in VOICE_JS
    assert "api/voice/live/disconnect" in VOICE_JS
    assert "X-Hermes-CSRF-Token" in VOICE_JS
    # never touches OpenAI directly or embeds a key
    assert "api.openai.com" not in VOICE_JS
    assert "sk-" not in VOICE_JS


def test_voice_js_async_bridge_contract():
    # busy-guard: the bridge surfaces busy instead of spawning concurrent turns
    assert "data.busy" in VOICE_JS
    assert "api/chat/stream/status" in VOICE_JS
    assert "api/chat/cancel" in VOICE_JS
    # mirrors spoken turns into the transcript
    assert "input_audio_transcription.completed" in VOICE_JS
    assert "_mirrorTurn" in VOICE_JS
    # YOLO lifecycle: connect enables, disconnect restores prior state
    assert "yolo_was_enabled" in VOICE_JS
    assert "_unbindSession" in VOICE_JS


def test_routes_wired_v2():
    assert '"/api/voice/live/ask"' in ROUTES_PY
    assert '"/api/voice/live/turn"' in ROUTES_PY
    assert '"/api/voice/live/connect"' in ROUTES_PY
    assert '"/api/voice/live/disconnect"' in ROUTES_PY


# ── v2 backend unit tests ────────────────────────────────────────────


def _mk_session(tmp_path, monkeypatch, sid="voicetest01", msgs=None):
    """Register a real Session and patch routes' lookup to return it."""
    from api.models import Session
    s = Session(session_id=sid, messages=msgs or [])
    monkeypatch.setattr(
        "api.voice_live._require_webui_session",
        lambda handler, sid_arg: (s, None) if sid_arg == sid else (None, None),
        raising=True,
    )
    return s


def test_connect_enables_yolo_and_returns_digest(monkeypatch):
    from api import voice_live
    from api.route_approvals import enable_session_yolo, disable_session_yolo, is_session_yolo_enabled

    sid = "voicetest02"
    disable_session_yolo(sid)
    from api.models import Session as _S
    sess = _S(session_id=sid, messages=[
        {"role": "user", "content": "hello there", "id": 1},
        {"role": "assistant", "content": "Hi! What can I do for you?", "id": 2},
    ])
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    h = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(h)
    out = _last_json(h)
    assert out["ok"] is True
    assert out["yolo_enabled"] is True
    assert out["yolo_was_enabled"] is False
    assert "hello there" in out["digest"]
    assert is_session_yolo_enabled(sid) is True
    # restore
    disable_session_yolo(sid)


def test_disconnect_restores_prior_yolo_state(monkeypatch):
    from api import voice_live
    from api.route_approvals import enable_session_yolo, disable_session_yolo, is_session_yolo_enabled

    sid = "voicetest03"
    disable_session_yolo(sid)
    from api.models import Session as _S
    sess = _S(session_id=sid)
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    # Simulate connect (server-side binding records prior state)
    h0 = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(h0)
    assert is_session_yolo_enabled(sid) is True
    # disconnect restores the pre-call state WITHOUT trusting client input
    h = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_disconnect(h)
    assert _last_json(h)["ok"] is True
    assert is_session_yolo_enabled(sid) is False
    # if YOLO was already on before the call, disconnect must NOT disable it
    enable_session_yolo(sid)
    h1 = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(h1)
    assert _last_json(h1)["yolo_was_enabled"] is True
    h2 = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_disconnect(h2)
    assert is_session_yolo_enabled(sid) is True
    # reconnect mid-call (page refresh): rebind must KEEP the recorded
    # pre-call state (False here), not re-read the flag we enabled ourselves
    disable_session_yolo(sid)
    voice_live.handle_voice_live_connect(_FakeHandler(body={"session_id": sid}))
    hb = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(hb)
    assert _last_json(hb)["yolo_was_enabled"] is False  # survived the rebind
    voice_live.handle_voice_live_disconnect(_FakeHandler(body={"session_id": sid}))
    assert is_session_yolo_enabled(sid) is False
    disable_session_yolo(sid)


def test_turn_mirror_appends_and_persists(monkeypatch, tmp_path):
    from api import voice_live
    from api.models import Session as _S

    sid = "voicetest04"
    sess = _S(session_id=sid, messages=[{"role": "user", "content": "earlier", "id": 1}])
    saved = []
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    monkeypatch.setattr(type(sess), "save", lambda self, **kw: saved.append(True))
    h = _FakeHandler(body={"session_id": sid, "user_text": "what time is it", "assistant_text": "Noon-ish"})
    voice_live.handle_voice_live_turn(h)
    out = _last_json(h)
    assert out["ok"] is True and out["appended"] == 2
    roles = [m["role"] for m in sess.messages]
    assert roles == ["user", "user", "assistant"]
    assert "[voice] what time is it" in sess.messages[1]["content"]
    assert saved == [True]


def test_turn_mirror_rejects_empty(monkeypatch):
    from api import voice_live
    from api.models import Session as _S

    sess = _S(session_id="voicetest05")
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    h = _FakeHandler(body={"session_id": "voicetest05"})
    voice_live.handle_voice_live_turn(h)
    assert h.status == 400


def test_digest_truncates_and_orders(monkeypatch):
    from api import voice_live
    from api.models import Session as _S

    msgs = [{"role": "user", "content": f"msg {i} " + "x" * 500, "id": i} for i in range(12)]
    sess = _S(session_id="voicetest06", messages=msgs, title="Test Chat")
    digest = voice_live._build_session_digest(sess)
    assert "Test Chat" in digest
    assert "msg 11" in digest       # recent turns included
    assert "msg 0" not in digest    # old turns dropped
    # each turn clipped to ~400 chars
    assert len([l for l in digest.splitlines() if l.startswith("- ")]) == 8


def test_css_live_voice_rules():
    assert ".live-voice-btn.live-voice-active" in STYLE_CSS


def test_i18n_keys_all_locales():
    assert len(re.findall(r"live_voice_start:", I18N_JS)) == 15
    assert len(re.findall(r"live_voice_stop:", I18N_JS)) == 15
    assert len(re.findall(r"live_voice_connecting:", I18N_JS)) == 15
