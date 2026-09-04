"""Verify bounded delivery against core consumption and repeated queue use."""
from tests.test_background_completion_delivery import delivery, enqueue  # noqa: F401
from tests.test_wakeup_defer_race import _install_fake_registry, _completion_evt, _wait_for


def test_native_wait_suppresses_followup_but_poll_does_not(delivery, monkeypatch):
    bp, cfg, fake, holder, sid = delivery
    import sys
    from tests.conftest import HERMES_AGENT
    monkeypatch.syspath_prepend(str(HERMES_AGENT))
    monkeypatch.delitem(sys.modules, 'tools')
    monkeypatch.delitem(sys.modules, 'tools.process_registry')
    import tools.process_registry as native
    registry = native.ProcessRegistry()
    monkeypatch.setattr(native, 'process_registry', registry)
    ProcessSession = native.ProcessSession
    for pid in ('waited', 'polled', 'unread'):
        session = ProcessSession(id=pid, command='printf synthetic', task_id=sid,
                                 session_key=sid, exited=True, exit_code=0,
                                 output_buffer='Synthetic result', notify_on_complete=True)
        session._completion_event.set()
        registry._finished[pid] = session
        bp._process_one(_completion_evt(pid, sid))
    assert len(cfg.DEFERRED_PROCESS_WAKEUPS[sid]) == 3
    registry.wait('waited', timeout=1)
    registry.poll('polled')
    assert registry.is_completion_consumed('waited')
    assert not registry.is_completion_consumed('polled')
    cfg.unregister_active_run('stream-delivery')
    assert bp.drain_deferred_wakeups_for_session(sid) == 1
    assert _wait_for(lambda: all(registry.is_completion_consumed(pid) for pid in ('waited','polled','unread')))
    assert len(holder['calls']) == 1
    prompt = holder['calls'][0]['message']
    assert 'waited' not in prompt
    assert 'polled' in prompt and 'unread' in prompt
    assert _wait_for(lambda: not cfg.PENDING_BG_TASK_COMPLETIONS)
    assert not cfg.BG_TASK_COMPLETE_EVENTS_SEEN


def test_repeated_batches_leave_no_webui_queue_ownership(delivery):
    bp, cfg, fake, holder, sid = delivery
    for i in range(100):
        cfg.ACTIVE_RUNS['stream-delivery'] = {'session_id':sid}
        for j in range(8):
            pid = f'process-{i}-{j}'
            fake.register(pid, sid)
            bp._process_one(_completion_evt(pid, sid))
        cfg.unregister_active_run('stream-delivery')
        assert bp.drain_deferred_wakeups_for_session(sid) == 1
        assert _wait_for(lambda: not cfg.PENDING_BG_TASK_COMPLETIONS)
        assert not cfg.BG_TASK_COMPLETE_EVENTS_SEEN
        assert not cfg.DEFERRED_PROCESS_WAKEUPS
    assert len(holder['calls']) == 100
