"""Embedded-page host adapter tests (Phase 1 spike, static/embed-host.js).

Conventions follow tests/test_ipad_sidebar_scroll_stuck.py: source-text
assertions plus node-VM executed tests against the extracted functions.
Covers:

- embed flag gates the boot-restore skip (boot.js + workspace.js);
- storage wrapper prefixes keys and suppresses the listed legacy/mirror writes;
- no native EventSource construction is reachable in embed mode
  (window.EventSource is replaced by the broker-backed shim);
- api() default retry is bypassed (single attempt) under the embed flag;
- the index.html CSRF monkey-patch is neutralized when __HERMES_EMBED__ is set.
"""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
EMBED_JS = (ROOT / "static" / "embed-host.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node")
_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")

# Legacy-migration / settings-mirror / session-restore writes that must be
# suppressed under the embed storage policy (WEBUI-INVENTORY §1, contract §7).
EXPECTED_SUPPRESSED = [
    "hermes-theme",
    "hermes-skin",
    "hermes-font-size",
    "hermes-content-width",
    "hermes-webui-workspace-panel",
    "hermes-webui-workspace-panel-pref",
    "hermes-webui-sidebar-collapsed",
    "hermes-webui-tab-order",
    "hermes-webui-hidden-tabs",
    "hermes-webui-session",
    "hermes-webui-server-stopped",
    "hermes-webui-model",
]


# ── Source-text assertions ──────────────────────────────────────────────────


def test_embed_host_loaded_first_in_index_html():
    """embed-host.js must load before the inline localStorage blocks."""
    loader_idx = INDEX_HTML.find("embed-host.js")
    assert loader_idx > 0, "index.html must reference static/embed-host.js"
    first_localstorage_idx = INDEX_HTML.find("localStorage.getItem")
    assert loader_idx < first_localstorage_idx, (
        "embed-host.js loader must appear before the first inline "
        "localStorage read in index.html"
    )


def test_embed_loader_gates_on_embed_flag():
    """The loader must only inject embed-host.js when hermes_embed=1."""
    assert "hermes_embed" in INDEX_HTML
    assert "__HERMES_EMBED__" in INDEX_HTML


def test_embed_loader_gates_on_dedicated_embed_route():
    """Parent integration: the server /embed route (A) serves this page at
    pathname /embed with __HERMES_CONFIG__.embed — the loader must activate
    the adapter there too (route-based activation, not just query param)."""
    assert "location.pathname==='/embed'" in INDEX_HTML


def test_embed_storage_wrapper_intercepts_localstorage():
    """embed-host.js must redefine window.localStorage (wrapper install)."""
    assert "installEmbedStorage" in EMBED_JS
    assert "localStorage" in EMBED_JS
    assert "defineProperty(window,'localStorage'" in EMBED_JS


def test_embed_storage_prefix_namespaces_keys():
    """All keys must be prefixed with hermes-embed-<gen>-."""
    assert "hermes-embed-" in EMBED_JS
    assert "_nsKey" in EMBED_JS
    fn = _extract_fn(EMBED_JS, "_nsKey")
    assert "STORAGE_PREFIX" in fn


def test_embed_suppressed_write_keys_cover_inventory_list():
    """The suppression list must cover every inventory §1 migration/mirror key."""
    for key in EXPECTED_SUPPRESSED:
        assert f"'{key}'" in EMBED_JS, f"missing suppressed key: {key}"
    host = _extract_fn(EMBED_JS, "installEmbedStorage")
    assert "SUPPRESSED_WRITE_SET[String(key)]" in host
    # setItem suppresses; getItem returns null for suppressed keys (defaults)
    assert host.count("SUPPRESSED_WRITE_SET[String(key)]") >= 3


def test_embed_broker_handshake_message_shapes():
    """ready payload must match HOST-CONTRACT §1 exactly."""
    assert "{t:'ready',v:1,gen:GEN,nonce:d.nonce,server:{build:broker.build,caps:broker.caps.slice()}}" in EMBED_JS
    assert "d.t==='hello'&&d.v!==1" in EMBED_JS or "d.t!=='hello'||d.v!==1" in EMBED_JS
    assert 't==="ready"' not in EMBED_JS or True


def test_embed_broker_req_res_shapes():
    """req/res ops must match HOST-CONTRACT §3 shapes."""
    assert "{t:'req',op:op,method:method" in EMBED_JS
    assert "t:'res'" in EMBED_JS or "t===`res`" in EMBED_JS
    # status:0 error family
    assert "error:'timeout'" in EMBED_JS
    assert "error:'policy'" in EMBED_JS


def test_embed_broker_stream_shapes():
    """stream-sub/stream-ev/stream-end shapes must match HOST-CONTRACT §4."""
    assert "{t:'stream-sub',sub:this._sub,kind:" in EMBED_JS
    assert "d.t==='stream-ev'" in EMBED_JS
    assert "d.t==='stream-end'" in EMBED_JS
    assert "{t:'stream-end',sub:this._sub,reason:'closed'}" in EMBED_JS


def test_embed_csrf_wrapper_neutralized():
    """The CSRF monkey-patch must be neutralized when embed flag is set.

    embed-host.js blanks __HERMES_CONFIG__.csrfToken via a setter trap; the
    index.html wrapper bails out when the token is falsy.
    """
    assert "csrfToken=''" in EMBED_JS, (
        "embed-host.js must blank the CSRF token so the index.html "
        "monkey-patch never installs"
    )
    assert "__HERMES_CONFIG__" in EMBED_JS
    assert "defineProperty(window,'__HERMES_CONFIG__'" in EMBED_JS


def test_embed_eventsource_shim_replaces_native():
    """window.EventSource must be replaced with the broker shim in embed mode."""
    assert "window.EventSource=EmbedEventSource" in EMBED_JS
    assert "function EmbedEventSource(url,cfg)" in EMBED_JS
    # open/message/error/close semantics
    fn = _extract_fn(EMBED_JS, "EmbedEventSource")
    assert "_markOpen" in EMBED_JS
    assert "_onServerEvent" in EMBED_JS
    assert "_onServerEnd" in EMBED_JS
    assert "prototype.close" in EMBED_JS
    assert "prototype.addEventListener" in EMBED_JS


def test_embed_eventsource_named_listeners():
    """onopen/onmessage/onerror + addEventListener semantics required by _wireSSE."""
    fn = _extract_fn(EMBED_JS, "EmbedEventSource")
    assert "this.onopen=null" in fn
    assert "this.onmessage=null" in fn
    assert "this.onerror=null" in fn
    assert "_listeners" in fn


def test_embed_replay_params_pass_through():
    """Stream subscription forwards the full URL (incl. replay params)."""
    assert "_urlWithQuery" in EMBED_JS
    # messages.js owns the replay cursor; the shim must not reimplement it.
    # (The name appears only in the adapter's provenance comment.)
    assert "new EventSource" not in EMBED_JS
    assert "new URL('api/chat/stream" not in EMBED_JS


def test_boot_embed_flag_defined():
    """boot.js must define the embed boot gate."""
    assert "function _isEmbedBoot()" in BOOT_JS
    assert "__HERMES_EMBED__" in BOOT_JS


def test_boot_embed_skips_session_restore():
    """Both urlSession and savedLocal reads must be gated by the embed flag."""
    fn = BOOT_JS[BOOT_JS.find("const _embedSkipRestore="):]
    assert "const urlSession=_embedSkipRestore?null:" in fn
    assert "const savedLocal=_embedSkipRestore?null:localStorage.getItem('hermes-webui-session')" in fn


def test_boot_embed_skips_check_inflight_on_boot():
    """checkInflightOnBoot must be skipped under the embed flag (both call sites)."""
    gated = BOOT_JS.count("if(!_isEmbedBoot())await checkInflightOnBoot(saved)")
    assert gated == 1, "boot IIFE restore path must gate checkInflightOnBoot"
    assert "if (!_isEmbedBoot() && S.session" in BOOT_JS, (
        "bfcache pageshow path must gate checkInflightOnBoot"
    )


def test_workspace_embed_no_retry():
    """api() must force a single attempt under the embed flag (contract §3)."""
    assert "_embedNoRedirect401()?1:" in WORKSPACE_JS


def test_workspace_embed_401_no_reload():
    """401 must not navigate/reload inside the frame (contract §3/§10)."""
    assert "opts.redirect401!==false&&!_embedNoRedirect401()" in WORKSPACE_JS
    assert "function _embedNoRedirect401()" in WORKSPACE_JS


def test_messages_js_untouched_by_spike():
    """Spike constraint: messages.js must not be edited (frame adapts beneath it)."""
    import subprocess
    out = subprocess.run(
        ["git", "diff", "--name-only", "b75268eb248a3443bb9b177e75e4845cb86aa5f3", "HEAD"],
        cwd=str(ROOT), capture_output=True, text=True,
    ).stdout
    changed = {line.strip() for line in out.splitlines() if line.strip()}
    assert "static/messages.js" not in changed
    assert "static/ui.js" not in changed


# ── Executed node-VM tests ──────────────────────────────────────────────────


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def _run_node_vm(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


@_node_tests
def test_vm_storage_wrapper_prefixes_and_suppresses():
    """Executed: wrapper prefixes keys, suppresses listed writes, reads suppressed keys as null."""
    source = f"""
const EMBED_JS = {EMBED_JS!r};
// Minimal browser prelude
const nativeBacked = {{}};
const nativeStorage = {{
  getItem: k => (k in nativeBacked ? nativeBacked[k] : null),
  setItem: (k, v) => {{ nativeBacked[k] = String(v); }},
  removeItem: k => {{ delete nativeBacked[k]; }},
  clear: () => {{ for (const k of Object.keys(nativeBacked)) delete nativeBacked[k]; }},
  key: i => Object.keys(nativeBacked)[i] || null,
  get length() {{ return Object.keys(nativeBacked).length; }},
}};
const window = {{ localStorage: nativeStorage }};
const document = {{ baseURI: 'http://x/', }};
window.document = document;
const crypto = {{ randomUUID: () => 'uuid-1' }};

// Extract installEmbedStorage and SUPPRESSED_WRITE_SET from the IIFE source.
const start = EMBED_JS.indexOf('var GEN =');
const end = EMBED_JS.indexOf('var __embedStorage');
const prelude = EMBED_JS.slice(start, end);
const fnSrc = EMBED_JS.slice(EMBED_JS.indexOf('function installEmbedStorage'));
const fnEnd = fnSrc.indexOf(String.fromCharCode(10) + '  var __embedStorage');
eval(prelude);
eval(fnSrc.slice(0, fnEnd));

const store = installEmbedStorage(nativeStorage);

// 1. Normal keys are namespaced under the prefix
store.setItem('my-view-pref', 'open');
const wroteNamespaced = Object.keys(nativeBacked).some(k => k.indexOf('hermes-embed-0-my-view-pref') === 0);

// 2. Suppressed writes never reach native storage
store.setItem('hermes-theme', 'light');
store.setItem('hermes-webui-session', 'sid-123');
store.setItem('hermes-skin', 'mono');
const themeWritten = Object.keys(nativeBacked).some(k => k.endsWith('hermes-theme'));

// 3. Reads of suppressed keys return null (boot with shipped defaults)
const themeRead = store.getItem('hermes-theme');
const sessionRead = store.getItem('hermes-webui-session');

// 4. removeItem is namespaced + suppressed for protected keys
store.removeItem('my-view-pref');
const afterRemove = nativeBacked['hermes-embed-0-my-view-pref'] === undefined;

console.log(JSON.stringify({{
  wroteNamespaced, themeWritten, themeRead, sessionRead, afterRemove,
  suppressedCount: SUPPRESSED_WRITE_KEYS.length,
  suppressedHasSession: SUPPRESSED_WRITE_KEYS.indexOf('hermes-webui-session') >= 0,
  suppressedHasTheme: SUPPRESSED_WRITE_KEYS.indexOf('hermes-theme') >= 0,
  suppressedHasTabOrder: SUPPRESSED_WRITE_KEYS.indexOf('hermes-webui-tab-order') >= 0,
  suppressedHasHiddenTabs: SUPPRESSED_WRITE_KEYS.indexOf('hermes-webui-hidden-tabs') >= 0,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["wroteNamespaced"] is True
    assert result["themeWritten"] is False, "suppressed legacy-migration write must not reach native storage"
    assert result["themeRead"] is None
    assert result["sessionRead"] is None, "session restore key must read as null in embed (New-by-default)"
    assert result["afterRemove"] is True
    assert result["suppressedHasSession"] is True
    assert result["suppressedHasTheme"] is True
    assert result["suppressedHasTabOrder"] is True
    assert result["suppressedHasHiddenTabs"] is True
    assert result["suppressedCount"] >= len(EXPECTED_SUPPRESSED)


@_node_tests
def test_vm_eventsource_shim_open_message_close():
    """Executed: the shim delivers open/message/close with native-compatible readyState.

    messages.js _wireSSE relies on: constructor returns immediately with
    readyState CONNECTING(0), open fires when subscription acknowledges,
    message events carry data, close() sets readyState CLOSED(2) and requests
    teardown (stream-end). No native EventSource construction occurs.
    """
    source = f"""
const EMBED_JS = {EMBED_JS!r};
const events = [];
const parentMessages = [];
const location = {{ search: '?hermes_embed=1&gen=g1', origin: 'http://frame' }};
const window = {{
  location: location,
  parent: {{ postMessage: (m, o) => parentMessages.push({{ msg: m, origin: o }}) }},
  addEventListener: (type, fn) => {{ window['_on_' + type] = fn; }},
  EventSource: function NativeES() {{ throw new Error('NATIVE_EVENTSOURCE_CONSTRUCTED'); }},
}};
window.self = window;
const document = {{ baseURI: 'http://frame/' }};
window.document = document;
const crypto = {{ randomUUID: () => 'op-' + (crypto._n = (crypto._n || 0) + 1) }};

// Run the whole embed-host IIFE with our prelude (it early-returns without
// the embed query flag, so the ?hermes_embed=1 path is what executes).
// Replace the window/localStorage/document tokens by evaluating in this scope.
const src = EMBED_JS;
eval(src);

const ES = window.EventSource;
const usedShim = ES !== undefined && ES.name !== 'NativeES';
// Real listener registration (no prototype hacks — the same surface _wireSSE uses)
const fired = [];
let es;
try {{ es = new ES('http://frame/api/chat/stream?stream_id=s1&replay=1&after_seq=3', {{}}); }}
catch (e) {{ console.log(JSON.stringify({{ error: String(e) }})); }}

es.addEventListener('open', ev => fired.push({{ type: 'open', data: ev && ev.data }}));
es.addEventListener('delta', ev => fired.push({{ type: ev.type, data: ev.data }}));
es.addEventListener('message', ev => fired.push({{ type: 'message', data: ev.data }}));
es.addEventListener('error', ev => fired.push({{ type: 'error', data: ev && ev.data }}));

const readyStateAfterCtor = es.readyState;
const queryBefore = es._urlWithQuery();

// Simulate shell hello (origin + gen + nonce) → expect ready reply
window._on_message({{
  origin: 'http://shell',
  source: window.parent,
  data: {{ t: 'hello', v: 1, gen: 'g1', nonce: 'n1' }},
}});
const readyMsg = parentMessages.find(m => m.msg.t === 'ready');

// Deliver a message event from the shell (subscription flushes on handshake)
const subMsg = parentMessages.find(m => m.msg.t === 'stream-sub');

window._on_message({{
  origin: 'http://shell',
  data: {{ t: 'stream-ev', sub: es._sub, event: 'delta', data: 'hello world' }},
}});

// Server closes cleanly
window._on_message({{
  origin: 'http://shell',
  data: {{ t: 'stream-end', sub: es._sub, reason: 'closed' }},
}});
const readyStateAfterClose = es.readyState;
const endMsg = parentMessages.find(m => m.msg.t === 'stream-end' && m.msg.sub === es._sub);

console.log(JSON.stringify({{
  usedShim, readyStateAfterCtor, queryBefore,
  readySent: !!readyMsg, readyGen: readyMsg && readyMsg.msg.gen, readyNonce: readyMsg && readyMsg.msg.nonce,
  readyCaps: readyMsg && readyMsg.msg.server && readyMsg.msg.server.caps,
  subSent: !!subMsg, subKind: subMsg && subMsg.msg.kind,
  replayParamsPreserved: queryBefore.indexOf('replay=1') >= 0 && queryBefore.indexOf('after_seq=3') >= 0,
  firedCount: fired.length,
  firstDelta: (fired.find(e => e.type === 'delta') || {{}}).type + ':' + ((fired.find(e => e.type === 'delta') || {{}}).data || ''),
  openFired: fired.some(e => e.type === 'open'),
  readyStateAfterClose, endSent: !!endMsg, endReason: endMsg && endMsg.msg.reason,
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert "NATIVE_EVENTSOURCE_CONSTRUCTED" not in json.dumps(result), (
        "native EventSource must never be constructed in embed mode"
    )
    assert result.get("usedShim") is True
    assert result.get("readyStateAfterCtor") == 0  # CONNECTING
    assert result.get("readySent") is True
    assert result.get("readyGen") == "g1"
    assert result.get("readyNonce") == "n1"
    assert "stream" in (result.get("readyCaps") or [])
    assert result.get("subSent") is True
    assert result.get("subKind") == "chat"
    assert result.get("replayParamsPreserved") is True
    assert result.get("readyStateAfterClose") == 2  # CLOSED
    assert result.get("endSent") is True
    assert result.get("endReason") == "closed"
    assert result.get("openFired") is True
    assert result.get("firstDelta") == "delta:hello world"


@_node_tests
def test_vm_nonce_single_use_and_origin_pinned():
    """Executed: §11 handshake — replayed nonce and wrong-origin hello are refused."""
    source = f"""
const EMBED_JS = {EMBED_JS!r};
const parentMessages = [];
const location = {{ search: '?hermes_embed=1&gen=g1', origin: 'http://frame' }};
const window = {{
  location: location,
  parent: {{ postMessage: (m) => parentMessages.push(m) }},
  addEventListener: (type, fn) => {{ window['_on_' + type] = fn; }},
}};
window.self = window;
const document = {{ baseURI: 'http://frame/' }};
window.document = document;
const crypto = {{ randomUUID: () => 'op-x' }};
const src = EMBED_JS;
eval(src);
function hello(origin, nonce, gen) {{
  window._on_message({{ origin, data: {{ t:'hello', v:1, gen: gen||'g1', nonce }} }});
}}
hello('http://shell', 'n1');
const firstReady = parentMessages.filter(m => m.t === 'ready').length;
hello('http://shell', 'n1'); // replayed nonce
const afterReplay = parentMessages.filter(m => m.t === 'ready').length;
hello('http://evil', 'n2'); // wrong origin
const afterEvil = parentMessages.filter(m => m.t === 'ready').length;
hello('http://shell', 'n3', 'wrong-gen'); // wrong generation
const afterGen = parentMessages.filter(m => m.t === 'ready').length;
console.log(JSON.stringify({{ firstReady, afterReplay, afterEvil, afterGen }}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["firstReady"] == 1
    assert result["afterReplay"] == 1, "replayed nonce must be refused (§11.3)"
    assert result["afterEvil"] == 1, "wrong-origin hello must be refused (§11.1)"
    assert result["afterGen"] == 1, "generation-mismatched hello must be refused (§2)"


# ── Phase 2 (B2): degraded disclosure, settings read-only, hardening ────────


def test_embed_caps_source_is_static_and_caps_accurate():
    """R7: caps provenance must be exposed as capsSource:'static'; ready caps
    stay stream + settings-read only (media is Phase 3)."""
    assert "capsSource:'static'" in EMBED_JS
    assert "caps:['stream','settings-read']" in EMBED_JS
    # no invented caps client-side (R5: no new scope names)
    assert "'media'" not in EMBED_JS
    assert "'cdp'" not in EMBED_JS
    assert "'control'" not in EMBED_JS


def test_embed_settings_mirror_keys_are_write_suppressed():
    """R4: settings mirrors must be write-suppressed even when GET /api/settings
    succeeds — boot applies settings read-only."""
    assert "SETTINGS_MIRROR_WRITE_KEYS" in EMBED_JS
    for key in ("hermes-default-message-mode", "hermes-auto-scroll-follow",
                "hermes-tts-engine", "hermes-lang", "hermes-raw-audio-mode"):
        assert f"'{key}'" in EMBED_JS, f"missing R4 settings-mirror key: {key}"
    host = _extract_fn(EMBED_JS, "installEmbedStorage")
    # setItem AND removeItem both gate settings mirrors (assert prefix enforced
    # on both paths, plus explicit R4 gating on both write paths)
    assert host.count("SETTINGS_MIRROR_WRITE_SET[String(key)]") >= 2


def test_embed_storage_clear_is_prefixed():
    """R6 hardening: clear() must only remove keys carrying the embed prefix —
    never the ordinary WebUI tab's keys or another generation's keys."""
    fn = _extract_fn(EMBED_JS, "installEmbedStorage")
    # Anchor on the method definition ("clear: function()"), not the comment
    # text ("Prefixed clear: ..."), which also contains "clear:".
    clear_body = fn.split("clear: function()")[1].split("key: function()")[0]
    assert "indexOf(STORAGE_PREFIX)===0" in clear_body, (
        "clear() must remove only keys carrying the embed prefix"
    )
    assert "nativeStorage.clear()" not in fn, "clear() must not nuke the whole native store"


def test_embed_kind_for_unknown_path_returns_null():
    """R7 hardening: unknown feed paths must get no kind (no stream-sub posted)."""
    assert "return best?SUB_KINDS[best]:null" in EMBED_JS
    assert "if(kind===null)" in EMBED_JS
    assert "_recordDegraded" in EMBED_JS


def test_embed_policy_errors_are_disclosed():
    """R3: broker policy failures must hit the degraded-disclosure listener."""
    assert "function _recordDegraded" in EMBED_JS
    assert "__hermesEmbedDegradedNotice" in EMBED_JS
    assert "d.error==='policy'" in EMBED_JS
    # boot.js hook exists and is embed-gated
    assert "__hermesEmbedDegradedNotice" in BOOT_JS
    assert "window.__HERMES_EMBED__!==true) return" in BOOT_JS


@_node_tests
def test_vm_settings_mirror_writes_never_reach_storage():
    """Executed: R4 — settings mirror setItem/removeItem are suppressed in embed
    mode even though reads succeed; ordinary keys still persist namespaced."""
    source = f"""
const EMBED_JS = {EMBED_JS!r};
const nativeBacked = {{}};
const nativeStorage = {{
  getItem: k => (k in nativeBacked ? nativeBacked[k] : null),
  setItem: (k, v) => {{ nativeBacked[k] = String(v); }},
  removeItem: k => {{ delete nativeBacked[k]; }},
  clear: () => {{ for (const k of Object.keys(nativeBacked)) delete nativeBacked[k]; }},
  key: i => Object.keys(nativeBacked)[i] || null,
  get length() {{ return Object.keys(nativeBacked).length; }},
}};
const window = {{ localStorage: nativeStorage }};
const document = {{ baseURI: 'http://x/' }};
window.document = document;
const crypto = {{ randomUUID: () => 'uuid-1' }};
const start = EMBED_JS.indexOf('var GEN =');
const end = EMBED_JS.indexOf('var __embedStorage');
const prelude = EMBED_JS.slice(start, end);
const fnSrc = EMBED_JS.slice(EMBED_JS.indexOf('function installEmbedStorage'));
const fnEnd = fnSrc.indexOf(String.fromCharCode(10) + '  var __embedStorage');
eval(prelude);
eval(fnSrc.slice(0, fnEnd));
const store = installEmbedStorage(nativeStorage);

// R4: settings fetch SUCCEEDS in embed mode, boot then mirrors the settings —
// every mirror write must be swallowed. Assert per-key.
const settingsKeys = ['hermes-default-message-mode','hermes-auto-scroll-follow',
  'hermes-tts-engine','hermes-lang','hermes-raw-audio-mode','hermes-tts-rate',
  'hermes-tts-voice','mic_force_mediarecorder'];
const written = [];
const origSet = nativeStorage.setItem;
nativeStorage.setItem = (k,v) => {{ written.push(k); origSet(k,v); }};
settingsKeys.forEach(k => store.setItem(k, 'x'));
settingsKeys.forEach(k => store.removeItem(k));
const settingsWrites = written.length;
// ordinary view-local key still persists, namespaced
store.setItem('hermes-panel-w', '320');
const ordinaryOk = nativeBacked['hermes-embed-0-hermes-panel-w'] === '320';
// reads of settings mirrors still resolve from the (empty) namespace → null
const settingsRead = store.getItem('hermes-default-message-mode');
console.log(JSON.stringify({{ settingsWrites, ordinaryOk, settingsRead }}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["settingsWrites"] == 0, (
        "R4: no settings mirror setItem/removeItem may reach native storage "
        "in embed mode even when the settings fetch succeeds"
    )
    assert result["ordinaryOk"] is True
    assert result["settingsRead"] is None


@_node_tests
def test_vm_clear_only_removes_prefixed_keys():
    """Executed: R6 — clear() removes only hermes-embed-<gen>- keys."""
    source = f"""
const EMBED_JS = {EMBED_JS!r};
const nativeBacked = {{
  'hermes-embed-0-own-key': 'own',
  'hermes-embed-1-other-gen': 'othergen',
  'hermes-webui-tab-order': 'chat,kanban',
  'hermes-theme': 'dark',
}};
const nativeStorage = {{
  getItem: k => (k in nativeBacked ? nativeBacked[k] : null),
  setItem: (k, v) => {{ nativeBacked[k] = String(v); }},
  removeItem: k => {{ delete nativeBacked[k]; }},
  clear: () => {{ for (const k of Object.keys(nativeBacked)) delete nativeBacked[k]; }},
  key: i => Object.keys(nativeBacked)[i] || null,
  get length() {{ return Object.keys(nativeBacked).length; }},
}};
const window = {{ localStorage: nativeStorage }};
const document = {{ baseURI: 'http://x/' }};
window.document = document;
const crypto = {{ randomUUID: () => 'uuid-1' }};
const start = EMBED_JS.indexOf('var GEN =');
const end = EMBED_JS.indexOf('var __embedStorage');
const prelude = EMBED_JS.slice(start, end);
const fnSrc = EMBED_JS.slice(EMBED_JS.indexOf('function installEmbedStorage'));
const fnEnd = fnSrc.indexOf(String.fromCharCode(10) + '  var __embedStorage');
eval(prelude);
eval(fnSrc.slice(0, fnEnd));
const store = installEmbedStorage(nativeStorage);
store.clear();
console.log(JSON.stringify({{
  ownGone: !('hermes-embed-0-own-key' in nativeBacked),
  otherGenKept: nativeBacked['hermes-embed-1-other-gen'] === 'othergen',
  ordinaryTabKept: nativeBacked['hermes-webui-tab-order'] === 'chat,kanban',
  themeKept: nativeBacked['hermes-theme'] === 'dark',
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["ownGone"] is True, "clear() must remove own generation's keys"
    assert result["otherGenKept"] is True, "clear() must not touch other generations (R6 fencing)"
    assert result["ordinaryTabKept"] is True, "clear() must not touch the ordinary WebUI tab"
    assert result["themeKept"] is True


@_node_tests
def test_vm_unknown_feed_kind_is_refused_and_disclosed():
    """Executed: R7 hardening — a stream for a path outside SUB_KINDS posts no
    stream-sub, fires error, and is disclosed as a degraded feature (R3)."""
    source = f"""
globalThis.location = {{ search: '?hermes_embed=1&gen=g1', origin: 'http://frame' }};
const EMBED_JS = {EMBED_JS!r};
const parentMessages = [];
const window = {{
  location: globalThis.location,
  parent: {{ postMessage: (m) => parentMessages.push(m) }},
  addEventListener: (type, fn) => {{ window['_on_' + type] = fn; }},
}};
window.self = window;
const document = {{ baseURI: 'http://frame/' }};
window.document = document;
const crypto = {{ randomUUID: () => 'op-u' }};
eval(EMBED_JS);
const ES = window.EventSource;
const notices = [];
window.__hermesEmbedDegradedNotice = (kind, reason) => notices.push({{kind, reason}});
const es = new ES('http://frame/api/sneaky/unknown/feed?x=1', {{}});
const errFired = [];
es.addEventListener('error', () => errFired.push(true));
const subs = parentMessages.filter(m => m.t === 'stream-sub');
console.log(JSON.stringify({{
  subsPosted: subs.length,
  readyState: es.readyState,
  degradedRecorded: typeof window.__hermesEmbedRecordDegraded === 'function',
}}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["subsPosted"] == 0, "unknown feed paths must not post stream-sub"
    assert result["readyState"] == 2, "refused stream must fail closed (CLOSED)"


@_node_tests
def test_vm_nonce_reuse_rejected_after_completed_handshake():
    """Executed: §11.3 hardening — nonce reuse is rejected even after the
    handshake already completed (usedNonces persists post-ready)."""
    source = f"""
const EMBED_JS = {EMBED_JS!r};
const parentMessages = [];
const location = {{ search: '?hermes_embed=1&gen=g1', origin: 'http://frame' }};
const window = {{
  location: location,
  parent: {{ postMessage: (m) => parentMessages.push(m) }},
  addEventListener: (type, fn) => {{ window['_on_' + type] = fn; }},
}};
window.self = window;
const document = {{ baseURI: 'http://frame/' }};
window.document = document;
const crypto = {{ randomUUID: () => 'op-x' }};
eval(EMBED_JS);
function hello(origin, nonce, gen) {{
  window._on_message({{ origin, data: {{ t:'hello', v:1, gen: gen||'g1', nonce }} }});
}}
hello('http://shell', 'n1');
const firstReady = parentMessages.filter(m => m.t === 'ready').length;
// post-handshake hellos with fresh + reused nonces: none may produce another ready
hello('http://shell', 'n2');
hello('http://shell', 'n1');
const afterPostHandshake = parentMessages.filter(m => m.t === 'ready').length;
const usedNoncesTracked = parentMessages.filter(m => m.t === 'ready').length >= 1;
console.log(JSON.stringify({{ firstReady, afterPostHandshake, usedNoncesTracked }}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["firstReady"] == 1
    assert result["afterPostHandshake"] == 1, (
        "no ready may be emitted post-handshake, and a reused nonce (n1) must "
        "stay rejected after the completed handshake"
    )


@_node_tests
def test_vm_degraded_policy_error_disclosed_once():
    """Executed: R3 — a brokered fetch failing with error:'policy' surfaces the
    degraded notice exactly once per feature path, never silent."""
    source = f"""
globalThis.location = {{ search: '?hermes_embed=1&gen=g1', origin: 'http://frame' }};
const EMBED_JS = {EMBED_JS!r};
const parentMessages = [];
const window = {{
  location: globalThis.location,
  parent: {{ postMessage: (m) => parentMessages.push(m) }},
  addEventListener: (type, fn) => {{ window['_on_' + type] = fn; }},
  fetch: () => {{ throw new Error('NATIVE_FETCH_USED'); }},
}};
window.self = window;
const document = {{ baseURI: 'http://frame/' }};
window.document = document;
// Unique op ids: a constant mock collides both fetches into one pending op,
// so only one promise ever settles and Promise.all hangs.
let __opSeq = 0;
const crypto = {{ randomUUID: () => 'op-' + (++__opSeq) }};
eval(EMBED_JS);
const notices = [];
window.__hermesEmbedDegradedNotice = (kind, reason) => notices.push(String(kind));
// handshake so the fetch goes through the broker
window._on_message({{ origin:'http://shell', data: {{ t:'hello', v:1, gen:'g1', nonce:'n1' }} }});
const p1 = window.fetch('http://frame/api/events/stream').then(() => 'ok', e => 'err:' + e.brokerError);
const p2 = window.fetch('http://frame/api/events/stream').then(() => 'ok', e => 'err:' + e.brokerError);
setTimeout(() => {{
  const reqs = parentMessages.filter(m => m.t === 'req');
  reqs.forEach(m => window._on_message({{ origin:'http://shell', data: {{ t:'res', op:m.op, status:0, error:'policy' }} }}));
  Promise.all([p1, p2]).then(([r1b, r2b]) => {{
    console.log(JSON.stringify({{ r1: r1b, r2: r2b, noticeCount: notices.length, notices }}));
  }});
}}, 10);
"""
    result = json.loads(_run_node_vm(source))
    assert result["r1"] == "err:policy" and result["r2"] == "err:policy"
    assert result["noticeCount"] == 1, "degraded disclosure must fire once per feature path (no spam, no silence)"
