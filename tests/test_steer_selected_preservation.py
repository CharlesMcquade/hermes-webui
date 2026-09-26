"""Selected-branch preservation gates for journal admission and terminal ordering."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api import config, run_journal, streaming
from tests.test_real_steer import _captured_response, _make_handler
from tests.test_steer_worker_boundaries import worker_scene as worker_scene  # noqa: F401


def _steer():
    handler = _make_handler()
    streaming._handle_chat_steer(handler, {"session_id": "original", "text": "guidance"})
    return _captured_response(handler)


@pytest.mark.parametrize("registered", [True, False])
@pytest.mark.parametrize("first", ["steer", "stop"])
def test_stop_and_journal_acceptance_share_one_edge(worker_scene, monkeypatch, registered, first):
    scene = worker_scene
    running, release_run = threading.Event(), threading.Event()
    reached, release = threading.Event(), threading.Event()
    scene.on_run = lambda: (running.set(), release_run.wait(5))
    original_accept = run_journal.RunJournalWriter.accept_and_append_if_nonterminal
    original_update = streaming.update_active_run

    def pause_accept(self, event, payload, accept, **kwargs):
        def gated_accept():
            assert scene.lock.owner == threading.get_ident()
            reached.set()
            assert release.wait(5)
            return accept()
        return original_accept(self, event, payload, gated_accept, **kwargs)

    def pause_cancel(*args, **kwargs):
        if kwargs.get("phase") == "cancelling":
            assert scene.lock.owner == threading.get_ident()
            reached.set()
            assert release.wait(5)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(run_journal.RunJournalWriter, "accept_and_append_if_nonterminal", pause_accept)
    monkeypatch.setattr(streaming, "update_active_run", pause_cancel)
    with ThreadPoolExecutor(max_workers=3) as pool:
        worker = pool.submit(scene.run)
        try:
            assert running.wait(5)
            if not registered:
                with config.STREAMS_LOCK:
                    config.AGENT_INSTANCES.pop("run")
                assert config.SESSION_AGENT_CACHE["original"][0] is scene.agent
            winning = pool.submit(_steer) if first == "steer" else pool.submit(streaming.cancel_stream, "run")
            try:
                assert reached.wait(5)
                losing = pool.submit(streaming.cancel_stream, "run") if first == "steer" else pool.submit(_steer)
                assert scene.lock.contender.wait(5)
                assert not losing.done()
            finally:
                release.set()
            winner_result = winning.result(timeout=5)
            loser_result = losing.result(timeout=5)
        finally:
            release_run.set()
        worker.result(timeout=10)
    response = winner_result if first == "steer" else loser_result
    assert response["accepted"] is (first == "steer")
    assert scene.calls.count("steer") == (1 if first == "steer" else 0)
    rows = run_journal.read_run_events("original", "run")["events"]
    deliveries = [row for row in rows if row["event"] == "steer_delivered"]
    live = [item for item in scene.events.queue if item[0] == "steer_delivered"]
    assert len(deliveries) == len(live) == (1 if first == "steer" else 0)
    if first == "steer":
        assert response["durable"] and response["published"]
        assert deliveries[0]["event_id"] == live[0][2] == response["steer_event"]["event_id"]
        assert loser_result["cancelled"]
    else:
        assert winner_result["cancelled"]
        assert response["fallback"] == "stream_dead"


def test_rotated_registered_agent_final_drain_rejects_concurrent_steer(worker_scene):
    scene = worker_scene
    reached, release = threading.Event(), threading.Event()

    def drain_barrier():
        assert scene.agent.session_id == "compressed-child"
        assert config.AGENT_INSTANCES["run"] is scene.agent
        reached.set()
        assert release.wait(5)

    scene.on_run = lambda: setattr(scene.agent, "session_id", "compressed-child")
    scene.on_drain = drain_barrier
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(scene.run)
        try:
            assert reached.wait(5)
            response = pool.submit(_steer).result(timeout=5)
            assert response["accepted"] is False
            assert response["fallback"] == "not_running"
        finally:
            release.set()
        worker.result(timeout=10)
    assert scene.drained == [""]
    assert not [row for row in run_journal.read_run_events("original", "run")["events"]
                if row["event"] == "steer_delivered"]


def test_leftover_precedes_synthetic_terminal_and_retry_is_once_only(worker_scene, monkeypatch):
    """The real queue cannot recursively call the journal while its lock is held."""
    scene = worker_scene
    scene.on_run = lambda: scene.agent.steer("accepted earlier")
    original_append = run_journal._append_run_event_locked

    def fail_terminal(path, sid, run, event, payload):
        if event in ("done", "stream_end"):
            raise OSError("synthetic terminal test")
        return original_append(path, sid, run, event, payload)

    monkeypatch.setattr(run_journal, "_append_run_event_locked", fail_terminal)
    scene.run()
    events = [item[0] for item in scene.events.queue]
    assert scene.drained == ["accepted earlier"]
    assert events.count("pending_steer_leftover") == 1
    assert events.count("stream_end") == 1
    assert events.index("pending_steer_leftover") < events.index("stream_end")
    writer = run_journal.RunJournalWriter("original", "run")
    published = []
    writer.publish_terminal("done", {}, published.append)
    writer.publish_terminal("stream_end", {}, published.append)
    assert published == []
    assert not [row for row in run_journal.read_run_events("original", "run")["events"]
                if row["event"] in ("done", "stream_end")]
