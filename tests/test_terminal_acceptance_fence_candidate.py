"""Per-journal admission and terminal transport regression boundaries."""
from api import run_journal as journal


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


def test_publication_failure_keeps_durable_replay_terminal(tmp_path):
    writer = journal.RunJournalWriter('session', 'run', session_dir=tmp_path)
    def fail(_row):
        raise RuntimeError('queue closed')
    event = writer.publish_terminal('cancel', {}, fail)
    assert event['seq'] == 1
    replay = journal.read_run_events('session', 'run', session_dir=tmp_path)['events']
    assert [row['event'] for row in replay] == ['cancel']
    assert writer.accept_and_append_if_nonterminal('steer_delivered', {}, lambda: True)[2] == 'terminal'
