"""Execute canonical scene settlement in Node for each originating display mode."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node unavailable")
@pytest.mark.parametrize("origin", ["compact_worklog", "transparent_stream", "hide_all_activity", "turn_worklog"])
def test_interim_event_identity_survives_mode_switch_and_reload(origin):
    script = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[1],'utf8');
const origin=process.argv[2];
function extract(name){
  const start=src.indexOf('function '+name+'(');
  if(start<0) throw Error('missing '+name);
  const params=src.indexOf('(',start);
  let depth=0, close=-1;
  for(let i=params;i<src.length;i++){
    if(src[i]==='(') depth++;
    if(src[i]===')'&&!--depth){close=i;break;}
  }
  const brace=src.indexOf('{',close);
  depth=0;
  for(let i=brace;i<src.length;i++){
    if(src[i]==='{') depth++;
    if(src[i]==='}'&&!--depth) return src.slice(start,i+1);
  }
  throw Error('unclosed '+name);
}
for(const name of ['_anchorSceneCleanText','_anchorSceneTextKey','_anchorSceneExistingRowKey',
  '_anchorSceneRowHasLiveIdentity','_anchorSceneSettleLiveRunningRow',
  '_anchorSceneRowLooksLikeFinalAnswer','_anchorSceneRowTextOverlapsExisting',
  '_anchorSceneMessageRowsHaveThinking','_anchorSceneActiveMode',
  '_anchorSceneRowDisplayHintForMode','_completeSettledAnchorSceneForTurn',
  '_anchorSceneHasWorklogWorthyRows','_anchorSceneHasOwnedOutcomes',
  '_anchorSceneMessageOffsetForPersist','_anchorSceneAbsoluteMessageIndexForPersist',
  '_persistSettledAnchorScene','_attachProjectedAnchorSceneToLastAssistant']) eval(extract(name));
let _persistAnchorSceneWarned=false;
const window=global.window={chatActivityMode:()=>origin,isFinalAnswerOnlyMode:()=>origin==='hide_all_activity'};
const activeSid='session-a',streamId='run-a';
const _anchorRegistry={};
const final='The diagnosis is clear and I have fixed the issue with a detailed explanation.';
const progress='The diagnosis is clear and I have fixed the issue with a detailed explanation';
const messages=[{role:'user',content:'Investigate'}, {role:'assistant',content:final,id:'answer'}];
const S=global.S={session:{},messages};
let persisted=null;
const api=(_path,request)=>{persisted=JSON.parse(request.body).scene;return Promise.resolve({});};
function _anchorSceneMessageRef(m){return m.id||m.content;}
function _anchorSceneFinalAnswerText(m){return m.content;}
function _anchorSceneRowsByMessageIndex(){return new Map();}
function _anchorSceneTurnDurationForSettlement(){return 5;}
function _projectLiveAnchorActivityScene(){return {
  mode:origin,activity_rows:[
    {role:'prose',kind:'process_prose',source_event_type:'interim_assistant',event_id:'run-a:1',local_id:'interim:1',text:progress,status:'running'},
    {role:'prose',kind:'process_prose',source_event_type:'interim_assistant',event_id:'run-a:2',local_id:'interim:2',text:progress,status:'running'},
    {role:'prose',kind:'process_prose',source_event_type:'interim_assistant',event_id:'run-a:3',local_id:'interim:3',text:progress+' now',status:'running'},
    {role:'prose',kind:'process_prose',source_event_type:'token',event_id:'run-a:4',local_id:'live-prose:final',text:final.slice(0,20),status:'running'},
  ]};}
const promoted=_attachProjectedAnchorSceneToLastAssistant(messages);
const saved=JSON.parse(JSON.stringify(persisted)); // server round-trip / reload
const prose=saved.activity_rows.filter(r=>r.role==='prose');
console.log(JSON.stringify({promoted, persisted:!!persisted, ids:prose.map(r=>r.event_id),
  text:prose.map(r=>r.text), final:saved.final_answer}));
"""
    result = subprocess.run(
        [NODE, "-e", script, str(ROOT / "static" / "messages.js"), origin],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["persisted"]
    assert data["ids"] == ["run-a:1", "run-a:2", "run-a:3"]
    assert len(set(data["ids"])) == len(data["ids"])
    assert data["text"][0] == data["text"][1]
    assert data["text"][2].startswith(data["text"][1])
    assert data["final"].endswith("detailed explanation.")
    assert data["promoted"] is (origin == "turn_worklog")


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_pure_final_does_not_create_empty_worklog():
    script = r"""
const fs=require('fs'),src=fs.readFileSync(process.argv[1],'utf8');
const start=src.indexOf('function _anchorSceneHasWorklogWorthyRows('),brace=src.indexOf('{',start);
let depth=0,end=brace;
for(;end<src.length;end++){if(src[end]==='{')depth++;if(src[end]==='}'&&!--depth)break;}
eval(src.slice(start,end+1));
const window=global.window={isFinalAnswerOnlyMode:()=>false};
console.log(JSON.stringify(_anchorSceneHasWorklogWorthyRows({mode:'turn_worklog',activity_rows:[]})));
"""
    result = subprocess.run([NODE, "-e", script, str(ROOT / "static" / "messages.js")],
                            cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) is False
