"""Frontend-only steer receipt / scene chronology regression."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_journal_receipt_scene_and_native_duplicate_gate():
    node = shutil.which('node')
    assert node
    anchors = ROOT / 'static/assistant_turn_anchors.js'
    ui = (ROOT / 'static/ui.js').read_text()
    start = ui.index('function _nativeSteerCoveredByScene(')
    end = ui.index('\nfunction _messageVirtualWindow(', start)
    suppression = ui[start:end]
    script = f"""
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const sandbox={{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(anchors))},'utf8'),sandbox);
const api=sandbox.window.HermesAssistantTurnAnchors;
const reg=api.createAssistantTurnAnchorRegistry({{session_id:'sid',run_id:'run'}});
const apply=(type,seq,payload)=>api.applyAssistantTurnAnchorSourceEvent(reg,{{
  source_event_type:type,event_id:`run:${{seq}}`,seq,run_id:'run',session_id:'sid',payload
}});
apply('interim_assistant',1,{{text:'before'}});
apply('interim_assistant',3,{{text:'after'}});
assert.strictEqual(apply('steer_delivered',2,{{text:'same text',receipt_id:'uuid-a'}}).applied,true);
assert.strictEqual(apply('steer_delivered',2,{{text:'same text',receipt_id:'uuid-a'}}).reason,'duplicate');
apply('steer_delivered',4,{{text:'same text',receipt_id:'uuid-b'}});
const scene=api.projectAssistantTurnAnchorActivityScene(reg);
assert.deepStrictEqual(Array.from(scene.activity_rows.map(r=>r.seq)),[1,2,3,4]);
assert.strictEqual(scene.activity_rows[1].payload.receipt_id,'uuid-a');
assert.strictEqual(scene.activity_rows[3].payload.receipt_id,'uuid-b');
const S={{messages:[
  {{role:'user',display_kind:'steer',content:'same text',_steer_receipt_ids:['uuid-a']}},
  {{role:'user',display_kind:'steer',content:'same text',_steer_receipt_ids:['uuid-other']}},
  {{role:'assistant',_anchor_activity_scene:scene}}
]}};
const scope={{S}};
vm.runInNewContext({json.dumps(suppression)}+`\nthis.check=_nativeSteerCoveredByScene;`,scope);
assert.strictEqual(scope.check(S.messages[0],0),true);
assert.strictEqual(scope.check(S.messages[1],1),false);
const foreign=JSON.parse(JSON.stringify(scene));
foreign.identity.run_id='other-run';
S.messages[2]._anchor_activity_scene=foreign;
assert.strictEqual(scope.check(S.messages[0],0),false);
console.log('steer journal order, idempotency, and receipt-only suppression verified');
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True, check=True)
    assert 'verified' in result.stdout


def test_steer_admission_and_renderer_contract():
    messages = (ROOT / 'static/messages.js').read_text()
    commands = (ROOT / 'static/commands.js').read_text()
    ui = (ROOT / 'static/ui.js').read_text()
    assert "source.addEventListener('steer_delivered'" in messages
    assert "_steerAdmissions.set(streamId,_admitSteer)" in messages
    assert "const receipt=result.steer_event" in commands
    assert "_showSteerIndicator(originalMsg,steerUploadedPaths)" not in commands
    assert "node.innerHTML=_steerRowInnerHtml(text,[], ''" in ui
    assert "row.source_event_type==='steer_delivered'" in ui
