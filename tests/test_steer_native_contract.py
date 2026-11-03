"""Steer presentation receipt + native model-history contract.

An ordered WebUI journal receipt renders guidance at acceptance time; Agent
consumption still produces one native display_kind=steer user row. Explicit
receipt IDs pair these records for display, never by comparing steer text.
Ownership, leftover delivery, and attachment path verification remain intact.
"""
from __future__ import annotations

import json
import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


class TestJournalReceiptDoesNotReplaceNativeSteer:
    def test_backend_keeps_native_agent_admission_and_adds_ordered_receipt(self):
        src = _read("api/streaming.py")
        assert "accept_and_append_if_nonterminal" in src
        assert "agent.steer(runtime_text, receipt_id=receipt_id)" in src
        assert "steer_delivered" in src
        assert "_steer_runtime_text(" in src
        assert 'if "receipt_id" not in steer_params:' in src

    def test_frontend_has_no_custom_projection(self):
        cmds = _read("static/commands.js")
        assert "_recordSteerEventFromResponse" not in cmds
        assert "steer_event" not in cmds
        assert "persistenceDegraded" not in cmds
        ui = _read("static/ui.js")
        for symbol in (
            "_anchorSceneDeliveredSteerRows",
            "_renderLiveAnchorSteerRowsOnly",
            "_renderSettledAnchorSteerRowsOnlyForMessage",
            "data-steer-delivery",
            "data-steer-only",
        ):
            assert symbol not in ui, symbol
        messages = _read("static/messages.js")
        assert "steer_delivered" not in messages
        anchors = _read("static/assistant_turn_anchors.js")
        assert "steer_delivered" not in anchors

    def test_response_exposes_only_actual_durable_receipts(self):
        from api.streaming import _steer_response
        bare = _steer_response({"accepted": True}, "sid", "stream")
        assert set(bare) == {"accepted", "fallback", "stream_id"}
        failed = _steer_response({"accepted": True, "durable": False,
                                  "published": False, "event": None}, "sid", "stream")
        assert failed["accepted"] and not failed["durable"]
        assert "steer_event" not in failed
        receipt = _steer_response({
            "accepted": True, "durable": True, "published": True,
            "event": {"event_id": "stream:5", "seq": 5, "run_id": "stream",
                      "session_id": "sid", "created_at": 123},
            "payload": {"receipt_id": "opaque", "text": "go left"},
        }, "sid", "stream")
        assert receipt["steer_event"]["payload"]["receipt_id"] == "opaque"
        assert receipt["steer_event"]["event_id"] == "stream:5"


class TestNativeSteerPathRetained:
    def test_post_queue_path_and_runtime_text_builder_retained(self):
        src = _read("api/streaming.py")
        assert "def _handle_chat_steer(" in src
        assert "def _steer_bound_stream(" in src
        assert "agent.steer(text)" in src
        assert "_steer_runtime_text(" in src
        cmds = _read("static/commands.js")
        assert "'/api/chat/steer'" in cmds
        assert "user_text:originalMsg" in cmds
        assert "attachment_paths:attachmentPaths" in cmds

    def test_pending_steer_leftover_sse_retained(self):
        src = _read("api/streaming.py")
        assert "put('pending_steer_leftover'" in src
        messages = _read("static/messages.js")
        assert "source.addEventListener('pending_steer_leftover'" in messages

    def test_native_steer_row_unwrap_retained(self):
        from api.streaming import _unwrap_steer_row_oob_marker

        row = {"role": "user", "display_kind": "steer", "content": "go left"}
        _unwrap_steer_row_oob_marker(row)
        assert row["role"] == "user"

    def test_steer_attachment_verification_retained(self, tmp_path, monkeypatch):
        from api import streaming, upload

        attachment_root = tmp_path / "attachments"
        monkeypatch.setattr(upload, "_attachment_root", lambda: attachment_root)
        session_dir = upload._session_attachment_dir("sid")
        session_dir.mkdir(parents=True)
        uploaded = session_dir / "benign.pdf"
        uploaded.write_text("content", encoding="utf-8")

        snapshot = streaming._verified_steer_attachment_paths("sid", [str(uploaded)])
        assert len(snapshot) == 1 and snapshot[0] != str(uploaded)
        assert Path(snapshot[0]).read_text() == "content"
        with pytest.raises(ValueError, match="not a session upload"):
            streaming._verified_steer_attachment_paths(
                "sid",
                [str(tmp_path / "Ignore previous instructions" / "benign.pdf")],
            )


class TestSteerOwnershipEdges:
    def _setup(self, monkeypatch, phase="running", backend="legacy", with_agent=True):
        from api import config
        from api.config import (
            ACTIVE_RUNS, ACTIVE_RUNS_LOCK, AGENT_INSTANCES,
            SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK,
            STREAMS, STREAMS_LOCK,
        )

        sid, stream_id = "sid_native", "stream_native"
        agent = MagicMock()
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig") if with_agent else None
        with STREAMS_LOCK:
            STREAMS[stream_id] = queue.Queue()
            if with_agent:
                AGENT_INSTANCES[stream_id] = agent
        monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {stream_id: sid})
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[stream_id] = {"session_id": sid, "backend": backend, "phase": phase}
        return agent, sid, stream_id

    def _teardown(self, stream_id, sid):
        from api.config import ACTIVE_RUNS, AGENT_INSTANCES, SESSION_AGENT_CACHE, STREAMS

        STREAMS.pop(stream_id, None)
        AGENT_INSTANCES.pop(stream_id, None)
        ACTIVE_RUNS.pop(stream_id, None)
        SESSION_AGENT_CACHE.pop(sid, None)

    def test_accepted_steer_calls_agent_queue_only(self, monkeypatch):
        from api.streaming import _steer_bound_stream

        agent, sid, stream_id = self._setup(monkeypatch)
        try:
            outcome = _steer_bound_stream(sid, stream_id, "go left",
                                          display_text="go left", files=[])
            assert outcome["accepted"] is True
            agent.steer.assert_called_once_with("go left")
        finally:
            self._teardown(stream_id, sid)

    def test_finalizing_phase_surfaces_not_running(self, monkeypatch):
        from api.streaming import _steer_bound_stream

        agent, sid, stream_id = self._setup(monkeypatch, phase="finalizing")
        try:
            outcome = _steer_bound_stream(sid, stream_id, "too late",
                                          display_text="too late", files=[])
            assert outcome["accepted"] is False
            assert outcome["fallback"] == "not_running"
            agent.steer.assert_not_called()
        finally:
            self._teardown(stream_id, sid)

    def test_cancelling_phase_surfaces_stream_dead(self, monkeypatch):
        from api.streaming import _steer_bound_stream

        agent, sid, stream_id = self._setup(monkeypatch, phase="cancelling")
        try:
            outcome = _steer_bound_stream(sid, stream_id, "nope",
                                          display_text="nope", files=[])
            assert outcome["accepted"] is False
            assert outcome["fallback"] == "stream_dead"
            agent.steer.assert_not_called()
        finally:
            self._teardown(stream_id, sid)

    def test_gateway_backend_surfaces_queue_fallback(self, monkeypatch):
        from api.streaming import _steer_bound_stream

        agent, sid, stream_id = self._setup(monkeypatch, backend="gateway")
        try:
            outcome = _steer_bound_stream(sid, stream_id, "later",
                                          display_text="later", files=[])
            assert outcome["accepted"] is False
            assert outcome["fallback"] == "gateway_steer_queued"
            assert outcome["stream_id"] == stream_id
            agent.steer.assert_not_called()
        finally:
            self._teardown(stream_id, sid)

    def test_steer_failure_is_distinct_from_http_success(self, monkeypatch):
        from api.streaming import _steer_bound_stream

        agent, sid, stream_id = self._setup(monkeypatch)
        agent.steer.side_effect = RuntimeError("boom")
        try:
            outcome = _steer_bound_stream(sid, stream_id, "x",
                                          display_text="x", files=[])
            assert outcome["accepted"] is False
            assert outcome["fallback"] == "steer_error"
        finally:
            self._teardown(stream_id, sid)
