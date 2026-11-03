"""A Steer receipt must survive the compact reattach transport and render once.

The journal's scene role is `user`, while the live Anchor registry uses
`control`. Both are the same accepted Steer and must share a renderer.
"""
import json
import shutil
import subprocess
from pathlib import Path

from api import models, routes
from api.routes import _runtime_journal_snapshot_for_session_payload
from api.run_journal import RunJournalWriter

ROOT = Path(__file__).resolve().parent.parent


def test_actual_journal_replay_keeps_order_identity_and_one_compact_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    writer = RunJournalWriter("sid", "run")
    before = writer.append_sse_event("interim_assistant", {"text": "before"})
    accepted = writer.append_sse_event("steer_delivered", {
        "text": "change direction", "receipt_id": "receipt-1", "session_id": "sid",
        "files": ["diagram.png"], "runtime_path": "/private/hidden/snapshot",
    })
    after = writer.append_sse_event("interim_assistant", {"text": "after"})
    assert isinstance(before, dict) and isinstance(accepted, dict) and isinstance(after, dict)
    live = routes._run_journal_live_snapshot("run")
    assert live is not None
    rows = live["anchor_activity_scene"]["activity_rows"]
    steer = [row for row in rows if row.get("source_event_type") == "steer_delivered"]
    assert len(steer) == 1
    assert steer[0]["role"] == "user"
    assert steer[0]["event_id"] == accepted["event_id"]
    assert steer[0]["seq"] == accepted["seq"]
    assert before["seq"] < accepted["seq"] < after["seq"]
    compact = _runtime_journal_snapshot_for_session_payload(live)
    assert isinstance(compact, dict)
    row = next(row for row in compact["anchor_activity_scene"]["activity_rows"]
               if row.get("source_event_type") == "steer_delivered")
    assert row["payload"] == {"receipt_id": "receipt-1", "files": ["diagram.png"]}
    assert row["text"] == "change direction"
    assert "/private/hidden/snapshot" not in str(compact)


def test_compact_reattach_preserves_receipt_id_and_authored_text_without_duplicate_payload():
    receipt = {
        "kind": "control_boundary", "role": "user", "source_event_type": "steer_delivered",
        "row_id": "run:4", "event_id": "run:4", "run_id": "run", "seq": 4,
        "text": "Follow my revision", "payload": {
            "receipt_id": "test-receipt-4", "text": "Follow my revision",
            "files": ["diagram.png"], "runtime_path": "/private/untrusted/snapshot",
        },
    }
    scene = {"version": "activity_scene_v1", "identity": {"run_id": "run"},
             "activity_rows": [receipt]}
    snapshot = {"messages": [], "tool_calls": [], "anchor_activity_scene": scene}
    compact = _runtime_journal_snapshot_for_session_payload(snapshot)
    assert isinstance(compact, dict)
    row = compact["anchor_activity_scene"]["activity_rows"][0]
    assert row["role"] == "user"
    assert row["text"] == "Follow my revision"
    assert row["payload"] == {"receipt_id": "test-receipt-4", "files": ["diagram.png"]}
    assert snapshot["anchor_activity_scene"]["activity_rows"][0] is receipt


def test_scene_renderer_handles_live_control_and_journal_replay_user_roles():
    node = shutil.which("node")
    assert node
    ui = (ROOT / "static/ui.js").read_text()
    start = ui.index("function _anchorSceneNodeForRow(")
    end = ui.index("\nfunction _anchorSceneTransparentNodeForRow(", start)
    renderer = ui[start:end]
    js = f"""
const vm=require('vm'),assert=require('assert');
const context={{S:{{session:{{session_id:'sid'}}}},
  document:{{createElement:()=>({{dataset:{{}},attributes:{{}},
    setAttribute(k,v){{this.attributes[k]=v;}}}})}},
  _steerRowInnerHtml:(text)=>`Steer: ${{text}}`}};
vm.runInNewContext({json.dumps(renderer)}+'\\nthis.draw=_anchorSceneNodeForRow;',context);
for (const role of ['control','user']) {{
 const row={{kind:'control_boundary',role,source_event_type:'steer_delivered',
   event_id:'run:4',run_id:'run',row_id:'run:4',text:'Follow my revision',
   payload:{{receipt_id:'test-receipt-4'}}}};
 const rendered=context.draw(row,{{settled:true}});
 assert.ok(rendered,`no node for ${{role}}`);
 assert.strictEqual(rendered.dataset.steerReceiptId,'test-receipt-4');
 assert.strictEqual(rendered.dataset.rawText,'Follow my revision');
 assert.strictEqual(rendered.innerHTML,'Steer: Follow my revision');
}}
"""
    result = subprocess.run([node, "-e", js], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
