"""Live voice realtime token usage logging (/api/voice/live/usage).

Covers: request validation, once-per-response dedupe, the JSONL detail log
(including the audio/text token breakdown state.db has no column for), and
the opt-in delta into state.db `session_model_usage` under
task='voice_realtime'.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api import voice_live  # noqa: E402


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    import api.auth as auth

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)


@pytest.fixture(autouse=True)
def _fresh_dedupe_cache():
    voice_live._USAGE_REPORTED.clear()
    yield
    voice_live._USAGE_REPORTED.clear()


@pytest.fixture()
def bound_session(monkeypatch):
    """A valid session binding without materializing a real WebUI session."""
    fake = SimpleNamespace(session_id="voice-usage-sess")
    monkeypatch.setattr(
        voice_live, "_require_webui_session", lambda handler, sid: (fake, None)
    )
    return fake


class _FakeHandler:
    def __init__(self, body=None, command="POST", client=("1.2.3.4", 1)):
        self.command = command
        self.client_address = client
        self._body = json.dumps(body or {}).encode()
        self.headers = {"Content-Length": str(len(self._body)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(self._body)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


def _last_json(handler):
    raw = handler.wfile.getvalue()
    return json.loads(raw.decode()) if raw else None


_FULL_USAGE = {
    "total_tokens": 155,
    "input_tokens": 100,
    "output_tokens": 50,
    "input_token_details": {
        "text_tokens": 60,
        "audio_tokens": 40,
        "cached_tokens": 40,
        "cached_tokens_details": {"text_tokens": 40},
    },
    "output_token_details": {"text_tokens": 30, "audio_tokens": 15, "reasoning_tokens": 5},
}


# ── validation ───────────────────────────────────────────────────────


def test_usage_endpoint_rejects_get():
    h = _FakeHandler(command="GET")
    voice_live.handle_voice_live_usage(h)
    assert h.status == 405


def test_usage_endpoint_rejects_unauthorized(monkeypatch):
    import api.auth as auth

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: None)
    h = _FakeHandler(body={"session_id": "voice-usage-sess"})
    voice_live.handle_voice_live_usage(h)
    assert h.status == 401


def test_usage_endpoint_requires_session(monkeypatch):
    # Real validator: an unsafe session id is rejected before anything else.
    h = _FakeHandler(body={"session_id": "../escape"})
    voice_live.handle_voice_live_usage(h)
    assert h.status == 400


def test_usage_endpoint_requires_response_id_and_usage(bound_session):
    h = _FakeHandler(body={"session_id": "voice-usage-sess"})
    voice_live.handle_voice_live_usage(h)
    assert h.status == 400

    h = _FakeHandler(body={"session_id": "voice-usage-sess", "response_id": "r1"})
    voice_live.handle_voice_live_usage(h)
    assert h.status == 400

    h = _FakeHandler(body={"session_id": "voice-usage-sess", "usage": {"input_tokens": 1}})
    voice_live.handle_voice_live_usage(h)
    assert h.status == 400


# ── dedupe + JSONL detail log ────────────────────────────────────────


def test_usage_dedupes_by_response_id(bound_session, tmp_path, monkeypatch):
    log_path = tmp_path / "voice_usage.jsonl"
    monkeypatch.setattr(voice_live, "_voice_usage_log_path", lambda: log_path)

    body = {"session_id": "voice-usage-sess", "response_id": "resp-1", "usage": _FULL_USAGE}
    h1 = _FakeHandler(body=body)
    voice_live.handle_voice_live_usage(h1)
    assert _last_json(h1) == {"ok": True}

    h2 = _FakeHandler(body=body)
    voice_live.handle_voice_live_usage(h2)
    assert _last_json(h2) == {"ok": True, "duplicate": True}

    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 1


def test_usage_jsonl_row_carries_full_breakdown(bound_session, tmp_path, monkeypatch):
    log_path = tmp_path / "voice_usage.jsonl"
    monkeypatch.setattr(voice_live, "_voice_usage_log_path", lambda: log_path)

    h = _FakeHandler(
        body={"session_id": "voice-usage-sess", "response_id": "resp-1", "usage": _FULL_USAGE}
    )
    voice_live.handle_voice_live_usage(h)
    assert h.status == 200

    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "voice-usage-sess"
    assert row["response_id"] == "resp-1"
    assert row["model"] == "gpt-realtime-2.1"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["total_tokens"] == 155
    assert row["cached_tokens"] == 40
    assert row["reasoning_tokens"] == 5
    # The audio/text breakdown only survives here — state.db has no columns for it.
    assert row["input_token_details"]["audio_tokens"] == 40
    assert row["output_token_details"]["audio_tokens"] == 15


def test_usage_tolerates_malformed_details(bound_session, tmp_path, monkeypatch):
    log_path = tmp_path / "voice_usage.jsonl"
    monkeypatch.setattr(voice_live, "_voice_usage_log_path", lambda: log_path)

    h = _FakeHandler(
        body={
            "session_id": "voice-usage-sess",
            "response_id": "resp-2",
            "usage": {
                "input_tokens": "7",
                "output_tokens": None,
                "input_token_details": "garbage",
                "output_token_details": ["also", "garbage"],
            },
        }
    )
    voice_live.handle_voice_live_usage(h)
    assert h.status == 200

    row = json.loads(log_path.read_text().splitlines()[0])
    assert row["input_tokens"] == 7
    assert row["output_tokens"] == 0
    assert row["cached_tokens"] == 0
    assert row["input_token_details"] is None
    assert row["output_token_details"] is None


# ── state.db delta (sync_to_insights opt-in) ─────────────────────────


@pytest.fixture()
def temp_state_db(tmp_path, monkeypatch):
    """Temp HERMES home with a schema-initialized state.db."""
    pytest.importorskip("hermes_state")
    from hermes_state import SessionDB

    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "state.db").touch()
    SessionDB(home / "state.db").close()

    import api.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "get_active_hermes_home", lambda: home)
    return home


def _set_sync_setting(monkeypatch, enabled):
    import api.config as config_mod

    monkeypatch.setattr(config_mod, "load_settings", lambda: {"sync_to_insights": enabled})


def _report(handler_body_overrides, bound_session, tmp_path, monkeypatch):
    log_path = tmp_path / "voice_usage.jsonl"
    monkeypatch.setattr(voice_live, "_voice_usage_log_path", lambda: log_path)
    h = _FakeHandler(body={"session_id": "voice-usage-sess", **handler_body_overrides})
    voice_live.handle_voice_live_usage(h)
    assert h.status == 200
    return h


def _usage_row(home):
    conn = sqlite3.connect(home / "state.db")
    try:
        return conn.execute(
            "SELECT api_call_count, input_tokens, output_tokens, cache_read_tokens, "
            "reasoning_tokens FROM session_model_usage WHERE session_id=? AND model=? AND task=?",
            ("voice-usage-sess", "gpt-realtime-2.1", "voice_realtime"),
        ).fetchone()
    finally:
        conn.close()


def test_usage_db_delta_accumulates_when_opted_in(
    bound_session, tmp_path, monkeypatch, temp_state_db
):
    _set_sync_setting(monkeypatch, True)

    _report(
        {"response_id": "resp-1", "usage": _FULL_USAGE}, bound_session, tmp_path, monkeypatch
    )
    assert _usage_row(temp_state_db) == (1, 100, 50, 40, 5)

    _report(
        {
            "response_id": "resp-2",
            "usage": {"input_tokens": 200, "output_tokens": 60},
        },
        bound_session,
        tmp_path,
        monkeypatch,
    )
    # Deltas accumulate: 2 calls, 300 in / 110 out / 40 cached / 5 reasoning.
    assert _usage_row(temp_state_db) == (2, 300, 110, 40, 5)


def test_usage_db_untouched_when_opted_out(
    bound_session, tmp_path, monkeypatch, temp_state_db
):
    _set_sync_setting(monkeypatch, False)

    _report(
        {"response_id": "resp-1", "usage": _FULL_USAGE}, bound_session, tmp_path, monkeypatch
    )

    conn = sqlite3.connect(temp_state_db / "state.db")
    try:
        count = conn.execute("SELECT COUNT(*) FROM session_model_usage").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_usage_survives_state_db_failure(
    bound_session, tmp_path, monkeypatch, temp_state_db
):
    """A broken state.db write must not fail the request or skip the JSONL log."""
    _set_sync_setting(monkeypatch, True)

    import hermes_state

    def _boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(hermes_state.SessionDB, "record_auxiliary_usage", _boom)

    h = _report(
        {"response_id": "resp-1", "usage": _FULL_USAGE}, bound_session, tmp_path, monkeypatch
    )
    assert _last_json(h) == {"ok": True}

    log_path = tmp_path / "voice_usage.jsonl"
    rows = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(rows) == 1
