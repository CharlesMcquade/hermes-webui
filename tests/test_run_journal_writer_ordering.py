"""Sequence allocation and physical append must share one journal lock.

Only scheduling at the real append boundary is controlled. Writers, sequence
allocation, on-disk JSONL and the session replay reader are production code.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import threading

import pytest

from api import run_journal


@pytest.mark.parametrize("competitor", ["same_writer", "other_writer", "free_function"])
def test_writer_sequence_follows_physical_append_order(tmp_path, monkeypatch, competitor):
    sid, rid = "writer_order_session", "writer_order_run"
    writer = run_journal.RunJournalWriter(sid, rid, session_dir=tmp_path)
    other = run_journal.RunJournalWriter(sid, rid, session_dir=tmp_path)
    reached = threading.Event()
    release = threading.Event()
    real_append = run_journal.append_run_event

    def pause_first_append(session_id, run_id, event_name, payload=None, **kwargs):
        if payload == {"text": "paused"}:
            reached.set()
            assert release.wait(5), "test did not release the paused append"
        return real_append(session_id, run_id, event_name, payload, **kwargs)

    monkeypatch.setattr(run_journal, "append_run_event", pause_first_append)

    def append_competitor():
        if competitor == "free_function":
            return run_journal.append_run_event(
                sid, rid, "token", {"text": "overtaking"}, session_dir=tmp_path
            )
        target = writer if competitor == "same_writer" else other
        return target.append_sse_event("token", {"text": "overtaking"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer.append_sse_event, "token", {"text": "paused"})
        try:
            assert reached.wait(5), "writer did not reach the append boundary"
            second = pool.submit(append_competitor).result(timeout=5)
        finally:
            release.set()
        first_result = first.result(timeout=5)

    journal = run_journal.read_run_events(sid, rid, session_dir=tmp_path)
    # Deliberately do not sort: the reader requires physical contiguous order.
    assert [row["seq"] for row in journal["events"]] == [1, 2]
    assert [row["payload"]["text"] for row in journal["events"]] == ["overtaking", "paused"]
    assert second["event_id"] == f"{rid}:1"
    assert first_result["event_id"] == f"{rid}:2"
    replay = run_journal.read_session_run_events(
        sid, after_event_id=second["event_id"], session_dir=tmp_path
    )
    assert replay["status"] == "ok"
    assert [row["event_id"] for row in replay["events"]] == [first_result["event_id"]]


def test_writer_rejected_empty_name_does_not_reserve_a_sequence(tmp_path):
    writer = run_journal.RunJournalWriter("name_session", "name_run", session_dir=tmp_path)
    with pytest.raises(ValueError, match="event_name is required"):
        writer.append_sse_event("  ", {"text": "rejected"})
    event = writer.append_sse_event("token", {"text": "accepted"})
    assert event["seq"] == 1
    assert event["event_id"] == "name_run:1"


def test_metering_skip_still_does_not_reserve_or_write(tmp_path):
    writer = run_journal.RunJournalWriter("meter_session", "meter_run", session_dir=tmp_path)
    assert writer.append_sse_event("metering", {"tps": 10}) is None
    assert not (tmp_path / "_run_journal" / "meter_session" / "meter_run.jsonl").exists()
    assert writer.append_sse_event("token", {"text": "kept"})["seq"] == 1


def test_unused_writer_does_not_allocate_a_registry_lock(tmp_path):
    parent = str(tmp_path / run_journal.RUN_JOURNAL_DIR_NAME / "unused_session")
    writer = run_journal.RunJournalWriter("unused_session", "unused_run", session_dir=tmp_path)
    assert writer.append_sse_event("metering", {"tps": 10}) is None
    assert not any(key[0] == parent for key in run_journal._WRITER_LOCKS)


def test_unused_writers_do_not_grow_registry_and_used_lock_is_cleaned_up(tmp_path):
    sid = "bounded_session"
    parent = str(tmp_path / run_journal.RUN_JOURNAL_DIR_NAME / sid)
    writers = [run_journal.RunJournalWriter(sid, f"run_{i}", session_dir=tmp_path)
               for i in range(64)]
    for writer in writers:
        assert writer.append_sse_event("metering", {"tps": 10}) is None
    assert not any(key[0] == parent for key in run_journal._WRITER_LOCKS)
    assert writers[0].append_sse_event("token", {"text": "first"})["seq"] == 1
    assert sum(key[0] == parent for key in run_journal._WRITER_LOCKS) == 1
    assert run_journal.delete_run_journal(sid, session_dir=tmp_path)
    assert not any(key[0] == parent for key in run_journal._WRITER_LOCKS)
    # A surviving writer must reacquire the current registry lock after cleanup.
    assert writers[0].append_sse_event("token", {"text": "again"})["seq"] == 1
    assert sum(key[0] == parent for key in run_journal._WRITER_LOCKS) == 1
    assert run_journal.delete_run_journal(sid, session_dir=tmp_path)


def test_delete_cannot_overtake_accepted_steer_or_split_terminal_lock(tmp_path, monkeypatch):
    """Deletion drains in-flight users before removing/evicting a run path."""
    writer = run_journal.RunJournalWriter("delete_race", "run", session_dir=tmp_path)
    writer.append_sse_event("start", {})
    path = writer._path
    key = (str(path.parent), path.name, str(os.getpid()))
    original_lock = run_journal._WRITER_LOCKS[key]
    entered = threading.Event()
    release = threading.Event()
    delete_started = threading.Event()
    remove_attempted = threading.Event()
    terminal_started = threading.Event()
    real_rmtree = shutil.rmtree

    def traced_rmtree(*args, **kwargs):
        remove_attempted.set()
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", traced_rmtree)

    def accept():
        entered.set()
        assert release.wait(5), "paused Steer was not released"
        return True

    def delete():
        delete_started.set()
        return run_journal.delete_run_journal("delete_race", session_dir=tmp_path)

    def close():
        terminal_started.set()
        return writer.publish_terminal("cancel", {}, lambda _: None)

    with ThreadPoolExecutor(max_workers=3) as pool:
        guiding = pool.submit(writer.accept_and_append_if_nonterminal,
                              "steer_delivered", {"text": "accepted"}, accept)
        try:
            assert entered.wait(5)
            deleting = pool.submit(delete)
            assert delete_started.wait(5)
            closing = pool.submit(close)
            assert terminal_started.wait(5)
            # The old implementation deletes and evicts the old lock while
            # Steer is still in its journal critical section; a terminal then
            # acquires a *different* lock for the very same path.
            assert not remove_attempted.wait(0.2)
            assert run_journal._WRITER_LOCKS[key] is original_lock
            assert not closing.done()
        finally:
            release.set()
        assert guiding.result(timeout=5)[0] is True
        assert deleting.result(timeout=5) is True
        closing.result(timeout=5)


def test_delete_drains_waiting_terminal_before_evicting_path(tmp_path):
    writer = run_journal.RunJournalWriter("delete_waiters", "run", session_dir=tmp_path)
    writer.append_sse_event("start", {})
    key = (str(writer._path.parent), writer._path.name, str(os.getpid()))
    entered = threading.Event()
    release = threading.Event()
    frames = []

    def accept():
        entered.set()
        assert release.wait(5)
        return True

    with ThreadPoolExecutor(max_workers=3) as pool:
        steering = pool.submit(writer.accept_and_append_if_nonterminal,
                               "steer_delivered", {}, accept, publish=frames.append)
        try:
            assert entered.wait(5)
            terminal = pool.submit(writer.publish_terminal, "cancel", {}, frames.append)
            with run_journal._WRITER_LOCKS_COND:
                assert run_journal._WRITER_LOCKS_COND.wait_for(
                    lambda: run_journal._WRITER_LOCK_USERS.get(key) == 2, timeout=5
                ), "terminal did not acquire a waiting lease"
            deleting = pool.submit(run_journal.delete_run_journal,
                                   "delete_waiters", session_dir=tmp_path)
            with run_journal._WRITER_LOCKS_COND:
                assert run_journal._WRITER_LOCKS_COND.wait_for(
                    lambda: str(writer._path.parent) in run_journal._DELETING_SESSION_DIRS,
                    timeout=5,
                ), "deletion did not enter its session gate"
            assert not terminal.done()
            assert not deleting.done()
        finally:
            release.set()
        assert steering.result(timeout=5)[0] is True
        terminal.result(timeout=5)
        assert deleting.result(timeout=5) is True
    assert [frame["event"] for frame in frames] == ["steer_delivered", "cancel"]
    assert not writer._path.exists()
    assert key not in run_journal._WRITER_LOCK_USERS
    assert key not in run_journal._WRITER_LOCKS
    assert str(writer._path.parent) not in run_journal._DELETING_SESSION_DIRS
