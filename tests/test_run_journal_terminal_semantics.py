"""Terminal settlement is one semantic outcome and one transport closure."""

from api import run_journal


def test_duplicate_done_does_not_publish_second_live_outcome(tmp_path):
    writer = run_journal.RunJournalWriter("terminal_session", "run", session_dir=tmp_path)
    live = []
    writer.publish_terminal("done", {"answer": "first"}, live.append)
    writer.publish_terminal("done", {"answer": "second"}, live.append)
    writer.publish_terminal("stream_end", {}, live.append)
    writer.publish_terminal("stream_end", {}, live.append)
    assert [row["event"] for row in live] == ["done", "stream_end"]
    assert [row["event"] for row in run_journal.read_run_events(
        "terminal_session", "run", session_dir=tmp_path)["events"]] == ["done", "stream_end"]


def test_malformed_journal_denies_steer_but_publishes_live_closure(tmp_path):
    writer = run_journal.RunJournalWriter("malformed_session", "run", session_dir=tmp_path)
    writer._path.parent.mkdir(parents=True, exist_ok=True)
    writer._path.write_text("{not json}\n", encoding="utf-8")
    live = []
    accepted = []
    writer.publish_terminal("cancel", {}, live.append)
    outcome = writer.accept_and_append_if_nonterminal(
        "steer_delivered", {}, lambda: accepted.append(True) or True)
    assert outcome[0] is False
    assert accepted == []
    assert len(live) == 1 and live[0]["_synthetic"] is True
    assert writer._path.read_text(encoding="utf-8") == "{not json}\n"


def test_failed_terminal_append_does_not_reopen_steer_admission(tmp_path, monkeypatch):
    writer = run_journal.RunJournalWriter("failed_session", "run", session_dir=tmp_path)
    live = []
    def fail_append(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(run_journal, "_append_run_event_locked", fail_append)
    writer.publish_terminal("cancel", {}, live.append)
    accepted = []
    result = writer.accept_and_append_if_nonterminal(
        "steer_delivered", {"text": "late"}, lambda: accepted.append(True) or True)
    assert result[0] is False
    assert accepted == []
    assert [row["event"] for row in live] == ["cancel"]
