"""Per-journal admission and terminal transport regression boundaries."""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import run_journal as journal
from api import config, streaming


def test_guidance_before_close_and_close_before_guidance(tmp_path):
    first = journal.RunJournalWriter('session', 'first', session_dir=tmp_path)
    seen = []
    assert first.accept_and_append_if_nonterminal('steer_delivered', {'text': 'a'},
        lambda: seen.append('a') or True)[0]
    first.close_acceptance_fence()
    assert first.accept_and_append_if_nonterminal('steer_delivered', {'text': 'b'},
        lambda: seen.append('b') or True)[2] == 'fence_closed'
    assert seen == ['a']
    second = journal.RunJournalWriter('session', 'second', session_dir=tmp_path)
    second.close_acceptance_fence()
    assert second.accept_and_append_if_nonterminal('steer_delivered', {},
        lambda: seen.append('c') or True)[2] == 'fence_closed'
    assert seen == ['a']


def test_done_then_stream_end_closes_transport_and_replays(tmp_path):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    frames = []
    writer.publish_terminal('done', {'session_id': 'session'}, frames.append)
    writer.publish_terminal('stream_end', {'session_id': 'session'}, frames.append)
    assert [frame['event'] for frame in frames] == ['done', 'stream_end']
    assert [frame['event'] for frame in journal.read_run_events('session', 'run', session_dir=tmp_path)['events']] == ['done', 'stream_end']
    assert frames[-1]['event'] in journal.SSE_RELAY_CLOSE_EVENTS


def test_append_failure_synthesizes_live_only_terminal(tmp_path, monkeypatch):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    frames = []
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(journal, '_append_run_event_locked', fail)
    frame = writer.publish_terminal('cancel', {}, frames.append)
    assert frames == [frame]
    assert frame['_synthetic'] and frame['event_id'] is None
    assert journal.read_run_events('session', 'run', session_dir=tmp_path)['events'] == []
    assert writer.accept_and_append_if_nonterminal('steer_delivered', {}, lambda: True)[2] == 'fence_closed'


def test_stream_end_append_failure_after_done_still_closes_live_transport(tmp_path, monkeypatch):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    frames = []
    writer.publish_terminal('done', {}, frames.append)
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(journal, '_append_run_event_locked', fail)
    writer.publish_terminal('stream_end', {}, frames.append)
    assert frames[-1]['event'] == 'stream_end' and frames[-1]['event_id'] is None
    assert [row['event'] for row in journal.read_run_events('session', 'run', session_dir=tmp_path)['events']] == ['done']
    assert writer.accept_and_append_if_nonterminal('steer_delivered', {}, lambda: True)[2] == 'terminal'


def test_publication_failure_keeps_durable_replay_terminal(tmp_path):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    def fail(_row):
        raise RuntimeError('queue closed')
    with pytest.raises(RuntimeError, match='queue closed'):
        writer.publish_terminal('cancel', {}, fail)
    replay = journal.read_run_events('session', 'run', session_dir=tmp_path)['events']
    assert [row['event'] for row in replay] == ['cancel']
    assert writer.accept_and_append_if_nonterminal('steer_delivered', {}, lambda: True)[2] == 'terminal'


def test_duplicate_stream_end_does_not_append_or_publish_twice(tmp_path):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    frames = []
    writer.publish_terminal('done', {}, frames.append)
    writer.publish_terminal('stream_end', {}, frames.append)
    writer.publish_terminal('stream_end', {}, frames.append)
    assert [e['event'] for e in journal.read_run_events('session', 'run', session_dir=tmp_path)['events']] == ['done', 'stream_end']
    assert [e['event'] for e in frames] == ['done', 'stream_end']


@pytest.mark.parametrize('close_first', [False, True])
def test_concurrent_steer_and_terminal_closure_are_ordered(tmp_path, close_first):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    entered = threading.Event()
    release = threading.Event()
    accepted = []
    frames = []

    def steer():
        return writer.accept_and_append_if_nonterminal('steer_delivered', {},
            lambda: accepted.append(True) or True, publish=frames.append)

    def terminal():
        writer.close_acceptance_fence()
        entered.set()
        assert release.wait(5)
        return writer.publish_terminal('cancel', {}, frames.append)

    with ThreadPoolExecutor(max_workers=2) as pool:
        if close_first:
            ending = pool.submit(terminal)
            assert entered.wait(5)
            guiding = pool.submit(steer)
            release.set()
        else:
            guiding = pool.submit(steer)
            assert guiding.result(timeout=5)[0]
            ending = pool.submit(terminal)
            assert entered.wait(5)
            release.set()
        result = guiding.result(timeout=5)
        ending.result(timeout=5)
    assert result[0] is (not close_first)
    assert len(accepted) == (not close_first)
    assert [row['event'] for row in frames] == (['steer_delivered'] if not close_first else []) + ['cancel']


@pytest.mark.parametrize('detached', [False, True])
def test_real_stop_journals_admitted_identity_and_closes_transport(tmp_path, monkeypatch, detached):
    sid, run = 'admitted_session', 'admitted_run'
    agent = Mock(session_id='rotated_session')
    channel = config.create_stream_channel()
    session = SimpleNamespace(active_stream_id=run, messages=[],
        pending_user_message=None, pending_attachments=[], pending_started_at=None,
        pending_user_source=None, save=Mock())
    for name, value in {
        'STREAMS': {} if detached else {run: channel}, 'CANCEL_FLAGS': {run: threading.Event()},
        'AGENT_INSTANCES': {run: agent}, 'STREAM_SESSION_OWNERS': {run: sid},
        'ACTIVE_RUNS': {run: {'session_id': sid, 'phase': 'running', 'backend': 'legacy'}},
        'SESSION_AGENT_CACHE': {}, 'STREAM_PARTIAL_TEXT': {},
        'STREAM_REASONING_TEXT': {}, 'STREAM_LIVE_TOOL_CALLS': {},
    }.items():
        monkeypatch.setattr(config, name, value)
        if hasattr(streaming, name):
            monkeypatch.setattr(streaming, name, value)
    monkeypatch.setattr(streaming, 'get_session', lambda _sid: session)
    monkeypatch.setattr(streaming, '_resolve_current_session_for_write', lambda s: s)
    monkeypatch.setattr(streaming, '_stream_writeback_is_current', lambda *_: True)
    monkeypatch.setattr(streaming, '_get_session_agent_lock', lambda _: nullcontext())
    monkeypatch.setattr(streaming, '_redacted_session_payload_with_full_messages', lambda _: {})
    monkeypatch.setattr(journal, '_run_path', lambda session_id, run_id, session_dir=None:
        tmp_path / session_id / f'{run_id}.jsonl')
    subscriber, _ = channel.subscribe_with_snapshot()
    result = streaming.cancel_stream(run)
    assert result['cancelled'] is True
    rows = journal.read_run_events(sid, run, session_dir=tmp_path)['events']
    assert rows[-1]['event'] == 'cancel'
    assert rows[-1]['session_id'] == sid
    assert journal.read_run_events('rotated_session', run, session_dir=tmp_path)['events'] == []
    if not detached:
        item = subscriber.get_nowait()
        assert item[0] == 'cancel' and item[2] == rows[-1]['event_id']
        assert channel.subscribe_with_snapshot()[1]['last_event_id'] == rows[-1]['event_id']
