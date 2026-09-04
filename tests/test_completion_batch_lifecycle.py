"""Queue ownership survives dispatch and retires after actual disposition."""
import pytest
from tests.test_background_completion_delivery import delivery, enqueue  # noqa: F401
from tests.test_wakeup_defer_race import _wait_for, _install_fake_start_session_turn


def test_all_consumed_completions_release_session_state(delivery):
    bp, cfg, fake, holder, sid = delivery
    enqueue(delivery, 2)
    fake._completion_consumed.update(['proc-0', 'proc-1'])
    cfg.unregister_active_run('stream-delivery')
    assert bp.drain_deferred_wakeups_for_session(sid) == 0
    assert sid not in cfg.PENDING_BG_TASK_COMPLETIONS
    assert sid not in cfg.BG_TASK_COMPLETE_EVENTS_SEEN
    assert holder['calls'] == []


def test_dispatch_keeps_pending_until_acceptance(delivery, monkeypatch):
    bp, cfg, fake, holder, sid = delivery
    enqueue(delivery, 2)
    cfg.unregister_active_run('stream-delivery')
    monkeypatch.setattr(bp, '_start_server_side_wakeup_turn', lambda *a, **k: None)
    assert bp.drain_deferred_wakeups_for_session(sid) == 1
    assert sid in cfg.PENDING_BG_TASK_COMPLETIONS
    assert fake._completion_consumed == set()


@pytest.mark.parametrize('status', [500, 503])
def test_failed_admission_retains_each_result_without_ack(delivery, monkeypatch, status):
    bp, cfg, fake, holder, sid = delivery
    _install_fake_start_session_turn(monkeypatch, status=status)
    enqueue(delivery, 2)
    cfg.unregister_active_run('stream-delivery')
    bp.drain_deferred_wakeups_for_session(sid)
    assert _wait_for(lambda: len(cfg.DEFERRED_PROCESS_WAKEUPS.get(sid, [])) == 2)
    assert fake._completion_consumed == set()
    assert sid in cfg.PENDING_BG_TASK_COMPLETIONS


def test_success_retires_session_state(delivery):
    bp, cfg, fake, holder, sid = delivery
    enqueue(delivery, 2)
    cfg.unregister_active_run('stream-delivery')
    bp.drain_deferred_wakeups_for_session(sid)
    assert _wait_for(lambda: fake._completion_consumed == {'proc-0', 'proc-1'})
    assert _wait_for(lambda: sid not in cfg.BG_TASK_COMPLETE_EVENTS_SEEN)
    assert sid not in cfg.PENDING_BG_TASK_COMPLETIONS
