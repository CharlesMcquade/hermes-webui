"""Regression: an accepted-but-unconsumed steer must not render twice.

Sequence seen in production (session c3d581871f28): the steer was accepted
mid-run (``steer_delivered`` status=received), the final model call was
already in flight, so the run ended and emitted ``pending_steer_leftover``
with the same text, which was then re-sent as the next turn's user message.
The in-turn Steer row stayed, so the transcript showed the text twice.
"""

import json
import shutil
import subprocess
from pathlib import Path

import api.routes as routes
from api import run_journal as RJ

ROOT = Path(__file__).resolve().parent.parent
STEER = "this is in the context of the nationwide flight grounding playa"


def _row(run_id, session_id, seq, event, payload):
    return json.dumps({
        "version": 2, "event_id": f"{run_id}:{seq}", "seq": seq,
        "run_id": run_id, "session_id": session_id, "event": event,
        "type": event, "created_at": 1000 + seq, "payload": payload,
        "terminal": event in ("done", "stream_end"),
        "terminal_state": "completed" if event in ("done", "stream_end") else None,
    })


def _write(root, session_id, run_id, events):
    d = root / RJ.RUN_JOURNAL_DIR_NAME / session_id
    d.mkdir(parents=True, exist_ok=True)
    rows = [_row(run_id, session_id, i + 1, ev, p) for i, (ev, p) in enumerate(events)]
    (d / f"{run_id}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _steer_rows(snapshot):
    rows = (snapshot.get("anchor_activity_scene") or {}).get("activity_rows") or []
    return [r for r in rows if r.get("source_event_type") == "steer_delivered"]


def _steer(text, receipt):
    return ("steer_delivered", {"text": text, "receipt_id": receipt, "files": [], "status": "received"})


def test_leftover_retires_unconsumed_steer_in_journal_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)
    _write(tmp_path, "s1", "run_a", [
        ("token", {"text": "answer"}),
        _steer(STEER, "r1"),
        ("pending_steer_leftover", {"session_id": "s1", "text": STEER}),
        ("done", {}),
    ])
    snap = routes._run_journal_live_snapshot("run_a")
    assert snap is not None
    assert _steer_rows(snap) == []


def test_consumed_steer_without_leftover_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)
    _write(tmp_path, "s1", "run_b", [
        ("token", {"text": "a"}),
        _steer(STEER, "r1"),
        ("token", {"text": "b"}),
        ("done", {}),
    ])
    snap = routes._run_journal_live_snapshot("run_b")
    assert [r["text"] for r in _steer_rows(snap)] == [STEER]


def test_leftover_cancels_only_matching_steer_and_one_per_block():
    steers = [
        {"seq": 2, "text": "same", "status": "received"},
        {"seq": 4, "text": "other", "status": "received"},
        {"seq": 6, "text": "same", "status": "received"},
    ]
    kept = routes._retire_leftover_steer_events(steers, [{"seq": 9, "text": "same"}])
    # One leftover block retires exactly one (the latest) identical steer.
    assert [s["seq"] for s in kept] == [2, 4]
    # Multi-steer joined leftover retires each contained block.
    kept = routes._retire_leftover_steer_events(steers, [{"seq": 9, "text": "other\nsame"}])
    assert [s["seq"] for s in kept] == [2]
    # A steer after the leftover is never retired by it.
    kept = routes._retire_leftover_steer_events(steers, [{"seq": 5, "text": "same"}])
    assert [s["seq"] for s in kept] == [4, 6]


def test_frontend_settled_render_drops_leftover_steer_row():
    node = shutil.which("node")
    assert node
    ui = (ROOT / "static/ui.js").read_text()
    start = ui.index("function _steerTextLines(")
    end = ui.index("function _anchorSceneRowsForRendering(", start)
    helpers = ui[start:end]
    script = f"""
const vm=require('vm'),assert=require('assert');
const scope={{}};
vm.runInNewContext({json.dumps(helpers)}+'\\nthis.retire=_retireLeftoverSteerRows;',scope);
const steer=(seq,text,status)=>({{source_event_type:'steer_delivered',seq,text,status,payload:{{text,status}}}});
const lo=(seq,text)=>({{source_event_type:'pending_steer_leftover',seq,text,payload:{{text}}}});
const tool={{source_event_type:'tool_complete',seq:1,role:'tool'}};
// Production shape: received steer then matching leftover -> steer row gone.
let out=scope.retire([tool,steer(2,{json.dumps(STEER)},'received'),lo(3,{json.dumps(STEER)})]);
assert.deepStrictEqual(out.map(r=>r.source_event_type),['tool_complete','pending_steer_leftover']);
// No leftover -> untouched (same array identity).
const rows=[tool,steer(2,'x','received')];
assert.strictEqual(scope.retire(rows),rows);
// Non-matching leftover keeps the steer.
out=scope.retire([steer(2,'x','received'),lo(3,'y')]);
assert.strictEqual(out.filter(r=>r.source_event_type==='steer_delivered').length,1);
// Steer after the leftover is not retired.
out=scope.retire([lo(1,'x'),steer(2,'x','received')]);
assert.strictEqual(out.filter(r=>r.source_event_type==='steer_delivered').length,1);
console.log('verified');
"""
    result = subprocess.run([node, "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout


def test_settled_render_entrypoint_applies_retire_filter():
    ui = (ROOT / "static/ui.js").read_text()
    start = ui.index("function _anchorSceneRowsForRendering(")
    body = ui[start:start + 400]
    assert "_retireLeftoverSteerRows(" in body
