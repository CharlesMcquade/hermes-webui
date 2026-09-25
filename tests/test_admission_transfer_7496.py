"""Atomic request reservation → concrete worker admission across restart drain."""
import threading

import pytest


def test_transfer_after_drain_and_worker_upgrade(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-transfer'
    stream_id = '7496-concrete'
    ready = threading.Event()
    proceed = threading.Event()
    outcome = []

    def producer():
        config.register_active_run(reservation, phase='admitting')
        ready.set()
        assert proceed.wait(5)
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = object()
            config.transfer_run_admission(reservation, stream_id, session_id='owner')
        config.unregister_active_run(reservation)
        with config.STREAMS_LOCK:
            config.register_active_run(stream_id, session_id='owner', phase='running')
        outcome.append(config.ACTIVE_RUNS[stream_id]['phase'])

    thread = threading.Thread(target=producer)
    thread.start()
    try:
        assert ready.wait(5)
        config.enter_restart_drain('test')
        proceed.set()
        thread.join(5)
        assert not thread.is_alive()
        assert outcome == ['running']
        assert reservation not in config.ACTIVE_RUNS
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run('7496-unreserved', session_id='owner')
    finally:
        proceed.set()
        thread.join(5)
        config.exit_restart_drain()
        config.unregister_active_run(stream_id)
        config.unregister_active_run(reservation)
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)


def test_transfer_rejects_stolen_or_absent_reservation(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-stolen'
    concrete = '7496-stolen-concrete'
    config.register_active_run(reservation, phase='admitting')
    try:
        config.update_active_run(reservation, phase='running')
        with pytest.raises(config.RunAdmissionDrainingError):
            config.transfer_run_admission(reservation, concrete, session_id='owner')
        assert concrete not in config.ACTIVE_RUNS
        config.unregister_active_run(reservation)
        with pytest.raises(config.RunAdmissionDrainingError):
            config.transfer_run_admission(reservation, concrete, session_id='owner')
    finally:
        config.unregister_active_run(reservation)


def test_drain_rejects_cross_session_worker_upgrade(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv('HERMES_WEBUI_RESTART_DRAIN_DIR', str(tmp_path))
    reservation = 'admission:7496-owner'
    concrete = '7496-owner-concrete'
    config.register_active_run(reservation, phase='admitting')
    try:
        config.transfer_run_admission(reservation, concrete, session_id='owner')
        config.enter_restart_drain('test')
        with pytest.raises(config.RunAdmissionDrainingError):
            config.register_active_run(concrete, session_id='other', phase='running')
        assert config.ACTIVE_RUNS[concrete]['session_id'] == 'owner'
    finally:
        config.exit_restart_drain()
        config.unregister_active_run(concrete)
        config.unregister_active_run(reservation)
