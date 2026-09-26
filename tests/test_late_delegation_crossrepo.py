"""Feature-worktree Agent + WebUI integration, with no live state or provider.

Opt in with HERMES_AGENT_TRIAGE_SOURCE=/path/to/hermes-agent-feature. A child
interpreter imports BOTH actual checkouts while a deterministic fork stands in
only for the remote model response. Ledger admission, routing, and settlement
are real SQLite operations in a disposable HERMES_HOME.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest


@pytest.mark.parametrize("verdict,expected", [
    ("no_change", "suppressed"),
    ("needs_parent", "delivered"),
    ("uncertain", "delivered"),
])
def test_real_ledger_review_and_webui_routing(tmp_path, verdict, expected):
    agent_source = os.environ.get("HERMES_AGENT_TRIAGE_SOURCE")
    if not agent_source or not (Path(agent_source) / "agent" / "late_delegation_review.py").is_file():
        pytest.skip("set HERMES_AGENT_TRIAGE_SOURCE to the Agent feature checkout")
    script = r'''
import json
import os
import queue
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

feature_home = os.environ["HERMES_HOME"]
from tools import async_delegation as ledger
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
assert str(get_hermes_home().resolve()) == os.path.realpath(os.environ["HERMES_HOME"]), (get_hermes_home(), os.environ["HERMES_HOME"])
from api import background_process as bp, config, models, profiles, routes
from agent import late_delegation_review as reviewer

verdict, expected = sys.argv[1:]
ident = "deleg_integration"
now = time.time()
evt = {
    "type": "async_delegation", "delegation_id": ident,
    "origin_ui_session_id": "parent", "parent_session_id": "parent",
    "session_key": "webui-parent", "goal": "Check the report",
    "status": "completed", "summary": "Child checked the report",
    "dispatched_at": now - 1, "completed_at": now,
}
token = set_hermes_home_override(feature_home)
try:
    with ledger._DB_LOCK, ledger._transaction() as conn:
        assert not conn.execute("SELECT delegation_id FROM async_delegations WHERE delegation_id=?", (ident,)).fetchone(), (ledger._db_path(), feature_home, os.environ["HERMES_HOME"])
        conn.execute("""INSERT INTO async_delegations
            (delegation_id, origin_session, origin_ui_session_id, parent_session_id,
             state, dispatched_at, completed_at, updated_at, event_json, result_json)
            VALUES (?, 'webui-parent', 'parent', 'parent', 'completed', ?, ?, ?, ?, ?)""",
            (ident, now-1, now, now, json.dumps(evt), json.dumps({
                "status": "completed", "summary": "Child checked the report"
            })))
    assert ledger.finalize_parent_delegations("other", [ident]) == []
    assert ledger.finalize_parent_delegations("parent", [ident]) == [ident]
finally:
    reset_hermes_home_override(token)
lock = threading.Lock()
session = SimpleNamespace(session_id="parent", profile=None, messages=[
    {"role": "user", "content": "Check the report"},
    {"role": "assistant", "content": "Done checking the report."},
], active_stream_id=None, pending_user_message=None,
   pending_user_source=None, updated_at=now)
profile_home = feature_home
agent = SimpleNamespace(session_id="parent", _webui_profile_home=profile_home)
class Fork:
    def run_conversation(self, *, user_message, conversation_history):
        from hermes_cli.plugins import _thread_tool_whitelist
        assert getattr(_thread_tool_whitelist, "allowed", None) == set()
        assert str(get_hermes_home().resolve()) == profile_home
        assert "Check the report" in user_message
        assert "Child checked the report" in user_message
        assert conversation_history[-1]["content"] == "Done checking the report."
        return {"completed": True, "final_response": json.dumps({"decision": verdict})}
    def release_clients(self):
        pass
cache = {"parent": (agent, "sig")}
registry = SimpleNamespace(completion_queue=queue.Queue())
started = []
emitted = []
def accept(sid, prompt, *, delegation_id, evt, claim, process_registry):
    started.append((sid, prompt, delegation_id))
    bp._record_async_delegation_accepted(evt, session_id=sid, claim=claim)
with patch.object(config, "_get_session_agent_lock", lambda sid: lock), \
     patch.object(config, "SESSION_AGENT_CACHE", cache), \
     patch.object(models, "get_session", lambda sid: session), \
     patch.object(profiles, "get_hermes_home_for_profile", lambda profile: profile_home), \
     patch.object(routes, "webui_gateway_chat_enabled", lambda cfg: False), \
     patch.object(routes, "get_config", lambda: {}), \
     patch.object(bp, "_session_has_active_turn", lambda sid: False), \
     patch.object(bp, "_start_async_delegation_wakeup_turn", accept), \
     patch.object(bp, "_emit_bg_task_complete_events_coalesced", lambda sid, payload: emitted.append(payload)), \
     patch.object(reviewer, "build_cache_parity_fork", lambda *a, **kw: (Fork(), {}, False)):
    bp._process_async_delegation_event(evt, session_id="parent", delegation_id=ident,
                                       process_registry=registry)
token = set_hermes_home_override(profile_home)
try:
    record = ledger.get_durable_delegation(ident)
    assert record["delivery_state"] == expected, (record, started)
    assert record["result"]["summary"] == "Child checked the report"
    assert ledger.restore_undelivered_completions(queue.Queue()) == 0
finally:
    reset_hermes_home_override(token)
if expected == "suppressed":
    assert not started and not emitted
else:
    assert len(started) == 1 and len(emitted) == 1
    assert started[0][0] == "parent" and started[0][2] == ident
    assert "Child checked the report" in started[0][1]
'''
    env = dict(os.environ)
    env.pop("PYTHONSAFEPATH", None)
    env["HERMES_HOME"] = str(tmp_path / f"agent-home-{uuid.uuid4().hex}")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path / f"webui-state-{uuid.uuid4().hex}")
    env["PYTHONPATH"] = os.pathsep.join((str(agent_source), str(Path(__file__).resolve().parents[1])))
    completed = subprocess.run([sys.executable, "-c", script, verdict, expected],
                               cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
