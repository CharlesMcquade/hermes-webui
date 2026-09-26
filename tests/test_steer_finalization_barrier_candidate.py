"""Deterministic admission/settlement orderings through the real worker and Steer route."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from api import config, streaming
from tests.test_real_steer import _captured_response, _make_handler
from tests.test_steer_worker_boundaries import worker_scene


def _steer():
    handler = _make_handler()
    streaming._handle_chat_steer(handler, {"session_id": "original", "text": "last guidance"})
    return _captured_response(handler)


def test_accepted_before_finalization_is_drained_once(worker_scene):
    scene = worker_scene
    running = threading.Event()
    release = threading.Event()

    def pause_run():
        # The stream's registered agent remains authoritative after compression.
        scene.agent.session_id = "compressed-child"
        running.set()
        assert release.wait(5), "provider barrier timed out"

    scene.on_run = pause_run
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(scene.run)
        try:
            assert running.wait(5), "worker never reached running phase"
            response = _steer()
            assert response["accepted"] is True
            assert response["durable"] is True
            assert scene.agent.pending == ["last guidance"]
        finally:
            release.set()
        worker.result(timeout=10)
    events = list(scene.events.queue)
    leftovers = [data["text"] for event, data, *rest in events if event == "pending_steer_leftover"]
    assert leftovers == ["last guidance"]
    assert scene.agent.pending == []
    assert scene.drained.count("last guidance") == 1
    assert next(i for i, item in enumerate(events) if item[0] == "pending_steer_leftover") < next(
        i for i, item in enumerate(events) if item[0] in ("done", "apperror", "stream_end"))


def test_finalization_before_steer_rejects_without_delivery_append(worker_scene):
    scene = worker_scene
    draining = threading.Event()
    release = threading.Event()
    phase_at_drain = []

    def pause_drain():
        phase_at_drain.append(config.ACTIVE_RUNS["run"]["phase"])
        draining.set()
        assert release.wait(5), "drain barrier timed out"

    scene.on_drain = pause_drain
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(scene.run)
        try:
            assert draining.wait(5), "worker never reached final drain"
            assert phase_at_drain == ["finalizing"]
            assert "run" in config.STREAMS
            response = _steer()
            # The session pointer may already be cleared before final drain.
            # The HTTP contract is rejection with not_running, not a particular
            # stream ID; registry-bound admission still identifies the live run.
            assert response["accepted"] is False
            assert response["fallback"] == "not_running"
            bound = streaming._steer_bound_stream(
                "original", "run", "last guidance", display_text="last guidance", files=[])
            assert bound == {"accepted": False, "fallback": "not_running", "stream_id": "run"}
            assert scene.agent.pending == []
        finally:
            release.set()
        worker.result(timeout=10)
    assert "steer" not in scene.calls
    assert not any(item[0] == "steer_delivered" for item in list(scene.events.queue))
    # The real journal must likewise have no delivery, not merely no live frame.
    from api.run_journal import _run_path
    path = _run_path("original", "run")
    assert path.exists()
    assert not any(json.loads(line).get("event") == "steer_delivered"
                   for line in path.read_text().splitlines())
