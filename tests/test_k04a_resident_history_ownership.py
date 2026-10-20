"""K04a resident-history/ownership core (plan §10 K04a, interface freeze F4).

Invariants under test, all in static/messages.js `done`-settle adoption:

1. Ownership gate: a settled snapshot whose owner (session/stream, or a
   conflicting revision/generation when both sides carry one) differs from the
   resident transcript's owner must be REJECTED — the resident messages stay,
   and nothing from the foreign snapshot is adopted. A→B switch with pending
   hydration must not mutate B's transcript.
2. Narrower-tail retention: when an adopted snapshot begins at a LATER point of
   the already-loaded transcript (bounded recovery snapshot / server retention),
   the already-loaded prefix is retained ahead of the snapshot tail — stable
   identity/range/owner merge, never naive array replacement or equal-text
   heuristics.
3. No duplicates: merged output carries each stable identity exactly once.
4. Server-wins preserved: a snapshot unrelated to the loaded window (no
   identity overlap) still replaces, and a snapshot covering the whole window
   does not invent a prefix.

The tests are BEHAVIOURAL: they extract the real adoption helpers from
static/messages.js and execute them in Node (same proven pattern as
tests/test_issue6112_done_settle_snapshot_guard.py).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src: str, name: str, prefix: str = "function") -> str:
    marker = f"{prefix} {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found in static/messages.js"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} opening brace not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} braces unbalanced"
    return src[start:i]


def _harness(case: str) -> dict:
    assert NODE, "node is required for behavioural K04a tests"
    pieces = [
        _extract_function(MESSAGES_JS, "_messageIdentityKey"),
        _extract_function(MESSAGES_JS, "_ownerIdentityConflict"),
        _extract_function(MESSAGES_JS, "_mergeResidentPrefix"),
        _extract_function(MESSAGES_JS, "_adoptDoneSnapshotMessages"),
        _extract_function(MESSAGES_JS, "_finalAnswerIsInTranscript"),
        _extract_function(MESSAGES_JS, "_snapshotHasUsableFinalAssistantRow"),
        _extract_function(MESSAGES_JS, "_streamRecoveryControlMessageText"),
        _extract_function(MESSAGES_JS, "_streamRecoveryControlMessage"),
        _extract_function(MESSAGES_JS, "_isMarkerOnlyAssistantMessage"),
        "function msgContent(m){return (m && m.content)||'';}",
        "function _isPreservedCompressionTaskListMarkerOnlyText(t){return /active task list was preserved/i.test(String(t||''));}",
        textwrap.dedent(case),
    ]
    script = "\n".join(pieces)
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}"
    return json.loads(res.stdout.strip())


def _msg(role, ts, text):
    return {"role": role, "_ts": ts, "content": text}


CASES = {
    # 1. Ownership: wrong session rejects adoption entirely.
    "wrong_session_rejects": r'''
const pre=[{role:'user',_ts:'1',content:'Q1'},{role:'assistant',_ts:'2',content:'A1'}];
const foreign=[{role:'user',_ts:'9',content:'other-session'},{role:'assistant',_ts:'10',content:'other-answer'}];
const r=_adoptDoneSnapshotMessages(pre,foreign,'other-answer',{preOwner:'sessA:stream1',settledOwner:'sessB:stream1'});
console.log(JSON.stringify({adopted:r.adopted,kept:r.messages===pre||JSON.stringify(r.messages)===JSON.stringify(pre),rejected:r.rejected==='owner-mismatch'}));
''',
    # 2. Ownership: same session, conflicting stream rejects adoption.
    "wrong_stream_rejects": r'''
const pre=[{role:'user',_ts:'1',content:'Q1'}];
const foreign=[{role:'user',_ts:'9',content:'old-stream-replay'},{role:'assistant',_ts:'10',content:'old-stream-answer'}];
const r=_adoptDoneSnapshotMessages(pre,foreign,'old-stream-answer',{preOwner:'sessA:stream2',settledOwner:'sessA:stream1'});
console.log(JSON.stringify({adopted:r.adopted,kept:r.messages===pre||JSON.stringify(r.messages)===JSON.stringify(pre),rejected:r.rejected==='owner-mismatch'}));
''',
    # 3. Ownership: conflicting revision rejects when both sides carry one.
    "wrong_revision_rejects": r'''
const pre=[{role:'user',_ts:'1',content:'Q1'}];
const snapshot=[{role:'user',_ts:'1',content:'Q1'},{role:'assistant',_ts:'2',content:'stale'}];
const r=_adoptDoneSnapshotMessages(pre,snapshot,'stale',{preOwner:{sessionId:'s',streamId:'x',revision:'7'},settledOwner:{sessionId:'s',streamId:'x',revision:'6'}});
console.log(JSON.stringify({adopted:r.adopted,kept:r.messages===pre||JSON.stringify(r.messages)===JSON.stringify(pre),rejected:r.rejected==='owner-mismatch'}));
''',
    # 4. Ownership: lenient when a side omits components (journal replay).
    "lenient_when_stream_absent": r'''
const pre=[{role:'user',_ts:'1',content:'Q1'}];
const snapshot=[{role:'user',_ts:'1',content:'Q1'},{role:'assistant',_ts:'2',content:'A'}];
const r=_adoptDoneSnapshotMessages(pre,snapshot,'A',{preOwner:'sessA:',settledOwner:'sessA:'});
console.log(JSON.stringify({adopted:r.adopted,sameSession:r.messages.length===2}));
''',
    # 5. Narrower tail: loaded prefix survives a bounded recovery snapshot.
    "narrower_tail_retains_prefix": r'''
const pre=[
  {role:'user',_ts:'1',content:'old question 1'},
  {role:'assistant',_ts:'2',content:'old answer 1'},
  {role:'user',_ts:'3',content:'old question 2'},
  {role:'assistant',_ts:'4',content:'old answer 2'},
  {role:'user',_ts:'5',content:'new question'},
  {role:'assistant',_ts:'6',content:'The streamed final answer.'}
];
const settled=[
  {role:'user',_ts:'3',content:'old question 2'},
  {role:'assistant',_ts:'4',content:'old answer 2'},
  {role:'user',_ts:'5',content:'new question'},
  {role:'assistant',_ts:'6',content:'The streamed final answer.'}
];
const r=_adoptDoneSnapshotMessages(pre,settled,'The streamed final answer.');
const texts=r.messages.map(m=>m.content);
console.log(JSON.stringify({
  keepsOldQ1:texts.includes('old question 1'),
  keepsOldA1:texts.includes('old answer 1'),
  tailPresent:texts.includes('The streamed final answer.'),
  noDuplicates:(function(){const seen=new Set();for(const m of r.messages){const k=m.role+'|'+m._ts;if(seen.has(k))return false;seen.add(k);}return true;})(),
  count:r.messages.length
}));
''',
    # 6. Snapshot unrelated to the loaded window: server-wins preserved.
    "unrelated_snapshot_server_wins": r'''
const pre=[{role:'user',_ts:'1',content:'loaded row'},{role:'assistant',_ts:'2',content:'loaded answer'}];
const settled=[{role:'user',_ts:'50',content:'brand new turn'},{role:'assistant',_ts:'51',content:'The final answer.'}];
const r=_adoptDoneSnapshotMessages(pre,settled,'The final answer.');
console.log(JSON.stringify({adopted:r.adopted,isSettled:JSON.stringify(r.messages)===JSON.stringify(settled)}));
''',
    # 7. Snapshot covering the whole window: no invented prefix, no duplicates.
    "covering_snapshot_no_change": r'''
const pre=[
  {role:'user',_ts:'1',content:'q'},
  {role:'assistant',_ts:'2',content:'The streamed final answer.'}
];
const settled=[
  {role:'user',_ts:'1',content:'q'},
  {role:'assistant',_ts:'2',content:'The streamed final answer.'}
];
const r=_adoptDoneSnapshotMessages(pre,settled,'The streamed final answer.');
console.log(JSON.stringify({count:r.messages.length,identical:JSON.stringify(r.messages)===JSON.stringify(settled)}));
''',
}

EXPECTED = {
    "wrong_session_rejects": {"adopted": False, "kept": True, "rejected": True},
    "wrong_stream_rejects": {"adopted": False, "kept": True, "rejected": True},
    "wrong_revision_rejects": {"adopted": False, "kept": True, "rejected": True},
    "lenient_when_stream_absent": {"adopted": True, "sameSession": True},
    "narrower_tail_retains_prefix": {
        "keepsOldQ1": True,
        "keepsOldA1": True,
        "tailPresent": True,
        "noDuplicates": True,
        "count": 6,
    },
    "unrelated_snapshot_server_wins": {"adopted": True, "isSettled": True},
    "covering_snapshot_no_change": {"count": 2, "identical": True},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_k04a_adoption_invariant(case):
    got = _harness(CASES[case])
    assert got == EXPECTED[case], f"K04a invariant {case} failed: {got} != {EXPECTED[case]}"
