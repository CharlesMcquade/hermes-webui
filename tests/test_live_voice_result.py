"""Run-scoped voice results use durable journals, never the current transcript."""
import io
import json

import pytest

from api import run_journal, voice_live


class Handler:
    command = "POST"

    def __init__(self, body):
        raw = json.dumps(body).encode()
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


@pytest.fixture
def journal(monkeypatch, tmp_path):
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path)
    monkeypatch.setattr(voice_live, "_auth_ok", lambda handler: True)

    class Session:
        session_id = "session-a"
        active_stream_id = None

        @property
        def messages(self):
            raise AssertionError("Mutable session messages must never be inspected")

    monkeypatch.setattr(voice_live, "_voice_binding_sid", lambda *args: (Session(), None))
    return lambda event, payload=None, sid="session-a": run_journal.append_run_event(
        sid, "run-a", event, payload
    )


def status(**kwargs):
    handler = Handler({"session_id": "session-a", "stream_id": "run-a", **kwargs})
    voice_live.handle_voice_live_status(handler)
    return handler.status, json.loads(handler.wfile.getvalue())


def test_completed_result_is_from_exact_done_snapshot(journal):
    journal("done", {"session": {"id": "session-a", "messages": [
        {"role": "assistant", "content": "Previous answer"},
        {"role": "user", "content": "This task"},
        {"role": "assistant", "content": "This run's answer"},
    ]}})
    journal("stream_end", {})
    code, body = status()
    assert code == 200
    assert body["stream_id"] == "run-a"
    assert body["terminal"] is True
    assert body["terminal_state"] == "completed"
    assert body["active"] is False
    assert body["answer"] == "This run's answer"
    assert body["result_available"] is True


@pytest.mark.parametrize("event,payload,state", [
    ("apperror", {"message": "failed"}, "errored"),
    ("cancel", {}, "interrupted-by-user"),
    ("error", {"type": "cancelled"}, "interrupted-by-user"),
    ("error", {"type": "interrupted"}, "interrupted-by-crash"),
])
def test_failure_or_cancel_overrides_done_and_stream_end(journal, event, payload, state):
    journal("done", {"session": {"messages": [{"role": "assistant", "content": "Stale"}]}})
    journal(event, payload)
    journal("stream_end")
    code, body = status()
    assert code == 200
    assert body["terminal_state"] == state
    assert body["terminal"] is True
    assert body["active"] is False
    assert body["answer"] is None
    assert body["result_available"] is False


def test_foreign_run_is_indistinguishable_from_missing(journal, monkeypatch):
    missing = status()
    journal("done", {"session": {"messages": [{"role": "assistant", "content": "Secret"}]}}, sid="other")
    monkeypatch.setattr(run_journal, "read_run_events", lambda *args: pytest.fail("foreign journal read"))
    assert status() == missing
    assert missing == (404, {"error": "Run not found"})


def test_journal_disappearing_after_summary_fails_closed(journal, monkeypatch):
    journal("done")
    monkeypatch.setattr(run_journal, "read_run_events", lambda *args: {"events": []})
    assert status() == (404, {"error": "Run not found"})


@pytest.mark.parametrize("stream_id", [None, "", "../run-a", "run-a/evil", " run-a ", 1])
def test_invalid_run_id(journal, stream_id):
    assert status(stream_id=stream_id)[0] == 400


def test_running_reports_only_that_run_progress(journal):
    journal("tool", {"name": "web_search"})
    journal("interim_assistant", {"text": "Checking sources"})
    code, body = status()
    assert code == 200
    assert body["active"] is True
    assert body["terminal"] is False
    assert body["terminal_state"] == "running"
    assert body["answer"] is None
    assert body["recent_tools"] == ["web_search"]
    assert body["latest_step"] == "Checking sources"


@pytest.mark.parametrize("payload", [{}, {"session": {"messages": [
    {"role": "assistant", "content": "Earlier answer"},
    {"role": "user", "content": "New task"},
]}}, {"session": {"messages": [
    {"role": "assistant", "content": "Interim"},
    {"role": "assistant", "content": ""},
]}}])
def test_done_without_final_does_not_invent_answer(journal, payload):
    journal("done", payload)
    code, body = status()
    assert code == 200
    assert body["terminal"] is True
    assert body["answer"] is None
    assert body["result_available"] is False


def test_transport_close_alone_is_not_a_result(journal):
    journal("stream_end")
    assert status()[1]["answer"] is None


@pytest.mark.parametrize("marker", ["_partial", "_interim", "_error", "tool_calls"])
def test_nonfinal_assistant_prose_is_not_a_result(journal, marker):
    journal("done", {"session": {"messages": [
        {"role": "assistant", "content": "Not final", marker: True},
    ]}})
    assert status()[1]["answer"] is None


def test_tool_limit_retains_its_terminal_state(journal):
    journal("done", {"terminal_state": "tool_limit_reached", "session": {
        "messages": [{"role": "assistant", "content": "Partial findings"}],
    }})
    journal("stream_end")
    body = status()[1]
    assert body["terminal_state"] == "tool_limit_reached"
    assert body["answer"] == "Partial findings"


@pytest.mark.parametrize("corruption", ["foreign-envelope", "malformed"])
def test_corrupt_journal_fails_closed(journal, monkeypatch, corruption):
    event = journal("done")
    if corruption == "foreign-envelope":
        event["session_id"] = "other"
    monkeypatch.setattr(run_journal, "read_run_events", lambda *args: {
        "events": [event], "malformed": ["bad row"] if corruption == "malformed" else [],
    })
    assert status() == (409, {"error": "Run journal unavailable"})


def test_unreadable_journal_is_an_explicit_error(journal, monkeypatch):
    def unavailable(*args):
        raise OSError("private filesystem detail")
    monkeypatch.setattr(run_journal, "find_run_summary", unavailable)
    assert status() == (503, {"error": "Run journal unavailable"})


def test_old_status_without_stream_id_preserved(journal):
    handler = Handler({"session_id": "session-a"})
    voice_live.handle_voice_live_status(handler)
    assert json.loads(handler.wfile.getvalue()) == {
        "ok": True, "session_id": "session-a", "active": False, "stream_id": None,
    }


def test_auth_checked_before_any_run_lookup(journal, monkeypatch):
    monkeypatch.setattr(voice_live, "_auth_ok", lambda handler: False)
    monkeypatch.setattr(run_journal, "find_run_summary", lambda *args: pytest.fail("unauthorized lookup"))
    assert status()[0] == 401
