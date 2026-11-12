"""Node-VM tests for turn-index outline navigation and bounded jump loads."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "static" / "outline.js").read_text(encoding="utf-8")


def _slice(start: str, end: str | None = None) -> str:
    begin = SOURCE.index(start)
    return SOURCE[begin:SOURCE.index(end, begin) if end else len(SOURCE)]


def _run(scenario: str) -> None:
    node = shutil.which("node") or str(Path.home() / ".local/bin/node")
    functions = "\n".join([
        _slice("function _currentSid()", "function _outlineAllowed()"),
        _slice("function _outlineNeedsTurnIndex()", "// Extracts the first 60"),
        _slice("function _excerptText(content)", "// Scrolls to a user"),
        _slice("async function _jumpToMessage", "// Content-search jump"),
        _slice("function _flashRow(row)", "// Builds the list"),
        _slice("function _buildEntries()", "// Simple HTML-escape"),
        _slice("function _escHtml(str)", "// Opens or closes"),
    ])
    scenario = scenario.replace("__KEY_HANDLER__", _slice("document.addEventListener('keydown',", "window.addEventListener('resize'"))
    prelude = """
const vm=require('vm'),assert=require('assert');
const S={session:{session_id:'sid',pending_started_at:0},messages:[],busy:false,activeStreamId:null};
let _messagesTruncated=false,_oldestIdx=0,_msgLimitMax=500,_loadingOlder=false;
let _messagesGeneration=1,_loadingSessionId=null,_outlineSid=null,_outlineTurnEntries=[];
let _currentPanel='chat';
const rows={},calls=[],flashes=[];
const panel={innerHTML:''};
const container={getBoundingClientRect:()=>({top:100}),querySelectorAll:()=>[]};
const document={getElementById:id=>id==='outlinePanel'?panel:id==='messages'?container:(rows[id]||null)};
const documentListeners={};
document.addEventListener=(name,fn)=>{documentListeners[name]=fn;};
const window={_sessionSearchJumpGen:0,setTimeout:fn=>fn()};
let api=async url=>{calls.push(url);throw new Error('unexpected request: '+url);};
const t=s=>s;
const _messageSessionIndexBase=()=>_oldestIdx;
const _messageRawIdxForSessionIndex=i=>i-_oldestIdx;
const _messageSessionIndexForRawIdx=i=>i+_oldestIdx;
const _mergeSessionToolCards=(a,b)=>b||a||[];
const _syncToolCallsForLoadedMessages=()=>{};
const _advanceVisibleBaselineForServerKnownRows=()=>{};
const _markPrefixProvenRows=()=>{},_adoptPrefixProof=()=>{};
const _cancelBottomSettle=()=>{},_beginMessageJumpScroll=()=>{};
const _expandOutlineRenderWindow=()=>{};
const requestAnimationFrame=fn=>fn();
let renderMessages=()=>{};
const _ensureAllMessagesLoaded=async()=>{calls.push('msg_limit=all');return true;};
const _highlightQuestionRow=row=>flashes.push(Number(row.dataset.sessionMsgIdx));
"""
    script = prelude + functions + "\n(async()=>{\n" + scenario + "\nconsole.log('verified');})().catch(e=>{console.error(e);process.exit(1);});"
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout


def _message(idx: int, role: str = "user") -> str:
    return "{role:'%s',content:'m%d'}" % (role, idx)


def test_turns_endpoint_used_when_truncated():
    _run("""
_messagesTruncated=true;_oldestIdx=50;
const turns=async url=>{calls.push(url);return {turns:[{k:1,msg_idx:2,ts:1,preview:'first question',n_msgs:3}]};};
await (async()=>{const original=api;api=turns;await _ensureOutlineMessagesLoaded('sid');api=original;})();
assert.strictEqual(calls.length,1);assert(calls[0].startsWith('/api/session/turns?session_id=sid'));
assert.strictEqual(_outlineTurnEntries[0].rawIdx,2);assert.strictEqual(_outlineTurnEntries[0].label,1);
""")


def test_turns_endpoint_not_used_when_untruncated():
    _run("""
S.messages=[{role:'user',content:'local question'}];
assert.strictEqual(await _ensureOutlineMessagesLoaded('sid'),false);
assert.strictEqual(calls.length,0);assert.strictEqual(_buildEntries().length,1);
""")


def test_unloaded_jump_uses_one_bounded_page_and_updates_bookkeeping():
    scenario = """
_oldestIdx=60;_messagesTruncated=true;S.messages=[%s];
api=async url=>{calls.push(url);return {session:{_messages_offset:30,_messages_truncated:true,
  _msg_limit_max:500,messages:[%s],tool_calls:[]}};};
renderMessages=()=>{rows['msg-user-0']={dataset:{sessionMsgIdx:'30'},
  scrollIntoView:()=>{},classList:{remove:()=>{},add:()=>{}}};};
await _jumpToMessage(30);
assert.strictEqual(calls.length,1);assert(calls[0].includes('msg_before=61'));
assert(!calls[0].includes('msg_limit=all'));assert.strictEqual(_oldestIdx,30);
assert.strictEqual(S.messages.length,50);assert.deepStrictEqual(flashes,[30]);
""" % (",".join(_message(i, "assistant") for i in range(60, 80)),
       ",".join(_message(i) for i in range(30, 60)))
    _run(scenario)


def test_alt_arrow_up_moves_to_previous_user_turn():
    _run("""
S.messages=[{role:'user',content:'zero'},{role:'assistant',content:'a'},
  {role:'user',content:'one'},{role:'assistant',content:'b'}];
rows['msg-user-0']={dataset:{sessionMsgIdx:'0'},scrollIntoView:()=>{}};
container.querySelectorAll=()=>[{dataset:{sessionMsgIdx:'2'},getBoundingClientRect:()=>({bottom:150})}];
__KEY_HANDLER__
let prevented=false;
await documentListeners.keydown({altKey:true,key:'ArrowUp',target:{tagName:'DIV'},preventDefault:()=>{prevented=true;}});
assert.deepStrictEqual(flashes,[0]);assert.strictEqual(prevented,true);
""")


def test_alt_arrow_ignored_in_textarea():
    _run("""
S.messages=[{role:'user',content:'zero'},{role:'user',content:'one'}];
container.querySelectorAll=()=>[{dataset:{sessionMsgIdx:'1'},getBoundingClientRect:()=>({bottom:150})}];
__KEY_HANDLER__
let prevented=false;
await documentListeners.keydown({altKey:true,key:'ArrowUp',target:{tagName:'TEXTAREA'},preventDefault:()=>{prevented=true;}});
assert.strictEqual(flashes.length,0);assert.strictEqual(prevented,false);
""")


def test_stale_session_switch_discards_turn_index_result():
    _run("""
_messagesTruncated=true;
let resolveTurns;api=()=>new Promise(resolve=>{resolveTurns=resolve;});
const pending=_ensureOutlineMessagesLoaded('sid');S.session.session_id='other';_outlineSid='other';
resolveTurns({turns:[{k:1,msg_idx:4,preview:'stale'}]});await pending;
assert.strictEqual(_outlineTurnEntries.length,0);
""")
