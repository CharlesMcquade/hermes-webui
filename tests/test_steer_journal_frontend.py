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
const scope={{S,isFinalAnswerOnlyMode:()=>false}};
vm.runInNewContext({json.dumps(suppression)}+`\nthis.check=_nativeSteerCoveredByScene;`,scope);
assert.strictEqual(scope.check(S.messages[0],0),true);
assert.strictEqual(scope.check(S.messages[1],1),false);
scope.isFinalAnswerOnlyMode=()=>true;
assert.strictEqual(scope.check(S.messages[0],0),false);
scope.isFinalAnswerOnlyMode=()=>false;
const foreign=JSON.parse(JSON.stringify(scene));
foreign.identity.run_id='other-run';
S.messages[2]._anchor_activity_scene=foreign;
assert.strictEqual(scope.check(S.messages[0],0),false);
console.log('steer journal order, idempotency, and receipt-only suppression verified');
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True, check=True)
    assert 'verified' in result.stdout


def test_journal_sse_header_is_reconstituted_as_post_receipt():
    node = shutil.which('node')
    assert node
    messages = (ROOT / 'static/messages.js').read_text()
    start = messages.index("    source.addEventListener('steer_delivered',e=>{")
    end = messages.index("\n    for(const _runJournalEventName", start)
    handler = messages[start:end]
    script = f"""
const vm=require('vm'),assert=require('assert');
const received=[];
const source={{addEventListener:(type,fn)=>{{if(type==='steer_delivered') source.handler=fn;}}}};
const streamId='run',activeSid='sid',_admitSteer=e=>received.push(e);
vm.runInNewContext({json.dumps(handler)},{{source,streamId,activeSid,_admitSteer}});
source.handler({{lastEventId:'run:12',data:JSON.stringify({{text:'steer',receipt_id:'uuid'}})}});
assert.deepStrictEqual(JSON.parse(JSON.stringify(received)),[{{event_id:'run:12',run_id:'run',seq:12,
  session_id:'sid',payload:{{text:'steer',receipt_id:'uuid'}}}}]);
source.handler({{lastEventId:'foreign:13',data:'{{}}'}});
assert.strictEqual(received.length,1);
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_collapsed_steer_boundary_projection_replaces_stale_rows():
    node = shutil.which('node')
    assert node
    ui = (ROOT / 'static/ui.js').read_text()
    start = ui.index('function _syncSettledSteerBoundaryPreviews(')
    end = ui.index('\nfunction _syncLiveWorklogReasonsForAnchor(', start)
    projection = ui[start:end]
    script = f"""
const vm=require('vm'),assert=require('assert');
function element(){{
  return {{className:'',classList:{{contains:n=>n==='steer-settled-preview-group'}},
    children:[],replaceChildren(){{this.children=[];}},
    appendChild(n){{this.children.push(n);}},
    get childElementCount(){{return this.children.length;}},
    remove(){{this.removed=true;}}}};
}}
const group={{parentElement:{{}},nextElementSibling:null,
  insertAdjacentElement(where,child){{assert.strictEqual(where,'afterend');this.nextElementSibling=child;}}}};
const context={{document:{{createElement:element}},
  _anchorSceneNodeForRow:r=>r.payload&&r.payload.receipt_id
    ?{{id:r.payload.receipt_id,setAttribute(k,v){{this[k]=v;}}}}:null}};
vm.runInNewContext({json.dumps(projection)}+'\\nthis.sync=_syncSettledSteerBoundaryPreviews;',context);
const steer=id=>({{source_event_type:'steer_delivered',payload:{{receipt_id:id}}}});
context.sync(group,[steer('a'),{{source_event_type:'tool'}},steer('b')]);
assert.deepStrictEqual(group.nextElementSibling.children.map(n=>n.id),['a','b']);
context.sync(group,[steer('b')]);
assert.deepStrictEqual(group.nextElementSibling.children.map(n=>n.id),['b']);
context.sync(group,[]);
assert.strictEqual(group.nextElementSibling.removed,true);
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert '_syncSettledSteerBoundaryPreviews(group,rows);' in ui


def test_steer_admission_and_renderer_contract():
    messages = (ROOT / 'static/messages.js').read_text()
    commands = (ROOT / 'static/commands.js').read_text()
    ui = (ROOT / 'static/ui.js').read_text()
    assert "source.addEventListener('steer_delivered'" in messages
    assert "_steerAdmissions.set(streamId,_admitSteer)" in messages
    assert "const receipt=result.steer_event" in commands
    assert "_showSteerIndicator(originalMsg,steerUploadedPaths," in commands
    assert "if(!admitted&&_steerOwnerIsCurrent(ownerSid))" in commands
    assert "node.innerHTML=_steerRowInnerHtml(text,labels,'',S.session&&S.session.session_id||'',true)" in ui
    assert "row.source_event_type==='steer_delivered'" in ui


def _backstop_harness(live_after_ms):
    """Run the real creation-time backstop with fake timers.

    `live_after_ms`: run stays live until this simulated time (None = never live).
    Returns ms at which the registry + Steer admission hook were deleted.
    """
    messages = (ROOT / 'static/messages.js').read_text()
    start = messages.index('  const _ANCHOR_BACKSTOP_MAX_DEFER_MS=')
    end = messages.index('  // Applying an event and painting it are separate outcomes.', start)
    block = messages[start:end]
    live = 'null' if live_after_ms is None else str(live_after_ms)
    return f"""
const vm=require('vm'),assert=require('assert');
let now=0;const timers=[];
const ctx={{
  Date:{{now:()=>now}},
  setTimeout:(fn,ms)=>timers.push({{at:now+ms,fn}}),
  streamId:'run',activeSid:'sid',
  _anchorRegistry:{{}},_admitSteer:()=>true,
  _terminalStateReached:false,_streamFinalized:false,
  LIVE_STREAMS:{{}},
}};
ctx._anchorRegistryMap=new Map([['run',ctx._anchorRegistry]]);
ctx._steerAdmissions=new Map([['run',ctx._admitSteer]]);
const liveUntil={live};
function step(){{
  timers.sort((a,b)=>a.at-b.at);const t=timers.shift();if(!t)return false;now=t.at;
  ctx.LIVE_STREAMS.sid=(liveUntil!==null&&now<liveUntil)?{{streamId:'run'}}:undefined;
  t.fn();return true;
}}
vm.runInNewContext({json.dumps(block)},ctx);
let deletedAt=null;
while(step()){{ if(deletedAt===null&&!ctx._anchorRegistryMap.has('run')){{deletedAt=now;
  assert.strictEqual(ctx._steerAdmissions.has('run'),false);}} if(now>24*3600e3)break; }}
console.log(JSON.stringify({{deletedAt}}));
"""


def _run_backstop(live_after_ms):
    node = shutil.which('node')
    assert node
    result = subprocess.run([node, '-e', _backstop_harness(live_after_ms)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])['deletedAt']


def test_backstop_does_not_expire_live_run_registry_or_steer_hook():
    # Regression: turns >10 min lost their live Steer admission hook, so
    # accepted Steers showed only a toast until settlement/reload.
    # Run stays live for 47 min: cleanup must wait until the first tick after.
    assert _run_backstop(47 * 60_000) == 50 * 60_000


def test_backstop_still_expires_dead_or_detached_runs_on_schedule():
    assert _run_backstop(None) == 10 * 60_000


def test_backstop_defer_is_bounded():
    # Never-ending "live" run: bounded at 6h so the registry cannot leak.
    assert _run_backstop(10 ** 12) == 6 * 60 * 60_000


def test_steer_post_falls_back_to_live_row_when_admission_fails():
    node = shutil.which('node')
    assert node
    commands = (ROOT / 'static/commands.js').read_text()
    start = commands.index('    const receipt=result.steer_event;')
    end = commands.index('    showToast(result.durable===false', start)
    block = commands[start:end]
    script = f"""
const vm=require('vm'),assert=require('assert');
function run(hook){{
  const shown=[];
  const window={{_steerAnchorAdmissions:new Map(hook?[['run',hook]]:[])}};
  vm.runInNewContext({json.dumps(block)},{{window,result:{{steer_event:{{event_id:'run:1'}}}},
    ownerStreamId:'run',ownerSid:'sid',originalMsg:'m',steerUploadedPaths:[],
    _steerOwnerIsCurrent:()=>true,_showSteerIndicator:(t)=>shown.push(t)}});
  return shown.length;
}}
assert.strictEqual(run(()=>true),0);      // admitted: scene owns the row
assert.strictEqual(run(()=>false),1);     // rejected: fallback live row
assert.strictEqual(run(null),1);          // hook missing (expired): fallback
assert.strictEqual(run(()=>{{throw new Error('x')}}),1);
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_scene_admission_removes_matching_fallback_row_only():
    node = shutil.which('node')
    assert node
    messages = (ROOT / 'static/messages.js').read_text()
    commands = (ROOT / 'static/commands.js').read_text()
    start = messages.index('  const _admitSteer=(envelope)=>{')
    end = messages.index('  _steerAdmissions.set(streamId,_admitSteer);', start)
    block = messages[start:end]
    assert "_showSteerIndicator(originalMsg,steerUploadedPaths,typeof rid==='string'?rid:'')" in commands
    assert "el.dataset.steerReceipt=receiptId" in commands
    script = f"""
const vm=require('vm'),assert=require('assert');
const rows=[{{r:'uuid-1',removed:false}},{{r:'other',removed:false}}];
const document={{querySelectorAll:sel=>{{
  const m=/data-steer-receipt="([^"]+)"/.exec(sel);
  return rows.filter(x=>x.r===m[1]).map(x=>({{remove(){{x.removed=true;}}}}));}}}};
const ctx={{document,CSS:{{escape:s=>s}},activeSid:'sid',
  _anchorRegistry:{{anchor:{{identity:{{run_id:'run'}}}}}},
  _applyToAnchor:()=>({{applied:true}})}};
vm.runInNewContext({json.dumps(block)}+';this.admit=_admitSteer;',ctx);
assert.strictEqual(ctx.admit({{event_id:'run:3',run_id:'run',session_id:'sid',seq:3,
  payload:{{text:'t',receipt_id:'uuid-1'}}}}),true);
assert.deepStrictEqual(rows.map(x=>x.removed),[true,false]);
ctx._applyToAnchor=()=>({{applied:false,reason:'stale'}});
rows[1].removed=false;
assert.strictEqual(ctx.admit({{event_id:'run:4',run_id:'run',session_id:'sid',seq:4,
  payload:{{text:'t',receipt_id:'other'}}}}),false);
assert.strictEqual(rows[1].removed,false);  // rejected admission keeps fallback
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
