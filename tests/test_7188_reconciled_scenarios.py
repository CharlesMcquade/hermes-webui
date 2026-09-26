"""Cross-branch #7188 behavior: compression journal identity and archive Steer."""
import threading
from unittest.mock import MagicMock

import pytest

from api import config, models, run_journal, streaming, upload
from api.models import Session


@pytest.fixture
def isolated_run(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", sessions)
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: sessions)
    for registry in (models.SESSIONS, config.STREAMS, config.CANCEL_FLAGS,
                     config.AGENT_INSTANCES, config.ACTIVE_RUNS,
                     config.STREAM_SESSION_OWNERS, config.SESSION_WRITEBACK_OWNERS):
        registry.clear()
    yield sessions
    for registry in (models.SESSIONS, config.STREAMS, config.CANCEL_FLAGS,
                     config.AGENT_INSTANCES, config.ACTIVE_RUNS,
                     config.STREAM_SESSION_OWNERS, config.SESSION_WRITEBACK_OWNERS):
        registry.clear()


def test_cancel_rotated_agent_keeps_terminal_in_admission_journal(isolated_run):
    original, continuation, run = "journal_origin_7188", "journal_tip_7188", "run_7188"
    session = Session(session_id=continuation, title="continuation", messages=[])
    session.active_stream_id = run
    session.save()
    models.SESSIONS[continuation] = session
    config.register_stream_owner(run, original)
    config.register_session_writeback_owner(original, run)
    channel = config.create_stream_channel()
    config.STREAMS[run] = channel
    config.CANCEL_FLAGS[run] = threading.Event()
    agent = MagicMock()
    agent.session_id = continuation
    agent.interrupt.return_value = True
    config.AGENT_INSTANCES[run] = agent
    config.register_active_run(run, session_id=original, phase="running")
    writer = run_journal.RunJournalWriter(original, run, session_dir=isolated_run)
    writer.append_and_publish_sse_event("steer_delivered", {"text": "guide"}, lambda _: None)

    outcome = streaming.cancel_stream(run)
    assert (outcome if isinstance(outcome, bool) else outcome["cancelled"]) is True
    events = run_journal.read_run_events(original, run, session_dir=isolated_run)["events"]
    assert [event["event"] for event in events] == ["steer_delivered", "cancel"]
    assert not run_journal.read_run_events(continuation, run, session_dir=isolated_run)["events"]
    assert session.active_stream_id is None


def test_archive_steer_expands_contained_files_and_rejects_symlink(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _: root)
    archive = root / "extracted"
    archive.mkdir(parents=True)
    (archive / "one.txt").write_text("one")
    nested = archive / "nested"
    nested.mkdir()
    (nested / "two.txt").write_text("two")
    snapshots = streaming._verified_steer_attachment_paths("sid", [str(archive)])
    assert [open(path).read() for path in snapshots] == ["two", "one"]
    assert all(".steer-snapshot-" in path for path in snapshots)
    (archive / "one.txt").write_text("changed")
    assert open(snapshots[1]).read() == "one"
    with pytest.raises(ValueError):
        streaming._verified_steer_attachment_paths("sid", [str(root)])
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (archive / "escape").symlink_to(outside)
    with pytest.raises(ValueError):
        streaming._verified_steer_attachment_paths("sid", [str(archive)])


def test_archive_steer_rejects_ancestor_links_and_swapped_member(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _: root)
    outside = tmp_path / "secret"
    outside.write_text("secret")
    directory = root / "archive"
    directory.mkdir()
    (directory / "file").write_text("safe")
    (root / "alias").symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError):
        streaming._verified_steer_attachment_paths("sid", [str(root / "alias" / "file")])
    (directory / "file").unlink()
    (directory / "file").symlink_to(outside)
    with pytest.raises(ValueError):
        streaming._verified_steer_attachment_paths("sid", [str(directory)])
    assert not list(root.glob(".steer-snapshot-*"))


def test_archive_steer_count_empty_duplicates_and_snapshot_reentry(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    directory = root / "archive"
    directory.mkdir(parents=True)
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _: root)
    for index in range(21):
        (directory / f"{index:02}.txt").write_text(str(index))
    with pytest.raises(ValueError, match="20"):
        streaming._verified_steer_attachment_paths("sid", [str(directory)])
    assert not list(root.glob(".steer-snapshot-*"))
    file = directory / "00.txt"
    for paths in ([""], [str(file), str(file)], [str(file)] * 21):
        with pytest.raises(ValueError):
            streaming._verified_steer_attachment_paths("sid", paths)
    (directory / "20.txt").unlink()
    paths = streaming._verified_steer_attachment_paths("sid", [str(file), str(directory)])
    assert len(paths) == 20  # overlap deduplicated by relative identity
    with pytest.raises(ValueError):
        streaming._verified_steer_attachment_paths("sid", [paths[0]])


def test_terminal_relay_closes_after_done_and_synthetic_errors(tmp_path, monkeypatch):
    writer = run_journal.RunJournalWriter("relay_sid", "relay_run", session_dir=tmp_path)
    published = []
    writer.publish_terminal("done", {"session": {}}, published.append)
    writer.publish_terminal("stream_end", {}, published.append)
    assert [event["event"] for event in published] == ["done", "stream_end"]

    failing = run_journal.RunJournalWriter("failed_sid", "failed_run", session_dir=tmp_path)
    published.clear()
    def broken_append(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(run_journal, "_append_run_event_locked", broken_append)
    failing.publish_terminal("apperror", {}, published.append)
    assert len(published) == 1
    assert published[0]["event"] == "apperror"
    assert published[0].get("_synthetic") is True


def test_archive_steer_accepts_ui_empty_attachment_list(tmp_path, monkeypatch):
    """Regression: the UI always sends attachment_paths, [] for a text-only steer."""
    root = tmp_path / "uploads"
    root.mkdir(parents=True)
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _: root)
    assert streaming._verified_steer_attachment_paths("sid", []) == []
    assert streaming._verified_steer_attachment_paths("sid", None) == []
    with pytest.raises(ValueError):
        streaming._verified_steer_attachment_paths("sid", "not-a-list")
    assert not list(root.glob(".steer-snapshot-*"))
