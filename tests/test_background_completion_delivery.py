"""Process completions belong in current work, not one follow-up per process."""
import pytest

from tests.test_wakeup_defer_race import (
    _FakeProcessRegistry, _completion_evt, _install_fake_registry,
    _install_fake_start_session_turn, _reset_cfg_state, _wait_for_wakeup,
)


@pytest.fixture
def delivery(monkeypatch):
    from api import background_process as bp, config as cfg
    fake = _FakeProcessRegistry()
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()
    holder = _install_fake_start_session_turn(monkeypatch)
    sid = "session-delivery"
    bp.register_process_session(sid, sid)
    cfg.ACTIVE_RUNS["stream-delivery"] = {"session_id": sid}
    yield bp, cfg, fake, holder, sid
    _reset_cfg_state()


def enqueue(delivery, count):
    bp, cfg, fake, holder, sid = delivery
    for i in range(count):
        pid = f"proc-{i}"
        fake.register(pid, sid)
        evt = _completion_evt(pid, sid)
        # Same shape as the reported burst: redirected outputs and unknown exit.
        evt.update(command=f"python check-{i}.py > result-{i}.log", exit_code=None)
        bp._process_one(evt)


def test_eight_pending_completions_make_one_idle_continuation(delivery):
    bp, cfg, fake, holder, sid = delivery
    enqueue(delivery, 8)
    cfg.unregister_active_run("stream-delivery")
    assert bp.drain_deferred_wakeups_for_session(sid) == 1
    assert _wait_for_wakeup(holder)
    prompt = holder["calls"][0]["message"]
    assert all(f"proc-{i}" in prompt for i in range(8))
    assert sid not in cfg.DEFERRED_PROCESS_WAKEUPS
    assert bp.drain_deferred_wakeups_for_session(sid) == 0
    assert len(holder["calls"]) == 1


def test_queued_is_not_consumed_and_read_output_retires_wakeup(delivery):
    bp, cfg, fake, holder, sid = delivery
    enqueue(delivery, 2)
    assert fake._completion_consumed == set()
    # This is what core wait()/read_log() sets after actual output consumption.
    fake._completion_consumed.add("proc-0")
    cfg.unregister_active_run("stream-delivery")
    bp.drain_deferred_wakeups_for_session(sid)
    assert _wait_for_wakeup(holder)
    assert "proc-0" not in holder["calls"][0]["message"]
    assert "proc-1" in holder["calls"][0]["message"]




def test_deferred_event_cannot_be_stolen_by_next_turn_queue(delivery):
    from api import streaming
    bp, cfg, fake, holder, sid = delivery
    enqueue(delivery, 1)
    fake.completion_queue.put(_completion_evt("proc-0", sid))
    assert streaming._drain_webui_process_notifications(sid) == []
    assert not fake.is_completion_consumed("proc-0")
    assert len(cfg.DEFERRED_PROCESS_WAKEUPS[sid]) == 1


def test_batch_admission_race_keeps_every_process(delivery, monkeypatch):
    from tests.test_wakeup_defer_race import _wait_for
    bp, cfg, fake, holder, sid = delivery
    holder = _install_fake_start_session_turn(monkeypatch, status=409)
    enqueue(delivery, 8)
    cfg.unregister_active_run("stream-delivery")
    bp.drain_deferred_wakeups_for_session(sid)
    assert _wait_for_wakeup(holder)
    assert _wait_for(lambda: len(cfg.DEFERRED_PROCESS_WAKEUPS.get(sid, [])) == 8)
    assert {e["process_id"] for e in cfg.DEFERRED_PROCESS_WAKEUPS[sid]} == {f"proc-{i}" for i in range(8)}
