"""The Agent's workspace-prefixed + todo-injected copy of a wakeup prompt must
still merge into the stamped durable row (#delegation-wakeup-todo-injection).

When a delegation_wakeup / process_wakeup turn crosses a context-compression
boundary, the Agent appends the synthetic task-list injection
(``[Your active task list was preserved across context compression] …``) to
its copy of the turn input. The Gateway's durable wake row holds the clean
prompt, so the two rows' texts differ by that suffix: the display-merge
dedupe did not recognize the Agent copy as the current turn and appended it
raw — a visible duplicate user bubble that rendered the injection verbatim.

Reproduction (session 60a3104a4258, deleg_27605a7b): two user rows at the
identical microsecond timestamp — one stamped ``_source: delegation_wakeup``
(correctly hidden) and one flagless copy of ``[Workspace::v1: …] + prompt +
task-list injection``.
"""

from api import streaming

PROMPT = "[ASYNC DELEGATION BATCH COMPLETE — deleg_27605a7b]\nNo remaining blockers in the two staging fixes."

TODO_SUFFIX = (
    "\n\n[Your active task list was preserved across context compression]\n"
    "- [>] production-host. Stage blockers from deleg_9764d6b3 fixed; 2 red->green regressions.\n"
    "- [ ] production-permissions. Validate final fixed-path identity. (pending)"
)


def _identity(token="stream-one:1790562913.667752", idx=0):
    return {
        "session_id": "wakeup-todo",
        "token": token,
        "text": PROMPT,
        "timestamp": 1790562913.667752,
        "source": "delegation_wakeup",
        "attachments": [],
        "checkpoint": None,  # deferred session-save mode: no checkpoint dict
        "current_turn_user_idx": idx,
        "agent_turn_boundary_resolved": True,
        "turn_id": "turn-1",
    }


def _durable_wake_row():
    """The Gateway-persisted clean prompt row (stamped by the prior fix)."""
    return {
        "role": "user",
        "content": PROMPT,
        "timestamp": 1790562913.667752,
        "_source": "delegation_wakeup",
        "_active_turn_token": "stream-one:1790562913.667752",
        "_row_id": 1,
        "_db_persisted": True,
        "id": 2292,
    }


def _agent_prefixed_todo_row():
    """The Agent's copy: workspace prefix + prompt + todo injection, no flags."""
    return {
        "role": "user",
        "content": f"[Workspace::v1: /Users/charles/hermes-webui]\n{PROMPT}{TODO_SUFFIX}",
        "timestamp": 1790562913.667752,
    }


def test_prefixed_todo_copy_merges_into_stamped_durable_row():
    identity = _identity()
    agent_rows = [
        _agent_prefixed_todo_row(),
        {"role": "assistant", "content": "Done.", "timestamp": 1790562914.0},
    ]
    merged = streaming._merge_display_messages_after_agent_result(
        [_durable_wake_row()],
        [],
        agent_rows,
        identity["text"],
        source="delegation_wakeup",
        verification_nudge_provenance={"active_turn_identity": identity},
    )
    user_rows = [m for m in merged if m.get("role") == "user"]
    assert len(user_rows) == 1, f"expected one user row, got {len(user_rows)}"
    assert user_rows[0]["_source"] == "delegation_wakeup"
    assert TODO_SUFFIX not in str(user_rows[0].get("content"))


def test_settlement_stamps_prefixed_todo_agent_row():
    identity = _identity()
    settled = streaming._settle_current_turn_boundary(
        [], [_agent_prefixed_todo_row()], identity, identity["text"], "delegation_wakeup",
    )
    assert settled[0]["_source"] == "delegation_wakeup"


def test_real_user_message_with_similar_text_stays_visible():
    """A genuine user message that merely mentions the injection is NOT reclassified."""
    # _looks_like_current_user_turn directly: prose after the injection header
    # must never match, with or without the wakeup flag.
    identity = _identity()
    prose_row = {
        "role": "user",
        "content": f"{PROMPT}\n\nBy the way, what does "
                   "'[Your active task list was preserved across context compression]' mean?",
        "timestamp": 1790562999.0,
    }
    assert streaming._looks_like_current_user_turn(prose_row, identity["text"]) is False
    assert not streaming._looks_like_current_user_turn(
        prose_row, identity["text"], allow_wakeup_todo_tail=True
    )
    # A prompt-prefixed user message that continues with normal prose also
    # stays unmatched even under the wakeup flag.
    prose_row2 = {
        "role": "user",
        "content": f"{PROMPT}\n\nAlso, please review the staging notes when you can.",
        "timestamp": 1790562999.0,
    }
    assert not streaming._looks_like_current_user_turn(
        prose_row2, identity["text"], allow_wakeup_todo_tail=True
    )
    # And the genuine wakeup-tail shape still matches under the flag.
    tail_row = _agent_prefixed_todo_row()
    assert streaming._looks_like_current_user_turn(
        tail_row, identity["text"], allow_wakeup_todo_tail=True
    )
