"""The WebUI presentation receipt is ordered at acceptance, not at Agent drain.

The Agent's display_kind=steer user row remains the model-history authority;
this event is only a durable rendering boundary and must never carry a private
attachment path or create a second Agent admission.
"""
from api import streaming
from api.run_journal import read_run_events


class ReceiptAgent:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def steer(self, text, *, receipt_id=None):
        self.calls.append((text, receipt_id))
        return self.result


class Channel:
    def __init__(self):
        self.items = []

    def subscribe_with_snapshot(self):
        return (), self.items

    def put_nowait(self, item):
        self.items.append(item)


def _writer(monkeypatch, tmp_path):
    from api.run_journal import RunJournalWriter as Writer
    monkeypatch.setattr(streaming, "RunJournalWriter",
                        lambda sid, run: Writer(sid, run, session_dir=tmp_path))
    return Writer("session", "stream", session_dir=tmp_path)


def _accept(agent, channel, label="Same words", runtime="Same words"):
    return streaming._accept_and_publish_steer_presentation(
        agent, "session", "stream", runtime, display_text=label,
        files=["invoice.pdf"], channel=channel,
    )


def test_accepted_receipts_are_durable_ordered_and_distinct_for_identical_text(monkeypatch, tmp_path):
    _writer(monkeypatch, tmp_path)
    from api import config
    monkeypatch.setattr(config, "STREAM_LAST_EVENT_ID", {})
    agent, channel = ReceiptAgent(), Channel()
    first = _accept(agent, channel)
    second = _accept(agent, channel)
    rows = read_run_events("session", "stream", session_dir=tmp_path)["events"]
    assert len(rows) == len(channel.items) == len(agent.calls) == 2
    assert [row["event"] for row in rows] == ["steer_delivered"] * 2
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert rows[0]["event_id"] != rows[1]["event_id"]
    assert agent.calls[0][1] != agent.calls[1][1]
    assert [row["payload"]["receipt_id"] for row in rows] == [call[1] for call in agent.calls]
    assert all(result["accepted"] and result["durable"] and result["published"]
               for result in (first, second))
    assert [item[2] for item in channel.items] == [row["event_id"] for row in rows]
    assert config.STREAM_LAST_EVENT_ID["stream"] == rows[-1]["event_id"]
    response = streaming._steer_response(first, "session", "stream")
    assert response["steer_event"]["payload"]["receipt_id"] == agent.calls[0][1]
    assert response["steer_event"]["event_id"] == rows[0]["event_id"]


def test_display_receipt_never_contains_runtime_attachment_path(monkeypatch, tmp_path):
    _writer(monkeypatch, tmp_path)
    private = "/private/session/.steer-snapshot-0/invoice.pdf"
    outcome = _accept(ReceiptAgent(), Channel(), "Review attachment", "Review attachment\nread_file " + private)
    response = streaming._steer_response(outcome, "session", "stream")
    assert private not in str(response)
    assert private not in str(read_run_events("session", "stream", session_dir=tmp_path)["events"])
    assert response["steer_event"]["payload"]["files"] == ["invoice.pdf"]


def test_rejection_and_terminal_fence_never_make_a_receipt(monkeypatch, tmp_path):
    writer = _writer(monkeypatch, tmp_path)
    agent = ReceiptAgent(False)
    outcome = _accept(agent, Channel())
    assert outcome["accepted"] is False
    assert not read_run_events("session", "stream", session_dir=tmp_path)["events"]
    writer.close_acceptance_fence()
    other = ReceiptAgent()
    outcome = _accept(other, Channel())
    assert outcome["accepted"] is False and outcome["fallback"] == "stream_dead"
    assert other.calls == []


def test_accepted_but_failed_append_does_not_claim_durability_or_retry(monkeypatch):
    class FailedWriter:
        def __init__(self, *_args):
            pass
        def accept_and_append_if_nonterminal(self, _name, _payload, accept, *, publish):
            assert accept() is True
            return True, None, "persistence_error", OSError("disk full")
    monkeypatch.setattr(streaming, "RunJournalWriter", FailedWriter)
    agent, channel = ReceiptAgent(), Channel()
    outcome = _accept(agent, channel)
    response = streaming._steer_response(outcome, "session", "stream")
    assert len(agent.calls) == 1
    assert channel.items == []
    assert response["accepted"] is True and response["durable"] is False
    assert "steer_event" not in response
    assert response["fallback"] == "persistence_error"


def test_bound_stream_publishes_receipt_for_owned_local_worker(monkeypatch, tmp_path):
    _writer(monkeypatch, tmp_path)
    from api import config
    agent, channel = ReceiptAgent(), Channel()
    monkeypatch.setattr(config, "STREAMS", {"stream": channel})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {"stream": agent})
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {"stream": "session"})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {"stream": {
        "session_id": "session", "backend": streaming.WEBUI_LOCAL_CHAT_BACKEND,
        "phase": "running", }})
    response = streaming._steer_bound_stream(
        "session", "stream", "steer the agent", display_text="steer the agent", files=[])
    assert response["accepted"] is True and response["durable"] is True
    assert response["steer_event"]["payload"]["receipt_id"] == agent.calls[0][1]
    assert channel.items[0][0] == "steer_delivered"


def test_legacy_worker_does_not_publish_unpairable_duplicate(monkeypatch, tmp_path):
    _writer(monkeypatch, tmp_path)
    from api import config
    class OldAgent:
        def __init__(self): self.calls = []
        def steer(self, text): self.calls.append(text); return True
    agent, channel = OldAgent(), Channel()
    monkeypatch.setattr(config, "STREAMS", {"stream": channel})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {"stream": agent})
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", {"stream": "session"})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {"stream": {
        "session_id": "session", "backend": streaming.WEBUI_LOCAL_CHAT_BACKEND,
        "phase": "running", }})
    response = streaming._steer_bound_stream(
        "session", "stream", "legacy", display_text="legacy", files=[])
    assert response == {"accepted": True, "fallback": None, "stream_id": "stream"}
    assert agent.calls == ["legacy"] and channel.items == []
    assert read_run_events("session", "stream", session_dir=tmp_path)["events"] == []


def test_published_event_cannot_precede_its_journal_row(monkeypatch, tmp_path):
    writer = _writer(monkeypatch, tmp_path)
    agent = ReceiptAgent()
    class InspectingChannel(Channel):
        def put_nowait(self, item):
            events = read_run_events("session", "stream", session_dir=tmp_path)["events"]
            assert events and events[-1]["event_id"] == item[2]
            super().put_nowait(item)
    channel = InspectingChannel()
    _accept(agent, channel)
    assert len(channel.items) == 1
