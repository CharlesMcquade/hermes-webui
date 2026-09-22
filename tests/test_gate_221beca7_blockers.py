"""Gate review 221beca7 regressions: admission, handoff, projection truth.

Covers the four still-open blocker families at the review head:

1. Backstop-truthful artifact projection: the display path must report a
   clipped state.db read instead of presenting it as complete.
2. Admission reservation at the earliest shared boundary for /api/btw,
   /api/background, goal kickoffs, and process-wakeup turns, before any
   session/external mutation.
3. One ownership token across the companion gateway restart and the WebUI
   replacement scheduler: the drain is never released into the handoff.
4. The alive-thread queue-removal race retires the route's concrete starting
   row on the worker's pre-start exit.
"""
import json
import os
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import config, gateway_chat, gateway_restart, routes, updates


@pytest.fixture(autouse=True)
def isolated_drain(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    yield
    config.exit_restart_drain()
    gateway_restart._GATEWAY_RESTART_DRAIN_HANDOFF.clear()


# ---------------------------------------------------------------------------
# Blocker 1: truthful complete artifact projection
# ---------------------------------------------------------------------------

class _StubBackstopSession:
    def __init__(self):
        self.truncation_watermark = None
        self.truncation_boundary = None


def test_backstop_clipped_state_db_read_is_detected(monkeypatch):
    """limit+1 probe: a full page proves the backstop clipped the source."""
    from api.routes import _STATE_DB_DISPLAY_ROW_BACKSTOP

    backstop = _STATE_DB_DISPLAY_ROW_BACKSTOP
    full_page = [{'role': 'user', 'content': str(i)} for i in range(backstop + 1)]
    assert routes._state_db_read_capped_by_backstop(backstop, full_page) is True
    # A short page (or an uncapped read) stays complete.
    assert routes._state_db_read_capped_by_backstop(backstop, full_page[:backstop]) is False
    assert routes._state_db_read_capped_by_backstop(None, full_page) is False


# ---------------------------------------------------------------------------
# Blocker 2: admission reservation at the earliest shared boundary
# ---------------------------------------------------------------------------

def _drain_env(monkeypatch, tmp_path):
    (tmp_path / f"{os.getpid()}.json").write_text(
        json.dumps({"generation": config._RESTART_DRAIN_GENERATION}) + "\n"
    )


@pytest.mark.parametrize('path,body,mutator', [
    ('/api/btw', {'session_id': 'x', 'question': 'hi'}, 'get_session'),
    ('/api/background', {'session_id': 'x', 'prompt': 'hi'}, 'get_session'),
    ('/api/goal', {'session_id': 'x', 'args': 'ship it'}, 'get_session'),
])
def test_btw_background_goal_refuse_before_session_mutation(
    monkeypatch, tmp_path, path, body, mutator
):
    _drain_env(monkeypatch, tmp_path)
    observed = {}

    def fake_json(_handler, payload, status=200):
        observed.update({'payload': payload, 'status': status})
        return True

    monkeypatch.setattr(routes, 'j', fake_json)
    monkeypatch.setattr(
        routes, mutator,
        Mock(side_effect=AssertionError('session mutated before admission refusal')),
    )
    handler = SimpleNamespace()
    if path == '/api/btw':
        routes._handle_btw(handler, body)
    elif path == '/api/background':
        routes._handle_background(handler, body)
    else:
        routes._handle_goal_command(handler, body)
    assert observed['status'] == 503
    assert observed['payload']['code'] == 'restart_draining'


def test_process_wakeup_turn_refused_before_session_resolution(monkeypatch, tmp_path):
    _drain_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        routes, 'get_session',
        Mock(side_effect=AssertionError('session resolved before admission refusal')),
    )
    resp = routes.start_session_turn('session-9', 'wake up', source='process_wakeup')
    assert resp['_status'] == 503
    assert resp['code'] == 'restart_draining'


def test_admitted_producers_release_their_reservation(monkeypatch, tmp_path):
    """A non-draining run must not leave admission reservations behind."""
    monkeypatch.setattr(routes, '_agent_runtime_barrier_response', lambda **k: None)
    monkeypatch.setattr(
        routes, 'get_session',
        Mock(side_effect=KeyError('session-404')),
    )

    def fake_json(_handler, payload, status=200):
        return True

    monkeypatch.setattr(routes, 'j', fake_json)
    monkeypatch.setattr(routes, 'bad', lambda handler, msg, status=400: fake_json(handler, {'error': msg}, status=status))
    routes._handle_btw(SimpleNamespace(), {'session_id': 'x', 'question': 'hi'})
    assert not any(
        str(key).startswith('admission:') for key in (config.ACTIVE_RUNS or {})
    )


# ---------------------------------------------------------------------------
# Blocker 3: one ownership token across gateway restart and WebUI scheduling
# ---------------------------------------------------------------------------

def test_completed_gateway_restart_parks_drain_for_scheduler(monkeypatch, tmp_path):
    """handoff_to_scheduler keeps admission closed after a completed restart."""
    class FakeProc:
        returncode = 0
        def communicate(self, timeout):
            return ('ok', '')

    monkeypatch.setattr(
        gateway_restart.subprocess, 'Popen', Mock(return_value=FakeProc())
    )
    monkeypatch.setattr(
        gateway_restart, '_wait_until_restart_safe',
        lambda: {'restart_blocked': False},
    )
    outcome = gateway_restart.restart_active_profile_gateway(handoff_to_scheduler=True)
    assert outcome['status'] == 'completed'
    assert config.restart_drain_active()
    # The scheduler claims the token and owns the same marker.
    assert gateway_restart.claim_parked_restart_drain() is True
    assert config.restart_drain_active()
    assert gateway_restart.claim_parked_restart_drain() is False


def test_health_gateway_restart_releases_drain_on_completed(monkeypatch, tmp_path):
    """Without handoff the completed restart stays terminal (legacy behavior)."""
    class FakeProc:
        returncode = 0
        def communicate(self, timeout):
            return ('ok', '')

    monkeypatch.setattr(
        gateway_restart.subprocess, 'Popen', Mock(return_value=FakeProc())
    )
    monkeypatch.setattr(
        gateway_restart, '_wait_until_restart_safe',
        lambda: {'restart_blocked': False},
    )
    outcome = gateway_restart.restart_active_profile_gateway()
    assert outcome['status'] == 'completed'
    assert not config.restart_drain_active()
    assert gateway_restart.claim_parked_restart_drain() is False


def test_parked_drain_expires_when_no_scheduler_claims(monkeypatch, tmp_path):
    """A health-side park that never gets claimed must release admission."""
    class FakeProc:
        returncode = 0
        def communicate(self, timeout):
            return ('ok', '')

    monkeypatch.setattr(
        gateway_restart.subprocess, 'Popen', Mock(return_value=FakeProc())
    )
    monkeypatch.setattr(
        gateway_restart, '_wait_until_restart_safe',
        lambda: {'restart_blocked': False},
    )
    monkeypatch.setattr(
        gateway_restart, '_GATEWAY_RESTART_HANDOFF_EXPIRY_SECONDS', 0.05
    )
    outcome = gateway_restart.restart_active_profile_gateway(handoff_to_scheduler=True)
    assert outcome['status'] == 'completed'
    assert config.restart_drain_active()
    import time as _time
    _time.sleep(0.5)
    assert not config.restart_drain_active()
    assert not gateway_restart._GATEWAY_RESTART_DRAIN_HANDOFF.is_set()


def test_scheduler_claims_parked_drain_without_republication(monkeypatch, tmp_path):
    """_schedule_restart with a parked token runs on the already-open drain."""
    config.enter_restart_drain(reason='gateway_restart')
    gateway_restart._GATEWAY_RESTART_DRAIN_HANDOFF.set()

    scheduled = {}
    monkeypatch.setattr(updates, '_drain_and_reexec', lambda delay: None, raising=False)

    def fake_thread(target, daemon=None):
        scheduled['target'] = target

        class Done:
            def start(self):
                pass

        return Done()

    monkeypatch.setattr(updates.threading, 'Thread', fake_thread)
    updates._schedule_restart(delay=0)
    # The claim consumed the parked token; admission stays closed under the
    # same marker (no second enter_restart_drain needed).
    assert config.restart_drain_active()
    assert not gateway_restart._GATEWAY_RESTART_DRAIN_HANDOFF.is_set()


# ---------------------------------------------------------------------------
# Blocker 4: alive-thread queue-removal race retires the starting row
# ---------------------------------------------------------------------------

def test_local_worker_pre_start_exit_retires_route_starting_row(monkeypatch):
    """cancel removes the stream before the worker registers: the row must go."""
    stream_id = 'race-local-starting'
    events = queue.Queue()
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = events
    routes.api_config.register_active_run(
        stream_id, session_id='sess-race-local', phase='starting'
    )
    try:
        import api.streaming as streaming
        streaming._run_agent_streaming(
            session_id='sess-race-local',
            msg_text='hello',
            model='m',
            workspace='/tmp',
            stream_id=stream_id,
        )
        assert stream_id not in config.ACTIVE_RUNS
        # The worker also released the stream-owner entry it never owned.
        assert routes.stream_owner_session_id(stream_id) is None
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)
        config.unregister_active_run(stream_id)


def test_gateway_worker_pre_start_exit_retires_route_starting_row(monkeypatch):
    stream_id = 'race-gateway-starting'
    events = queue.Queue()
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = events
    routes.api_config.register_active_run(
        stream_id, session_id='sess-race-gateway', phase='starting'
    )
    try:
        gateway_chat._run_gateway_chat_streaming(
            session_id='sess-race-gateway',
            msg_text='hello',
            model='m',
            workspace='/tmp',
            stream_id=stream_id,
        )
        assert stream_id not in config.ACTIVE_RUNS
        assert routes.stream_owner_session_id(stream_id) is None
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)
        config.unregister_active_run(stream_id)
