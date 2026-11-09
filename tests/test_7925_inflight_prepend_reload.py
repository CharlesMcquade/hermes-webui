"""An older-history prepend must survive an in-flight bounded reload (#7925)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

HARNESS = r"""
const fs = require('fs');
const SOURCE = fs.readFileSync(process.argv[2], 'utf8');
function extractFunction(name) {
  let start = SOURCE.indexOf(`async function ${name}(`);
  if (start < 0) start = SOURCE.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`missing ${name}`);
  const open = SOURCE.indexOf('{', SOURCE.indexOf('(', start));
  let depth = 0, quote = null, escaped = false, lineComment = false, blockComment = false;
  for (let i = open; i < SOURCE.length; i++) {
    const c = SOURCE[i], n = SOURCE[i + 1];
    if (lineComment) { if (c === '\n') lineComment = false; continue; }
    if (blockComment) { if (c === '*' && n === '/') { blockComment = false; i++; } continue; }
    if (quote) {
      if (escaped) escaped = false;
      else if (c === '\\') escaped = true;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '/' && n === '/') { lineComment = true; i++; continue; }
    if (c === '/' && n === '*') { blockComment = true; i++; continue; }
    if (c === "'" || c === '"' || c === '`') { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return SOURCE.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}
const realFunctions = [
  '_markPrefixProvenRows', '_heldRowsArePrefixProven', '_adoptPrefixProof',
  '_clearSameSessionForceReloadHint', '_messageReloadLimitForSession',
  '_appendAnchorRowFingerprint', '_mergeSessionToolCards',
  '_syncToolCallsForLoadedMessages', '_messageComparableText', '_sameTranscriptMessage',
  '_advanceVisibleBaselineForServerKnownRows', '_ensureMessagesLoaded', '_loadOlderMessages',
];
const _INITIAL_MSG_LIMIT = 30, _MSG_LIMIT_MAX = 500, MESSAGE_RENDER_WINDOW_DEFAULT = 50;
const INFLIGHT = {}, window = globalThis;
let S, _oldestIdx, _messagesTruncated, _msgLimitMax = 500;
let _loadingSessionId, _loadSessionGeneration, _sameSessionForceReloadHint;
let _pendingCarryForwardSnapshot = null, _pendingSessionToolCalls = null;
let _messageRenderWindowSize = 200, _messagesGeneration = 0, _loadingOlder = false;
let _sessionPrefixFp = 'seed', _sessionPrefixFpEnd = 150, _prefixProvenRows = new WeakSet();
let requests = [];
const container = {scrollHeight: 5000, scrollTop: 700};
function $(id) { return id === 'messages' ? container : null; }
function clearLiveToolCards() {}
function clearVisibleMessageRowCache() {}
function _messageRenderableMessageCount() { return S.messages.length; }
function _currentMessageRenderWindowSize() { return _messageRenderWindowSize; }
function _adoptRegenerationRevision() {}
function _hydrateTodosFromSession() {}
function scheduleTodosRefresh() {}
function _setSessionViewedCount() {}
function syncTopbar() {}
function renderMessages() {}
function requestAnimationFrame(fn) { fn(); }
function msgContent(m) { return m && m.content || ''; }
eval(realFunctions.map(extractFunction).join('\n\n'));
function rows(start, end) {
  return Array.from({length:end-start+1}, (_, k) => {
    const idx = start+k;
    return {role:idx%2?'assistant':'user', content:`m${idx}`, timestamp:1700000000+idx, _idx:idx};
  });
}
function payload(start, end, generation=1) {
  return {session:{
    session_id:'s1', message_count:151, visible_message_count:151,
    transcript_generation:generation, regeneration_revision:1,
    messages:rows(start,end), _messages_offset:start,
    _messages_truncated:true, _msg_limit_max:500,
    prefix_fp:`fp-${generation}-${end+1}`, tool_calls:[], last_usage:{},
  }};
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function snapshot(label) {
  const indices = S.messages.map(m => m._idx);
  return {label, indices, first:indices[0], last:indices[indices.length-1],
    length:indices.length, offset:_oldestIdx, truncated:_messagesTruncated};
}
function assert(cond, message) { if (!cond) throw new Error(message); }
async function run(mode) {
  requests = []; _messagesGeneration = 0; _loadingOlder = false; _messageRenderWindowSize = 200;
  _oldestIdx = 100; _messagesTruncated = true; _msgLimitMax = 500;
  _loadingSessionId = 's1'; _loadSessionGeneration = 7;
  _sameSessionForceReloadHint = {session_id:'s1', loaded_renderable_count:50,
    loaded_message_count:50, message_count:150, truncated:true};
  _sessionPrefixFp = 'seed'; _sessionPrefixFpEnd = 150; _prefixProvenRows = new WeakSet();
  S = {session:{session_id:'s1', message_count:151, transcript_generation:1,
    regeneration_revision:1, tool_calls:[]}, messages:rows(100,149), busy:false};
  S.messages.forEach(r => _prefixProvenRows.add(r));
  const heldReload = deferred();
  globalThis.api = url => {
    requests.push(url);
    const q = new URLSearchParams(url.split('?')[1]);
    if (q.get('msg_limit') === '80') return Promise.resolve(payload(70,149));
    if (q.get('msg_limit') === '51') return heldReload.promise;
    throw new Error(`unexpected request: ${url}`);
  };
  const reloadPromise = _ensureMessagesLoaded('s1', {force:true, loadGeneration:7});
  await Promise.resolve();
  assert(requests.length === 1 && requests[0].includes('msg_limit=51'), 'bounded reload did not pause');
  if (mode !== 'control') {
    await _loadOlderMessages();
    assert(S.messages.length === 80 && S.messages[0]._idx === 70 && S.messages[79]._idx === 149,
      'Load-older did not establish 70..149');
    assert(_oldestIdx === 70 && _messagesTruncated === true, 'prepend offsets/truncation wrong');
  }
  heldReload.resolve(payload(100,150, mode === 'generation-change' ? 2 : 1));
  await reloadPromise;
  return {mode, requests, final:snapshot('final')};
}
(async () => {
  const prepend = await run('prepend');
  const control = await run('control');
  const generationChange = await run('generation-change');
  const fullRange = Array.from({length:81}, (_,i) => i+70);
  assert(JSON.stringify(prepend.final.indices) === JSON.stringify(fullRange),
    `prepend was lost: ${JSON.stringify(prepend.final)}`);
  assert(prepend.final.offset === 70 && prepend.final.truncated === true, 'widened cursor wrong');
  assert(control.final.first === 100 && control.final.last === 150 && control.final.length === 51 &&
    control.final.offset === 100, 'ordinary bounded replace changed');
  assert(generationChange.final.first === 100 && generationChange.final.last === 150 &&
    generationChange.final.length === 51 && generationChange.final.offset === 100,
    'changed transcript generation did not replace authoritatively');
  console.log(JSON.stringify({prepend, control, generationChange}));
})().catch(err => { console.error(err && err.stack || err); process.exitCode = 1; });
"""


def test_inflight_prepend_survives_real_bounded_reload(tmp_path):
    assert NODE is not None
    script = tmp_path / "prepend_reload.cjs"
    script.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script), str(REPO / "static" / "sessions.js")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["prepend"]["final"]["indices"] == list(range(70, 151))
    assert outcome["control"]["final"]["indices"] == list(range(100, 151))
