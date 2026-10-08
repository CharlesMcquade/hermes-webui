"""Round-2 re-gate regression tests for #7882 (2026-10-07 review head ff0aa873).

Four findings, each reproduced by the gate on the rebase-with-no-code-change
head. The four fixes:

1. Fork regeneration after an async delegation was rejected at the row-source
   guard BEFORE the fork ownership proof was consulted (CORE, 403).
2. Id-less display rows missed the compression content-match exception, so
   retry/undo removed the exchange from display but left it in saved model
   context (CORE).
3. A local send promoted the raw count into the visible count — a hidden
   wakeup plus one send reported visible 5 for raw 4 / visible 3 (SILENT).
4. Sidebar state.db growth credited the hidden pending wakeup row as visible
   (SILENT): raw 4 / visible 3 with a pending wakeup reported 4.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from api.process_event_utils import is_hidden_transcript_row

_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _run_node_vm(source: str) -> str:
    if NODE_BIN is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


# ── Finding 1: fork regeneration after async delegation ───────────────────


def _fork_gate(row, *, parent="parent-123", sid="fork-child-7882"):
    from api.session_ops import _selected_regeneration_turn_owned

    session = type(
        "_ForkSession",
        (),
        {
            "read_only": False,
            "session_source": "fork",
            "is_cli_session": False,
            "raw_source": None,
            "source_tag": None,
            "parent_session_id": parent,
            "session_id": sid,
        },
    )()
    return _selected_regeneration_turn_owned(session, row)


def test_fork_gate_accepts_delegation_wakeup_with_matching_child_proof():
    """The re-gate's exact rejected shape: a settled wakeup row keeps
    ``_source: delegation_wakeup`` and carries ``_fork_child_turn`` pointing
    at this fork session. The ownership proof — parent session set and the
    child turn matching the session id — must be the authorization, not the
    row source. This calls the REAL gate (the round-1 test asserted only the
    stamp)."""
    row = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
        "_fork_child_turn": "fork-child-7882",
    }
    assert _fork_gate(row) is True


def test_fork_gate_rejects_wakeup_without_ownership_proof():
    """The wakeup-source exception carries NO authorization by itself: a row
    claiming the wakeup source without a matching ``_fork_child_turn`` stays
    rejected (missing ownership)."""
    no_proof = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
    }
    assert _fork_gate(no_proof) is False


def test_fork_gate_rejects_wakeup_with_foreign_child_turn():
    foreign = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
        "_fork_child_turn": "some-other-session",
    }
    assert _fork_gate(foreign) is False


def test_fork_gate_rejects_wakeup_without_parent_session():
    orphan = {
        "role": "user",
        "content": "[ASYNC DELEGATION COMPLETE d1] internal handoff",
        "_source": "delegation_wakeup",
        "_fork_child_turn": "fork-child-7882",
    }
    assert _fork_gate(orphan, parent=None) is False


def test_fork_gate_still_rejects_unallowed_row_sources():
    """Non-wakeup, non-allowed row sources are unchanged: the exception is
    scoped to ``delegation_wakeup`` only."""
    cli = {"role": "user", "content": "hello", "_source": "cli"}
    assert _fork_gate(cli) is False


# ── Finding 2: id-less display rows match sanitized context copies ────────


@pytest.mark.parametrize("op", ["retry", "undo"])
def test_retry_undo_id_less_display_row_cuts_id_less_context_copy(
    monkeypatch, tmp_path, op
):
    """Round-2 must-fix: the selected display row has NO id (production
    settlement + compression sanitizing strips it), and the context copy is
    id-less too. The content match must still cut before it — the old
    ``row_id is None and target_id is not None`` condition required the
    TARGET to carry an id, so the removed question and answer survived in
    saved model context."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id=f"{op}idless7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "old question", "timestamp": 1000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1001.0},
            # Selected display row: id-less, carries its ORIGINAL timestamp —
            # production settlement stamps wall-clock times on eager rows and
            # the compression writeback re-stamps only the CONTEXT copies.
            {"role": "user", "content": "real question", "timestamp": 1002.0},
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d2] internal handoff",
                "timestamp": 1781024055.0,
                "_source": "delegation_wakeup",
            },
            {"role": "assistant", "content": "child result summary"},
        ],
        # Manual-compression context: sanitized id-less copies with FRESH
        # re-stamped timestamps that disagree with the display rows'.
        context_messages=[
            {"role": "user", "content": "old question", "timestamp": 1781024000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1781024001.0},
            {"role": "user", "content": "real question", "timestamp": 1781024002.0},
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d2] internal handoff",
                "timestamp": 1781024003.0,
                "_source": "delegation_wakeup",
            },
            {"role": "assistant", "content": "child result summary", "timestamp": 1781024004.0},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    getattr(session_ops, f"{op}_last")(session.session_id)

    # The content match cuts BEFORE the selected id-less turn: the removed
    # question/answer (and the hidden handoff + reply after it) must NOT
    # survive in model context.
    assert [m["content"] for m in session.context_messages] == [
        "old question",
        "old answer",
    ]
    assert saved


@pytest.mark.parametrize("op", ["retry", "undo"])
def test_retry_undo_different_id_still_vetoes_same_text(monkeypatch, tmp_path, op):
    """The different-ID veto is preserved: when BOTH rows carry ids and they
    differ, the same-text later row must not capture the cut (greptile P1,
    unchanged by the round-2 fix)."""
    import contextlib

    import api.session_ops as session_ops
    from api.models import Session

    session = Session(
        session_id=f"{op}veto7882",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "same text", "id": "msg-display-1"},
            {
                "role": "user",
                "content": "[ASYNC DELEGATION COMPLETE d3] handoff",
                "timestamp": 1781024056.0,
                "_source": "delegation_wakeup",
            },
            {"role": "assistant", "content": "child result summary"},
        ],
        context_messages=[
            {"role": "user", "content": "same text", "id": "msg-display-1",
             "timestamp": 1781024000.0},
            {"role": "user", "content": "same text", "id": "msg-later-2",
             "timestamp": 1781024005.0},
            {"role": "assistant", "content": "child result summary"},
        ],
    )
    saved = []
    session.save = lambda *args, **kwargs: saved.append(True)
    monkeypatch.setattr(session_ops, "get_session", lambda sid: session)
    monkeypatch.setattr(session_ops, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(
        session_ops, "_get_session_agent_lock", lambda sid: contextlib.nullcontext()
    )

    getattr(session_ops, f"{op}_last")(session.session_id)

    # The id mismatch vetoes the later same-text row, so the cut lands on the
    # TRUE selected turn (matched by id) — everything after it is removed.
    assert session.context_messages == []
    assert saved


# ── Finding 3: local-send visible promotion excludes the raw count ────────


@_node_tests
def test_local_send_visible_promotion_excludes_raw_count():
    """The re-gate's Chromium reproduction in the Node VM: a session with a
    hidden wakeup row — server says raw 4 / visible 3 — plus one local send.
    S.messages holds the 3 visible rows + 1 hidden wakeup + the optimistic
    send. The visible count must report 4 (3 + the send), never 5 (the raw
    count), across repeated updater calls (send() calls it twice)."""
    start = SESSIONS_SRC.index("function upsertActiveSessionForLocalTurn")
    end = SESSIONS_SRC.index("function _sessionRowsWithActiveEphemeralSession", start)
    body = SESSIONS_SRC[start:end]
    source = (
        "const SESSIONS_JS = " + repr(SESSIONS_SRC) + ";\n"
        + r"""
function extractFunc(name) {
  const start = SESSIONS_JS.indexOf('function ' + name + '(');
  if (start < 0) throw Error(name + ' missing');
  let i = SESSIONS_JS.indexOf('{', start) + 1, depth = 1;
  while (depth && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const S = {
  session: {
    session_id: 'sid-7882',
    title: 'Test chat',
    message_count: 4,             // server raw total: 3 visible + 1 hidden
    visible_message_count: 3,     // server visible total
  },
  messages: [
    {role: 'user', content: 'q1'},
    {role: 'assistant', content: 'a1'},
    {role: 'user', content: 'q2'},
    {role: 'user', content: '[ASYNC DELEGATION COMPLETE d1] handoff',
     _source: 'delegation_wakeup'},
    // The optimistic send row is added by send() BEFORE the updater runs.
    {role: 'user', content: 'my new question'},
  ],
  activeProfile: 'default',
};
const _allSessions = [];
const t = (k) => k;
function renderSessionListFromCache() {}
function closeSessionActionMenu() {}
const document = {createElement: () => ({style: {}, dataset: {}})};

"""
        + body
        + r"""

// Two updater calls in one send (optimistic + provisional-title pass).
upsertActiveSessionForLocalTurn({messageCount: 5});
upsertActiveSessionForLocalTurn({messageCount: 5});

console.log(JSON.stringify({
  raw: S.session.message_count,
  visible: S.session.visible_message_count,
}));
"""
    )
    result = json.loads(_run_node_vm(source))
    assert result["raw"] == 5, f"Raw count follows the transcript, got {result}"
    assert result["visible"] == 4, (
        "Visible count must promote only the local-transcript authority "
        f"(3 visible + 1 send = 4), never the raw count (5), got {result}"
    )


# ── Finding 4: overlay credits the hidden pending wakeup ──────────────────


def test_overlay_pending_wakeup_excluded_from_visible_growth():
    """The re-gate's SQLite/HTTP reproduction: sidecar says raw 4 / visible 3
    is WRONG — the real shape is visible 3 provenance-stamped rows, then the
    pending wakeup turn appends one PLAIN user row to state.db (raw 4). The
    overlay must credit the delta minus the pending hidden user row:
    visible 3 + (4 - 2 - 1) = 4... no: sidecar raw 3 / visible 3, state.db
    raw 4 with a pending wakeup → visible 3 + (4-3-1) = 3."""
    from api.models import _apply_sidebar_state_db_override_metadata

    sessions = [
        {
            "session_id": "growth7882b",
            "message_count": 3,
            "visible_message_count": 3,
            # Provenance: the pending turn is a hidden wakeup.
            "pending_user_source": "delegation_wakeup",
            "has_pending_user_message": True,
            "last_message_at": 1781024000.0,
            "updated_at": 1781024000.0,
        }
    ]
    metadata = {
        "growth7882b": {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,  # +1: the plain (unstamped) wakeup row
            "_state_db_last_message_at": 1781024010.0,
        }
    }
    _apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    # Raw 4 = 3 provenance-stamped + 1 pending hidden user row. The overlay
    # must NOT report 4 visible: the wakeup row is hidden.
    assert sessions[0]["visible_message_count"] == 3


def test_overlay_pending_visible_turn_still_bumps_visible_count():
    """A pending NON-hidden turn (webui/fork) keeps the monotone bump: its
    state.db row is a real visible user row."""
    from api.models import _apply_sidebar_state_db_override_metadata

    sessions = [
        {
            "session_id": "growth7882c",
            "message_count": 3,
            "visible_message_count": 3,
            "pending_user_source": "webui",
            "has_pending_user_message": True,
            "last_message_at": 1781024000.0,
            "updated_at": 1781024000.0,
        }
    ]
    metadata = {
        "growth7882c": {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,
            "_state_db_last_message_at": 1781024010.0,
        }
    }
    _apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    assert sessions[0]["visible_message_count"] == 4


def test_overlay_no_pending_row_keeps_monotone_bump():
    """Without a pending turn, the growth is ordinary settled rows — the
    round-1 behaviour is unchanged (visible rides the raw delta)."""
    from api.models import _apply_sidebar_state_db_override_metadata

    sessions = [
        {
            "session_id": "growth7882",
            "message_count": 2,
            "visible_message_count": 2,
            "last_message_at": 1781024000.0,
            "updated_at": 1781024000.0,
        }
    ]
    metadata = {
        "growth7882": {
            "_state_db_source": "webui",
            "_state_db_message_count": 4,
            "_state_db_last_message_at": 1781024010.0,
        }
    }
    _apply_sidebar_state_db_override_metadata(sessions, metadata)

    assert sessions[0]["message_count"] == 4
    assert sessions[0]["visible_message_count"] == 4


def test_compact_emits_pending_user_source_only_when_set(tmp_path):
    """compact() carries the pending provenance the overlay reads — but only
    when set (ordinary webui/fork rows keep the old shape)."""
    from api.models import Session

    s = Session(
        session_id="compact7882a",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
        pending_user_message="[ASYNC DELEGATION COMPLETE d4] handoff",
        pending_user_source="delegation_wakeup",
    )
    compact = s.compact(include_runtime=True)
    assert compact.get("pending_user_source") == "delegation_wakeup"

    s2 = Session(
        session_id="compact7882b",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
        pending_user_message="hello",
        pending_user_source="webui",
    )
    compact2 = s2.compact(include_runtime=True)
    assert "pending_user_source" not in compact2


def test_hidden_predicate_unchanged():
    """Guard: the hidden-row predicate stays keyed on the typed stamp."""
    assert is_hidden_transcript_row({"_source": "delegation_wakeup"})
    assert not is_hidden_transcript_row({"_source": "webui"})
    assert not is_hidden_transcript_row({})
