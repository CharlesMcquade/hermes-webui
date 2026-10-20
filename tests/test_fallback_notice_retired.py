"""W3-R12 R01 invariant tests — persistent fallback-notice machinery retired (#6405).

Negative: no persistent notice is rendered or written on any terminal path.
Positive: upstream live warnings, ordinary saves, served-model identity and
Stop/recovery paths survive.
"""
from __future__ import annotations

import pathlib

from api.streaming import (
    _CANCEL_MARKER_PATTERNS,
    _cancelled_turn_content,
    _finalize_cancelled_turn,
    _is_transient_fallback_warning,
    _persist_cancelled_turn,
    _turn_final_save_commit,
)

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


class _DummySession:
    def __init__(self, path: str = ""):
        self.path = path
        self.messages = []
        self.active_stream_id = "stream-1"
        self.pending_user_message = "hello"
        self.pending_attachments = []
        self.pending_started_at = 123
        self.saved = 0

    def save(self, *args, **kwargs):
        self.saved += 1


class TestRetiredMachineryAbsent:
    def test_no_fallback_notice_symbols_in_streaming(self):
        src = _read("api/streaming.py")
        for symbol in (
            "_publish_fallback_notice",
            "_STREAM_FALLBACK_NOTICES",
            "_STREAM_FALLBACK_DEAD_LETTER",
            "_retire_worker_cancelled_state",
            "_stamp_notice_on_current_turn_row",
            "_capture_fallback_notice_for_row",
            "_settle_latest_fallback_notice_before_teardown",
            "_abandon_stale_stream_settlement",
        ):
            assert symbol not in src, symbol

    def test_no_fallback_notice_writes(self):
        src = _read("api/streaming.py")
        assert "'_fallbackNotice'] =" not in src
        assert '"_fallbackNotice"] =' not in src
        assert "['_fallbackNotice'] =" not in src

    def test_no_persistent_notice_renderer(self):
        ui = _read("static/ui.js")
        assert "_fallbackNoticeHtml" not in ui
        assert "fallbackNoticeHtml" not in ui

    def test_settings_preference_surface_removed(self):
        cfg = _read("api/config.py")
        assert "show_fallback_notices" not in cfg
        html = _read("static/index.html")
        assert "settingsShowFallbackNotices" not in html

    def test_live_warning_channel_retained(self):
        src = _read("api/streaming.py")
        # The status callback still forwards fallback warnings over SSE.
        assert "put('warning', {'type': 'fallback', 'message': _message})" in src
        assert "put('warning', _fallback_data)" in src
        assert "_is_transient_fallback_warning" in src

    def test_served_model_identity_retained(self):
        src = _read("api/streaming.py")
        assert "_capture_turn_served_model" in src
        assert "_usedModel" in src


class TestTerminalWritesAreNoticeFree:
    def test_persist_cancelled_turn_appends_marker_without_notice(self):
        session = _DummySession()
        _persist_cancelled_turn(session, message="Task cancelled.")
        assert len(session.messages) == 1
        marker = session.messages[-1]
        assert "_fallbackNotice" not in marker
        assert marker["_error"] is True

    def test_finalize_cancelled_turn_saves_once_without_notice(self):
        session = _DummySession()
        _finalize_cancelled_turn(session, ephemeral=False)
        assert session.saved >= 1
        assert session.active_stream_id is None
        assert session.messages[-1]["content"] == _cancelled_turn_content("Task cancelled.")
        for m in session.messages:
            assert "_fallbackNotice" not in m

    def test_turn_final_save_commit_is_plain_save_wrapper(self):
        session = _DummySession()
        with _turn_final_save_commit("stream-1", session):
            session.save()
        assert session.saved == 1


class TestTransientClassifierRetained:
    def test_transient_warning_still_classified(self):
        assert _is_transient_fallback_warning("lifecycle", "Trying fallback model: m2")
        assert not _is_transient_fallback_warning("status", "ordinary status line")

    def test_cancel_marker_patterns_unchanged(self):
        assert "task cancelled" in _CANCEL_MARKER_PATTERNS
