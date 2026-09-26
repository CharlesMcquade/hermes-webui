"""Late-child triage is ledger-owned and must not ACK an ordinary delivery."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from api import background_process as bp, delegation_triage as triage, streaming


@pytest.fixture
def ledger(monkeypatch):
    state = {"disposition": "triage", "settled": [], "admitted": [], "result": {"summary": "done"}}
    module = types.ModuleType("tools.async_delegation")
    module.late_result_disposition = lambda owner, ident: state["disposition"]
    module.admit_late_result = lambda owner, ident: state["admitted"].append((owner, ident)) or "token"
    def settle(owner, ident, token, decision):
        state["settled"].append((owner, ident, token, decision))
        state["disposition"] = "settled"
        return True
    module.settle_late_result = settle
    module.get_durable_delegation = lambda ident: {"result": state["result"]}
    monkeypatch.setitem(sys.modules, "tools.async_delegation", module)
    return state


@pytest.fixture
def parent(monkeypatch):
    from api import config, models, profiles, routes
    session = SimpleNamespace(session_id="parent", profile=None, messages=[{"role": "user", "content": "Build a report"}, {"role": "assistant", "content": "final"}], active_stream_id=None,
                              pending_user_message=None, pending_user_source=None, updated_at=1)
    agent = SimpleNamespace(session_id="parent", _webui_profile_home="/tmp/profile")
    monkeypatch.setattr(models, "get_session", lambda sid: session)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda profile: "/tmp/profile")
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda config: False)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: False)
    monkeypatch.setattr(config, "_get_session_agent_lock", lambda sid: threading.Lock())
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {"parent": (agent, "sig")})
    return session, agent


@pytest.mark.parametrize("verdict,expected", [("no_change", "suppress"), ("needs_parent", "wake"), ("uncertain", "wake")])
def test_reviewer_disposition(ledger, parent, monkeypatch, verdict, expected):
    monkeypatch.setattr(triage, "_review", lambda agent, history, goal, report: verdict)
    assert triage.assess_late_result("parent", "child") == ("suppressed" if expected == "suppress" else "wake")
    assert ledger["settled"] == [("parent", "child", "token", expected)]


def test_parent_turn_changes_revision_during_review(ledger, parent, monkeypatch):
    session, _ = parent
    def review(agent, history, report):
        session.messages.append({"role": "user", "content": "new work"})
        return "no_change"
    monkeypatch.setattr(triage, "_review", lambda agent, history, goal, report: review(agent, history, report))
    assert triage.assess_late_result("parent", "child") == "wake"
    assert ledger["settled"][-1][-1] == "wake"


def test_missing_reviewer_and_gateway_owner_wake(ledger, parent, monkeypatch):
    from api import routes
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda config: True)
    assert triage.assess_late_result("parent", "child") == "wake"
    assert ledger["settled"][-1][-1] == "wake"


def test_invalid_profile_cannot_fall_back_to_default_ledger(ledger, parent, monkeypatch):
    session, _ = parent
    session.profile = "../other"
    assert triage.assess_late_result("parent", "child") == "retry"
    assert ledger["settled"] == []


def test_reviewer_failure_and_wrong_profile_wake(ledger, parent, monkeypatch):
    session, agent = parent
    def fail_review(*_args):
        raise TimeoutError("private reviewer exceeded budget")
    monkeypatch.setattr(triage, "_review", fail_review)
    assert triage.assess_late_result("parent", "child") == "wake"
    assert ledger["settled"][-1][-1] == "wake"
    agent._webui_profile_home = "/wrong/profile"
    ledger["disposition"] = "triage"
    assert triage.assess_late_result("parent", "child") == "wake"
    assert ledger["settled"][-1][-1] == "wake"


def test_suppression_never_takes_ordinary_claim_or_emits(ledger, parent, monkeypatch):
    monkeypatch.setattr(triage, "assess_late_result", lambda sid, ident: "suppressed")
    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *args: pytest.fail("ordinary claim"))
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", lambda *args, **kwargs: pytest.fail("wakeup"))
    bp._process_async_delegation_event({"type": "async_delegation", "delegation_id": "child"}, session_id="parent", delegation_id="child", process_registry=None)


def test_next_turn_does_not_ack_stale_enrolled_child(ledger, parent, monkeypatch):
    registry = SimpleNamespace(completion_queue=queue.Queue(), get=lambda ident: None)
    evt = {"type": "async_delegation", "delegation_id": "child", "origin_ui_session_id": "parent", "completed_at": time.time() - 100000}
    registry.completion_queue.put(evt)
    module = types.ModuleType("tools.process_registry")
    module.process_registry = registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", module)
    monkeypatch.setattr(streaming, "claim_async_delegation_delivery", lambda *args: pytest.fail("ordinary claim"))
    monkeypatch.setattr(streaming, "schedule_async_delegation_claim_retry", lambda *args: True)
    assert streaming._drain_webui_process_notifications("parent") == []


def test_claimed_lease_retries_without_ordinary_claim(ledger, parent, monkeypatch):
    ledger["disposition"] = "claimed"
    calls = []
    monkeypatch.setattr(bp, "_retry_unclaimed_async_delegation_event", lambda *args: calls.append(True))
    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *args: pytest.fail("ordinary claim"))
    bp._process_async_delegation_event({"type": "async_delegation"}, session_id="parent", delegation_id="child", process_registry=None)
    assert calls


def test_feature_agent_ledger_restart_and_owner(tmp_path):
    """Opt-in integration: load the specified Agent checkout in an isolated child."""
    source = os.environ.get("HERMES_AGENT_TRIAGE_SOURCE")
    if not source or not (Path(source) / "tools" / "async_delegation.py").is_file():
        pytest.skip("set HERMES_AGENT_TRIAGE_SOURCE to the Agent feature checkout")
    script = '''
from tools import async_delegation as ledger
import queue, time
with ledger._DB_LOCK, ledger._transaction() as db:
    now = time.time() - 60 * 3600
    db.execute("""INSERT INTO async_delegations
        (delegation_id, origin_session, parent_session_id, state, dispatched_at,
         completed_at, updated_at, event_json, result_json)
        VALUES ('child', 'route', 'parent', 'completed', ?, ?, ?, ?, ?)""",
        (now, now, now, '{"type":"async_delegation","delegation_id":"child"}', '{"summary":"retained"}'))
assert ledger.finalize_parent_delegations('wrong', ['child']) == []
assert ledger.finalize_parent_delegations('parent', ['child']) == ['child']
assert ledger.late_result_disposition('wrong', 'child') == 'wake'
token = ledger.admit_late_result('parent', 'child')
assert token and not ledger.claim_completion_delivery('child', 'ordinary')
with ledger._DB_LOCK, ledger._transaction() as db:
    db.execute("UPDATE async_delegation_triage SET triage_claimed_at=0 WHERE delegation_id='child'")
    db.execute("UPDATE async_delegations SET delivery_claimed_at=0 WHERE delegation_id='child'")
replacement = ledger.admit_late_result('parent', 'child')
assert replacement and not ledger.settle_late_result('parent', 'child', token, 'suppress')
assert ledger.settle_late_result('parent', 'child', replacement, 'suppress')
assert ledger.get_durable_delegation('child')['result']['summary'] == 'retained'
assert ledger.restore_undelivered_completions(queue.Queue()) == 0
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path), PYTHONPATH=source)
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
