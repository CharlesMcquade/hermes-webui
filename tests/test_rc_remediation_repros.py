"""Phase 0 failing-on-baseline regressions for the RC remediation plan.

Ported from the executable eval spec (/Users/charles/extension-eval-evidence/
test_backend_repros.py). Each test asserts a PRODUCT INVARIANT, so it is RED
on the pre-fix baseline (where the defect is confirmed) and must turn GREEN
only when Phase 1 fixes the product behavior:

1. test_stale_steer_rejected_for_successor_run — a steer POST whose
   expected_stream_id names a retired run must be rejected; the successor
   run must receive no guidance (STEER_BASELINE_RED diagnostic on baseline).
2. test_preference_race_preserves_disjoint_edits — a WebUI-side edit of
   field A made while an extension PATCH of field B is paused mid-save must
   survive in the final record (RACE_BASELINE_RED diagnostic on baseline).
"""
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.test_client_preference_service import prefs_app  # noqa: F401


def test_stale_steer_rejected_for_successor_run(tmp_path, monkeypatch):
    """A steer carrying expected_stream_id of a RETIRED run must be rejected.

    Invariant: (a) the response is not accepted (accepted:false or an explicit
    stale precondition rejection), (b) the successor run's agent callable is
    never invoked — no guidance is delivered to the live run.

    Baseline: the handler ignores the stale expected_stream_id precondition
    and delivers to the session's current (successor) stream — STEER_CONFIRMED
    in the eval evidence — so this test fails on the delivery invariant.
    """
    from api import config as cfg, streaming as st, helpers

    sid, successor, retired = 'eval-session', 'eval-successor', 'eval-retired'
    cfg.STATE_DIR = tmp_path
    session = SimpleNamespace(active_stream_id=successor)
    delivered = []
    agent = SimpleNamespace(session_id=sid, steer=lambda text: delivered.append(text))
    monkeypatch.setattr(st, 'get_session', lambda s: session)
    monkeypatch.setitem(cfg.STREAMS, successor, queue.Queue())
    monkeypatch.setitem(cfg.AGENT_INSTANCES, successor, agent)
    # Journal adapter isolates persistence while exercising the real acceptance
    # helper and the actual agent.steer callback, not a fake accepted response.
    class Writer:
        def __init__(self, *args):
            pass

        def accept_and_append_if_nonterminal(self, kind, payload, accept, publish):
            accept()
            event = {'event_id': 'eval-event', 'seq': 1, 'run_id': successor,
                     'payload': payload}
            publish(event)
            return True, event, None, None
    monkeypatch.setattr(st, 'RunJournalWriter', Writer)
    monkeypatch.setattr(helpers, 'j', lambda h, body, **kwargs: body)

    body = st._handle_chat_steer(None, {
        'session_id': sid,
        'text': 'guidance for retired run',
        'expected_stream_id': retired,
        'expected_request_id': 'eval-retired-request',
    })

    # ── PRODUCT INVARIANTS (not merely revision/echo fields) ──
    try:
        assert not body.get('accepted'), (
            'stale-precondition steer was accepted against the successor run')
        assert not delivered, (
            'successor agent received guidance from a steer addressed to a '
            'retired run: %r' % (delivered,))
    except AssertionError:
        print('STEER_BASELINE_RED', json.dumps({
            'response': body, 'actual_agent_received': delivered,
            'expected_stream_id': retired, 'successor_stream_id': successor}))
        raise


def test_preference_race_preserves_disjoint_edits(prefs_app, monkeypatch):
    """A WebUI edit of field A during a paused extension PATCH of field B survives.

    Invariant: after the deterministic two-lock interleaving (extension PATCH
    paused inside save_settings while a legacy WebUI save of a disjoint field
    lands), the final record contains BOTH values and the revision reflects
    both writes.

    Baseline: the extension's stale in-memory values overwrite the WebUI edit
    (RACE_CONFIRMED in the eval evidence), so field A is lost.
    """
    from api import client_preferences as cp
    cfg = prefs_app['config']
    _, headers = prefs_app['pair'](['preferences:read', 'preferences:write'])
    entered, release = threading.Event(), threading.Event()
    real_save = cfg.save_settings

    def paused_save(settings, source='webui'):
        if source == cfg._PATCH_SENTINEL_SOURCE:
            entered.set()
            assert release.wait(6)
        return real_save(settings, source)

    monkeypatch.setattr(cfg, 'save_settings', paused_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            prefs_app['call'], 'PATCH', '/api/client/preferences',
            {'authority': 'appearance', 'expected_revision': 1,
             'changes': {'theme': 'light'}}, headers)
        assert entered.wait(6)
        # Separate writer bypasses _PREFS_LOCK, as legacy WebUI POST does.
        real_save({'skin': 'github'})
        during = json.loads(
            (prefs_app['state'] / 'client-preferences.json').read_text())
        assert during['revisions']['appearance'] == 2
        assert cfg.load_settings()['skin'] == 'github'
        release.set()
        status, body = pending.result(timeout=8)
    current = prefs_app['call'](
        'GET', '/api/client/preferences?authority=appearance',
        headers=headers)[1]
    rec = current['authorities']['appearance']
    # ── PRODUCT INVARIANTS: both disjoint edits survive, both writes counted ──
    try:
        assert status == 200, 'extension PATCH failed: %r' % (body,)
        assert rec['values']['theme'] == 'light', (
            'extension edit of theme was lost')
        assert rec['values']['skin'] == 'github', (
            'WebUI edit of skin made during the paused extension PATCH was '
            'overwritten by the extension stale values; final values: %r' % (
                rec['values'],))
        assert rec['revision'] == 3, (
            'revision must reflect both writes (webui intermediate + '
            'extension patch), got %r' % (rec['revision'],))
    except AssertionError:
        print('RACE_BASELINE_RED', json.dumps({
            'patch_status': status, 'webui_intermediate_revision':
                during['revisions']['appearance'], 'final': rec}))
        raise
