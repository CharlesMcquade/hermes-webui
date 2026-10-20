"""K04c disclosure ownership — settled worklog keys use stable turn/stream identity.

Plan §10 K04c / interface freeze F4 companion: disclosure and row-recovery for
settled anchor-scene worklog groups must be addressed by stable turn/stream
identity (`settled-stream:<streamId>`), not raw-index-only
`anchor-scene:<rawIdx>`. A raw index into S.messages silently changes meaning
after a Load-earlier history prepend or rebuild, so deferred-row recovery
(#5839) and the just-settled collapse pass (#6414) can resolve the WRONG turn
(cross-turn leakage) or nothing at all.

Invariants (extracted from static/ui.js, executed in Node):

1. A group keyed `settled-stream:run-2` resolves its owning message by
   `_anchor_stream_id` even after a history prepend has shifted every raw
   index (the stale `anchor-scene:0` no longer points at that turn).
2. The raw-index `anchor-scene:<rawIdx>` key still resolves (fallback for
   identity-less groups).
3. A `settled-stream` group whose stream id matches no message falls back to
   its raw-index worklog key, then null.
4. Distinct streams never cross-resolve (no cross-turn leakage).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found in static/ui.js"
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
    assert NODE, "node is required for behavioural K04c tests"
    pieces = [
        _extract_function(UI_JS, "_messageForWorklogGroup"),
        _extract_function(UI_JS, "_deferredWorklogRowsFromGroup"),
        "let S={messages:[]};",
        "let _anchorSceneRowsForRendering=(scene,opts)=>({from:scene.stream_id,rows:scene.rows});",
        textwrap.dedent(case),
    ]
    script = "\n".join(pieces)
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}"
    return json.loads(res.stdout.strip())


# Transcript AFTER a Load-earlier prepend: three prepended rows shifted the
# two activity-scene turns from rawIdx 0/1 to 3/4. The fixture group still
# carries the STALE worklog key `anchor-scene:0` but a stable disclosure key
# `settled-stream:run-2`.
FIXTURE = r'''
S.messages=[
  {role:'user',content:'older-1'},
  {role:'assistant',content:'older-2'},
  {role:'user',content:'older-3'},
  {role:'user',content:'q2',_anchor_activity_scene:{stream_id:'run-1',rows:['r1']},_anchor_stream_id:'run-1'},
  {role:'assistant',content:'a2',_anchor_activity_scene:{stream_id:'run-2',rows:['r2a','r2b']},_anchor_stream_id:'run-2'}
];
const group={getAttribute:(n)=>n==='data-tool-worklog-key'?'anchor-scene:0':(n==='data-activity-disclosure-key'?'settled-stream:run-2':null)};
'''

CASES = {
    # 1. Stable identity resolves the right turn despite stale raw index.
    "settled_stream_beats_stale_raw_index": FIXTURE + r'''
const msg=_messageForWorklogGroup(group);
console.log(JSON.stringify({found:!!msg,stream:msg&&msg._anchor_stream_id,rows:_deferredWorklogRowsFromGroup(group)}));
''',
    # 2. Raw-index fallback for identity-less groups (no disclosure key).
    "raw_index_fallback_still_works": r'''
S.messages=[
  {role:'user',content:'x'},
  {role:'assistant',content:'y',_anchor_activity_scene:{stream_id:'',rows:['r0']}}
];
const g={getAttribute:(n)=>n==='data-tool-worklog-key'?'anchor-scene:1':null};
const msg=_messageForWorklogGroup(g);
console.log(JSON.stringify({found:!!msg,idx:1,rows:_deferredWorklogRowsFromGroup(g)}));
''',
    # 3. settled-stream miss falls back to the raw worklog key, then null.
    "stream_miss_falls_back_then_null": r'''
S.messages=[
  {role:'assistant',content:'y',_anchor_activity_scene:{stream_id:'other',rows:['r9']},_anchor_stream_id:'other'}
];
const g={getAttribute:(n)=>n==='data-tool-worklog-key'?'anchor-scene:0':(n==='data-activity-disclosure-key'?'settled-stream:missing':null)};
const msg=_messageForWorklogGroup(g);
const g2={getAttribute:(n)=>n==='data-tool-worklog-key'?'anchor-scene:7':(n==='data-activity-disclosure-key'?'settled-stream:missing':null)};
console.log(JSON.stringify({fallback:msg&&msg._anchor_stream_id,rows:_deferredWorklogRowsFromGroup(g),missing:_messageForWorklogGroup(g2)}));
''',
    # 4. No cross-turn leakage between distinct streams.
    "no_cross_turn_leakage": r'''
S.messages=[
  {role:'assistant',content:'a1',_anchor_activity_scene:{stream_id:'run-A',rows:['rA']},_anchor_stream_id:'run-A'},
  {role:'assistant',content:'a2',_anchor_activity_scene:{stream_id:'run-B',rows:['rB']},_anchor_stream_id:'run-B'}
];
const gA={getAttribute:(n)=>n==='data-activity-disclosure-key'?'settled-stream:run-A':null};
const gB={getAttribute:(n)=>n==='data-activity-disclosure-key'?'settled-stream:run-B':null};
const rA=_deferredWorklogRowsFromGroup(gA), rB=_deferredWorklogRowsFromGroup(gB);
console.log(JSON.stringify({a:rA&&rA.from,b:rB&&rB.from,distinct:(rA&&rA.from)!==(rB&&rB.from)}));
''',
}

EXPECTED = {
    "settled_stream_beats_stale_raw_index": {
        "found": True,
        "stream": "run-2",
        "rows": {"from": "run-2", "rows": ["r2a", "r2b"]},
    },
    "raw_index_fallback_still_works": {
        "found": True,
        "idx": 1,
        "rows": {"from": "", "rows": ["r0"]},
    },
    "stream_miss_falls_back_then_null": {
        "fallback": "other",
        "rows": {"from": "other", "rows": ["r9"]},
        "missing": None,
    },
    "no_cross_turn_leakage": {"a": "run-A", "b": "run-B", "distinct": True},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_k04c_disclosure_key_invariant(case):
    got = _harness(CASES[case])
    assert got == EXPECTED[case], f"K04c invariant {case} failed: {got} != {EXPECTED[case]}"
