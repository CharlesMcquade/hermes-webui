"""Middle-click (auxclick button 1) or Ctrl/Cmd+click on a sidebar session row
opens that session in a new browser tab instead of switching the current tab.

Covers the P1 from Greptile review on #7429 (modified clicks retaining
gesture state) via a regression lock, plus behavioral coverage the review
asked for: the new-tab helpers are extracted from the shipped
``static/sessions.js`` and driven through a Node VM that dispatches synthetic
pointer/aux/mouse events against a stub row and observes ``window.open``,
propagation, exclusions, and gesture cleanup.

Handler/gesture background: top-level ``.session-item`` rows swallow
non-left buttons (``onpointerup`` returns early for ``button !== 0`` and no
``auxclick`` handler existed), so middle-click did nothing or triggered
autoscroll. The fix wires ``_openSessionUrlInNewTab(sid)`` through the shared
``_consumeSessionNewTabClick`` choke point into all sidebar row kinds
(top-level rows, fork rows + main buttons, plain child buttons, lineage
segments) via ``auxclick`` (open) + ``mousedown`` (kill autoscroll) plus
Ctrl/Cmd+click on the tap paths, leaving right-click menu, select mode,
rename, swipe, and single-tap behavior untouched.
"""
import json
import shutil
import subprocess
from pathlib import Path

import tempfile

import pytest


REPO = Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    """Extract ``function name(...)`` with balanced braces (sync or plain)."""
    for prefix in (f"function {name}(", f"async function {name}("):
        start = source.find(prefix)
        if start >= 0:
            break
    else:
        raise AssertionError(f"{name} function not found in static/sessions.js")
    brace = source.find("{", start)
    assert brace >= 0, f"{name} opening brace not found"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    raise AssertionError(f"{name} function braces unbalanced")


def test_new_tab_helper_exists():
    """A shared `_openSessionUrlInNewTab(sid)` helper builds the deep link."""
    helper = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
    assert "_sessionUrlForSid(" in helper
    assert "window.open(" in helper
    assert "'_blank'" in helper or '"_blank"' in helper


def test_auxclick_wired_for_all_row_kinds():
    """Each row kind handles middle-click via the shared wiring helper."""
    # The auxclick/mousedown listeners live once in _wireSessionNewTabListeners;
    # every row kind (top-level .session-item, fork row, fork main button,
    # plain child button, lineage segment) must call the wirer.
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id, ()=>s)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(row, ()=>child.session_id, ()=>child)") == 2
    assert "_wireSessionNewTabListeners(mainBtn, ()=>child.session_id, ()=>child)" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(row, ()=>seg.session_id, ()=>seg)" in SESSIONS_JS
    assert SESSIONS_JS.count("_wireSessionNewTabListeners(") >= 6  # def + 5 call sites
    # All opens route through the two choke points with the concrete sid and
    # the concrete session (so the owning-profile gate can inspect the row).
    assert "_openSessionUrlInNewTab(getSid(), typeof getSession==='function'?getSession():undefined)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(sid, session)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession)" in SESSIONS_JS


def test_middle_mousedown_prevents_autoscroll():
    """`mousedown` on button 1 preventDefaults so the browser doesn't autoscroll."""
    assert "addEventListener('auxclick'" in SESSIONS_JS
    assert "addEventListener('mousedown'" in SESSIONS_JS
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert "button" in wire and "1" in wire
    assert "preventDefault()" in wire
    # Ctrl/Cmd+click on the tap paths also routes to the new-tab opener.
    assert "_consumeSessionNewTabClick(e, child.session_id, child)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in SESSIONS_JS


def test_ctrl_click_opens_new_tab():
    """Ctrl/Cmd+left-click on a row opens the deep link in a new tab."""
    assert "e.ctrlKey||e.metaKey" in SESSIONS_JS.replace(" ", "")


def test_action_menu_and_select_mode_untouched():
    """New-tab must not fire from the ⋮ menu, checkboxes, or select mode."""
    consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
    assert "_isSessionActionTarget" in consume
    assert "_sessionSelectMode" in consume
    assert "_renamingSid" in consume
    wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
    assert "_isSessionActionTarget" in wire
    assert "session-actions" in wire


def test_openChildSession_new_tab_flag():
    """Child-row programmatic path supports open-in-new-tab without a same-tab switch."""
    idx = SESSIONS_JS.index("const openChildSession=async(childSession,")
    window = SESSIONS_JS[idx:idx + 400]
    assert "newTab" in window
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession)" in window


def test_modified_click_cancels_pending_tap_before_new_tab():
    """P1 (#7429 review): the Ctrl/Cmd+click branch must clear the pending
    single-tap timer *before* opening the new tab.

    Without this, the deferred single-tap opener from the FIRST click of a
    fast modified double-click fires after the new tab opens and switches the
    current tab anyway — the exact stale-state class the review flagged.
    """
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    window = SESSIONS_JS[idx:idx + 2400]
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in window
    clear_idx = window.index("clearTimeout(_tapTimer)")
    consume_idx = window.index("_consumeSessionNewTabClick(e, s.session_id, s)")
    assert clear_idx < consume_idx, (
        "pending-tap cancel must run before the new-tab open, not after"
    )
    assert "_lastTapTime=0" in window.replace(" ", "")
    assert "_tapTimer=null" in window.replace(" ", "")
    # The row's gesture machine was armed by pointerdown before this
    # pointerup fired, and a pen drag may have painted swipe offsets
    # (mouse never paints: `_isSessionSwipeTarget` excludes mouse). The
    # branch must settle via the shared `_clearPointerDragState()` choke
    # point BEFORE the early return — parking `_gestureState` alone would
    # leave pen-painted offsets displaced and the `dragging` class stuck.
    assert "_clearPointerDragState()" in window
    assert window.index("_clearPointerDragState()") < consume_idx
    # Select/rename gate: the branch must not touch gesture state when the
    # new-tab consumer refuses the event (select mode / mid-rename), or the
    # fall-through _finishSessionGesture early-returns on 'idle' and the row
    # (de)select toggle never runs.
    compact = window.replace(" ", "")
    assert "!_sessionSelectMode" in compact
    assert "!_renamingSid" in compact


# ── Behavioral tests via Node VM ─────────────────────────────────────────────

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_VM_PRELUDE = r"""
const ret = {};
const sandbox = { opened: null, openCalls: 0, stopped: 0, prevented: 0,
  _sessionSelectMode: false, _renamingSid: null,
  _isSessionActionTarget: () => false,
  mkEvent: null };
sandbox.window = { open: (u, t, f) => { sandbox.opened = { u, t, f }; sandbox.openCalls++; return null; } };
sandbox.document = { baseURI: 'http://127.0.0.1:8787/' };
sandbox.location = { href: 'http://127.0.0.1:8787/', pathname: '/', search: '', hash: '', origin: 'http://127.0.0.1:8787' };
sandbox.window.location = sandbox.location;
sandbox.URL = URL; sandbox.URLSearchParams = URLSearchParams; sandbox.encodeURIComponent = encodeURIComponent;
sandbox.mkEvent = (over) => Object.assign(
  { button: 0, ctrlKey: false, metaKey: false, target: null,
    preventDefault() { sandbox.prevented++; }, stopPropagation() { sandbox.stopped++; } }, over || {});
const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(params.helpers, sandbox);
vm.runInContext('var mkEvent = this.mkEvent; var window = this.window; var document = this.document;', sandbox);
const sid = 'test-session-123';
"""


def _run_node(payload: dict) -> dict:
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + _VM_PRELUDE
        + payload["driver"]
    )
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _helpers() -> str:
    return "\n".join(
        _extract_function(SESSIONS_JS, name)
        for name in (
            "_sessionUrlForSid",
            "_newTabOwningProfileAllowed",
            "_openSessionUrlInNewTab",
            "_consumeSessionNewTabClick",
        )
    )


def _pointerup_branch() -> str:
    """The Ctrl/Cmd branch of the top-level onpointerup, verbatim."""
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    end = SESSIONS_JS.index(
        "if(_finishSessionGesture(e.clientX,e.clientY,e.target,e.pointerType))",
        idx,
    )
    return SESSIONS_JS[idx:end]


class TestNewTabBehavior:
    def test_helper_opens_deep_link_blank(self):
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
const opened0 = vm.runInContext(`_openSessionUrlInNewTab("test-session-123")`, sandbox);
ret.opened = sandbox.opened; ret.calls = sandbox.openCalls; ret.ret = opened0;
console.log(JSON.stringify(ret));
""",
        })
        assert out["calls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["opened"]["t"] == "_blank"

    def test_middle_click_consumed_plain_click_passes_through(self):
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
const mid = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:null}), "test-session-123")`, sandbox);
const midOpened = sandbox.opened;
sandbox.opened = null; sandbox.openCalls = 0;
const plain = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:0,target:null}), "test-session-123")`, sandbox);
ret.mid = mid; ret.midOpened = !!midOpened; ret.plain = plain; ret.plainOpened = !!sandbox.opened;
console.log(JSON.stringify(ret));
""",
        })
        assert out["mid"] is True and out["midOpened"] is True
        assert out["plain"] is False and out["plainOpened"] is False

    def test_ctrl_click_consumed_select_mode_and_menu_blocked(self):
        out = _run_node({
            "helpers": _helpers(),
            "driver": r"""
const ctrl = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:0,ctrlKey:true,target:null}), "test-session-123")`, sandbox);
const ctrlOpened = !!sandbox.opened;
sandbox.opened = null;
vm.runInContext(`_sessionSelectMode = true;`, sandbox);
const blockedSelect = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:null}), "test-session-123")`, sandbox);
vm.runInContext(`_sessionSelectMode = false; _isSessionActionTarget = () => true;`, sandbox);
const blockedMenu = vm.runInContext(
  `_consumeSessionNewTabClick(mkEvent({button:1,target:{}}), "test-session-123")`, sandbox);
ret.ctrl = ctrl; ret.ctrlOpened = ctrlOpened;
ret.blockedSelect = blockedSelect; ret.blockedMenu = blockedMenu;
ret.stillClosed = !sandbox.opened;
console.log(JSON.stringify(ret));
""",
        })
        assert out["ctrl"] is True and out["ctrlOpened"] is True
        assert out["blockedSelect"] is False
        assert out["blockedMenu"] is False
        assert out["stillClosed"] is True

    def test_modified_pointerup_cancels_pending_tap(self):
        """Execute the real pointerup branch: pending tap cleared, tab opened,
        and the same-tab gesture finisher never runs.

        The branch ends in a bare ``return`` (it lives inside the row's
        ``onpointerup`` closure), so the harness rewrites the verbatim
        ``if(_consume...) return;`` tail to capture the observation object
        into ``globalThis`` *before* returning, then observes the timer ref,
        opened tab, and finisher flag. The branch and helpers travel via
        temp files (instead of nested ``json.dumps`` string concatenation)
        to keep quoting levels manageable.
        """
        branch = _pointerup_branch()
        consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
        opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
        urlfn = _extract_function(SESSIONS_JS, "_sessionUrlForSid")
        # The branch under test runs inside the row's gesture closure, so the
        # harness must stub every closure free-var the branch touches:
        # _tapTimer/_lastTapTime (pending tap), _clearPointerDragState
        # (gesture settle choke point), el (row node), and the new-tab choke
        # point. Missing stubs surface as ReferenceError here by design —
        # that is exactly the CI failure the maintainer reported.
        assert "_clearPointerDragState()" in branch
        driver = (
            "const fs = require('fs');\n"
            "const branchSrc = fs.readFileSync("
            + json.dumps("BRANCH_FILE") + ", 'utf8');\n"
            "const params = { urlSrc: fs.readFileSync("
            + json.dumps("URL_FILE") + ", 'utf8'),\n"
            "  openSrc: fs.readFileSync("
            + json.dumps("OPEN_FILE") + ", 'utf8'),\n"
            "  consumeSrc: fs.readFileSync("
            + json.dumps("CONSUME_FILE") + ", 'utf8') };\n"
            + r"""
const ret = {};
const ref = { v: 'PENDING-TAP' };
const runnerSrc =
  'let _tapTimer = ref.v; let _lastTapTime = 111;' +
  'const clearTimeout = (id) => { if (id === _tapTimer) { _tapTimer = null; ref.v = null; } };' +
  'const e = { button: 0, ctrlKey: true, metaKey: false, target: null, preventDefault() {}, stopPropagation() {} };' +
  'const s = { session_id: "test-session-123" };' +
  'const _sessionUrlForSid = ' + params.urlSrc + ';' +
  'const _openSessionUrlInNewTab = ' + params.openSrc + ';' +
  'const _consumeSessionNewTabClick = ' + params.consumeSrc + ';' +
  'let opened = null; const window = { open: (u,t,f) => { opened = {u,t,f}; return null; } };' +
  'window.location = { href: "http://127.0.0.1:8787/", pathname: "/", search: "", hash: "", origin: "http://127.0.0.1:8787" };' +
  'const doc = { baseURI: "http://127.0.0.1:8787/" };' +
  'const _sessionSelectMode = false; const _renamingSid = null;' +
  'const _isSessionActionTarget = () => false;' +
  // Row is owned by the active profile (single-profile path): the new-tab
  // owning-profile gate is a no-op here; it is exercised in
  // TestMaintainerFollowUps.
  'const _newTabOwningProfileAllowed = () => true;' +
  'let finisherRan = false;' +
  'const _finishSessionGesture = () => { finisherRan = true; return false; };' +
  // Pen-drag-painted row state: the gesture is mid-drag with swipe tracking
  // on (i.e. _paintSessionSwipe already ran and set the offset CSS vars).
  // The choke-point stub below mirrors the shipped _clearPointerDragState
  // (idle + long-press disarm + settle swipe paint when a drag was in
  // flight) so the test observes the same settlement the row gets.
  'let _gestureState = "dragging"; let _swipeTracking = true;' +
  'let _longPressMenuOpened = false;' +
  'let longPressCleared = false; let settleCalls = 0;' +
  'const removedClasses = [];' +
  'const _clearLongPressTimer = () => { longPressCleared = true; };' +
  'const _settleSessionSwipePaint = () => { settleCalls++; removedClasses.push("dragging"); };' +
  'const _clearPointerDragState = () => {' +
  '  const wasDragging = _gestureState === "dragging" || _swipeTracking;' +
  '  _gestureState = "idle"; _clearLongPressTimer();' +
  '  if(wasDragging){ settleCalls++; removedClasses.push("dragging"); }' +
  '};' +
  'let loadingRemoved = false;' +
  'const el = { classList: { remove(c) { if(c === "loading") loadingRemoved = true; } } };' +
  branchSrc.replace(/(\W)document(\W)/g, '$1doc$2')
  .replace(/if\(_consumeSessionNewTabClick\(e, s\.session_id, s\)\) return;/,
    'if(_consumeSessionNewTabClick(e, s.session_id, s)){ globalThis.__capture = { tapTimer: _tapTimer, ref: ref.v, lastTap: _lastTapTime, opened: opened, loadingRemoved: loadingRemoved, finisherRan: finisherRan, gestureState: _gestureState, settleCalls: settleCalls, longPressCleared: longPressCleared }; }') +
  '; globalThis.__capture = globalThis.__capture || { tapTimer: _tapTimer, ref: ref.v, lastTap: _lastTapTime, opened: opened, loadingRemoved: loadingRemoved, finisherRan: finisherRan, gestureState: _gestureState, settleCalls: settleCalls, longPressCleared: longPressCleared };';
try {
  new Function('ref', runnerSrc)(ref);
  ret.out = globalThis.__capture;
  delete globalThis.__capture;
} catch (err) { ret.error = String(err && err.message || err); }
console.log(JSON.stringify(ret));
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "BRANCH_FILE": branch,
                "URL_FILE": urlfn,
                "OPEN_FILE": opener,
                "CONSUME_FILE": consume,
            }
            concreto = driver
            for key, content in files.items():
                path = str(Path(tmp) / (key.lower() + ".js"))
                Path(path).write_text(content, encoding="utf-8")
                concreto = concreto.replace(json.dumps(key), json.dumps(path))
            r = subprocess.run([NODE, "-e", concreto],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f"node failed: {r.stderr}")
            out = json.loads(r.stdout.strip().splitlines()[-1])
        assert "error" not in out, out.get("error")
        assert out["out"]["tapTimer"] is None
        assert out["out"]["ref"] is None
        assert out["out"]["lastTap"] == 0
        assert out["out"]["opened"] == {"u": "/session/test-session-123", "t": "_blank", "f": "noopener"}
        assert out["out"]["loadingRemoved"] is True
        assert out["out"]["finisherRan"] is False
        # Gesture settled via the shared choke point before the return: parked
        # to idle, swipe-paint settle ran for the in-flight pen drag,
        # long-press disarmed. (Row starts `dragging` + swipe-tracking to
        # emulate the pen-drag-painted state the maintainer identified.)
        assert out["out"]["gestureState"] == "idle"
        assert out["out"]["settleCalls"] >= 1
        assert out["out"]["longPressCleared"] is True

    def test_select_mode_ctrl_click_skips_branch_and_runs_finisher(self):
        """Select-mode regression: Ctrl+click must NOT mutate gesture state.

        _consumeSessionNewTabClick refuses select mode, so the pointerup
        branch must be skipped entirely — gesture stays 'pressing' and the
        fall-through _finishSessionGesture runs (row toggles). Before the
        select/rename gate, the branch parked state to 'idle' first and the
        finisher early-returned, breaking Ctrl+click (de)select.
        """
        branch = _pointerup_branch()
        consume = _extract_function(SESSIONS_JS, "_consumeSessionNewTabClick")
        opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
        urlfn = _extract_function(SESSIONS_JS, "_sessionUrlForSid")
        # The branch under test runs inside the row's gesture closure, so the
        # harness must stub every closure free-var the branch touches.
        # Select mode ON: _consumeSessionNewTabClick refuses the event, so
        # the branch must be skipped — no choke, no timer touch — and the
        # appended verbatim finisher tail must run with state intact.
        assert "_sessionSelectMode" in branch
        driver = (
            "const fs = require('fs');\n"
            "const branchSrc = fs.readFileSync("
            + json.dumps("BRANCH_FILE") + ", 'utf8');\n"
            "const params = { urlSrc: fs.readFileSync("
            + json.dumps("URL_FILE") + ", 'utf8'),\n"
            "  openSrc: fs.readFileSync("
            + json.dumps("OPEN_FILE") + ", 'utf8'),\n"
            "  consumeSrc: fs.readFileSync("
            + json.dumps("CONSUME_FILE") + ", 'utf8') };\n"
            + r"""
const ret = {};
const ref = { v: null };
const runnerSrc =
  'let _tapTimer = null; let _lastTapTime = 0;' +
  'const clearTimeout = (id) => {};' +
  'const e = { button: 0, ctrlKey: true, metaKey: false, clientX: 10, clientY: 20, pointerType: "mouse", target: null, preventDefault() {}, stopPropagation() {} };' +
  'const s = { session_id: "test-session-123" };' +
  'const _sessionUrlForSid = ' + params.urlSrc + ';' +
  'const _openSessionUrlInNewTab = ' + params.openSrc + ';' +
  'const _consumeSessionNewTabClick = ' + params.consumeSrc + ';' +
  'let opened = null; const window = { open: (u,t,f) => { opened = {u,t,f}; return null; } };' +
  'window.location = { href: "http://127.0.0.1:8787/", pathname: "/", search: "", hash: "", origin: "http://127.0.0.1:8787" };' +
  'const doc = { baseURI: "http://127.0.0.1:8787/" };' +
  'const _sessionSelectMode = true; const _renamingSid = null;' +
  'const _isSessionActionTarget = () => false;' +
  'const _newTabOwningProfileAllowed = () => true;' +
  'let finisherRan = false;' +
  'const _finishSessionGesture = () => { finisherRan = true; return true; };' +
  'let _gestureState = "pressing"; let _swipeTracking = false;' +
  'let _longPressMenuOpened = false;' +
  'let chokeRan = false;' +
  'const _clearLongPressTimer = () => {};' +
  'const _settleSessionSwipePaint = () => {};' +
  'const _clearPointerDragState = () => { chokeRan = true; _gestureState = "idle"; };' +
  'const el = { classList: { remove(c) {} } };' +
  // Verbatim branch (ends before the finisher tail), then the real
  // fall-through tail re-attached so the skip path is exercised for real.
  branchSrc.replace(/(\W)document(\W)/g, '$1doc$2') +
  '; if(_finishSessionGesture(e.clientX,e.clientY,e.target,e.pointerType)) { globalThis.__stopCalled = true; }' +
  '; globalThis.__capture = { opened: opened, finisherRan: finisherRan, gestureState: _gestureState, chokeRan: chokeRan, stopCalled: !!globalThis.__stopCalled };';
try {
  new Function('ref', runnerSrc)(ref);
  ret.out = globalThis.__capture;
  delete globalThis.__capture;
  delete globalThis.__stopCalled;
} catch (err) { ret.error = String(err && err.message || err); }
console.log(JSON.stringify(ret));
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "BRANCH_FILE": branch,
                "URL_FILE": urlfn,
                "OPEN_FILE": opener,
                "CONSUME_FILE": consume,
            }
            concreto = driver
            for key, content in files.items():
                path = str(Path(tmp) / (key.lower() + ".js"))
                Path(path).write_text(content, encoding="utf-8")
                concreto = concreto.replace(json.dumps(key), json.dumps(path))
            r = subprocess.run([NODE, "-e", concreto],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f"node failed: {r.stderr}")
            out = json.loads(r.stdout.strip().splitlines()[-1])
        assert "error" not in out, out.get("error")
        assert out["out"]["opened"] is None
        assert out["out"]["finisherRan"] is True
        assert out["out"]["gestureState"] == "pressing"
        assert out["out"]["chokeRan"] is False

    def test_wirer_opens_on_auxclick_and_swallows_mousedown(self):
        """The shared wirer (single choke point for all row kinds): auxclick
        button-1 opens the tab; mousedown button-1 preventDefaults (no
        autoscroll) without opening; other buttons ignored."""
        wire = _extract_function(SESSIONS_JS, "_wireSessionNewTabListeners")
        out = _run_node({
            "helpers": _helpers(),
            "driver": (
                "const wireSrc = " + json.dumps(wire) + r""";
const seen = {};
const node = { addEventListener: (t, fn) => { seen[t] = fn; } };
const getSid = () => "test-session-123";
const mkLocalEvent = (over) => Object.assign(
  { button: 0, ctrlKey: false, metaKey: false, target: null,
    preventDefault() { sandbox.prevented++; }, stopPropagation() { sandbox.stopped++; } }, over || {});
const runner = new Function('node', 'getSid', 'window', 'document',
  '_sessionSelectMode', '_renamingSid', '_isSessionActionTarget',
  '_openSessionUrlInNewTab', '_sessionUrlForSid', '_consumeSessionNewTabClick',
  wireSrc + '; _wireSessionNewTabListeners(node, getSid);');
runner(node, getSid, sandbox.window, sandbox.document, false, null, () => false,
  sandbox._openSessionUrlInNewTab, sandbox._sessionUrlForSid, sandbox._consumeSessionNewTabClick);
ret.hasAux = typeof seen['auxclick'] === 'function';
ret.hasDown = typeof seen['mousedown'] === 'function';
// auxclick middle button opens
seen['auxclick'](mkLocalEvent({ button: 1, target: null }));
ret.auxOpened = !!sandbox.opened;
sandbox.opened = null; sandbox.openCalls = 0; sandbox.prevented = 0;
// mousedown middle button only swallows default
seen['mousedown'](mkLocalEvent({ button: 1, target: null }));
ret.downPrevented = sandbox.prevented === 1;
ret.downOpened = !!sandbox.opened;
// left auxclick ignored
seen['auxclick'](mkLocalEvent({ button: 0, target: null }));
ret.leftIgnored = sandbox.openCalls === 0;
console.log(JSON.stringify(ret));
"""
            ),
        })
        assert out["hasAux"] is True and out["hasDown"] is True
        assert out["auxOpened"] is True
        assert out["downPrevented"] is True and out["downOpened"] is False
        assert out["leftIgnored"] is True


# ── Maintainer follow-ups (nesquena-hermes CHANGES_REQUESTED, 2026-10-06) ─────
#
# The review gated head 85e6bf0fc and asked for one CORE fix (another profile's
# session must not open in a new tab) and three SHOULD-FIX gesture edges closed
# by a single extra condition on the Ctrl/Cmd pointerup branch. Each test below
# is a red-before lock: it executes the *shipped* branch (or the shipped
# handler text) verbatim, so it fails against the pre-follow-up revision.


def test_pointerup_branch_gates_on_gesture_origin_and_long_press():
    """[SHOULD-FIX] The Ctrl branch must require a press that began on this row
    (`_gestureState!=='idle'`) and no already-open pen long-press menu
    (`!_longPressMenuOpened`). Red-before: without both, a Ctrl-release over an
    untouched row, or on top of an open long-press menu, opened a tab."""
    idx = SESSIONS_JS.index("if((e.ctrlKey||e.metaKey)")
    condition = SESSIONS_JS[idx:SESSIONS_JS.index("{", idx)].replace(" ", "")
    assert "_sessionSelectMode" in condition
    assert "_renamingSid" in condition
    assert "_gestureState!=='idle'" in condition
    assert "!_longPressMenuOpened" in condition


def test_ctrl_double_click_does_not_start_rename():
    """[SHOULD-FIX #1] A Ctrl/Cmd+double-click is two modified clicks (two
    tabs); the dblclick handler must bail outside select mode instead of also
    renaming in the current tab. Red-before: it renamed."""
    idx = SESSIONS_JS.index("el.ondblclick=(e)=>{")
    handler = SESSIONS_JS[idx:idx + 500].replace(" ", "")
    assert "if((e.ctrlKey||e.metaKey)&&!_sessionSelectMode)return;" in handler


def test_open_session_url_consults_owning_profile():
    """[CORE] Opening a new tab is gated on the row's owning profile, and every
    call site threads the concrete session through so the gate can see it."""
    opener = _extract_function(SESSIONS_JS, "_openSessionUrlInNewTab")
    assert "_newTabOwningProfileAllowed(session)" in opener
    assert "session_new_tab_other_profile" in opener
    assert "_profileMatchesActiveProfile" in _extract_function(
        SESSIONS_JS, "_newTabOwningProfileAllowed")
    # Every row kind passes its session to the choke points.
    assert "_consumeSessionNewTabClick(e, s.session_id, s)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, child.session_id, child)" in SESSIONS_JS
    assert "_consumeSessionNewTabClick(e, seg.session_id, seg)" in SESSIONS_JS
    assert "_openSessionUrlInNewTab(childSession.session_id, childSession)" in SESSIONS_JS
    assert "_wireSessionNewTabListeners(el, ()=>s.session_id, ()=>s)" in SESSIONS_JS


# Executes the verbatim Ctrl/Cmd pointerup branch with caller-controlled
# closure state. Every free-var the branch reads is either a `ref` field
# (locals declared below) or a sandbox global passed in as a parameter.
_POINTERUP_BRANCH_BODY = (
    "let _tapTimer = ref.tapTimer; let _lastTapTime = ref.lastTap;"
    "let _gestureState = ref.gestureState; let _swipeTracking = ref.swipeTracking;"
    "let _longPressMenuOpened = ref.longPress;"
    "const clearTimeout = (id) => { if (id === _tapTimer) { _tapTimer = null; ref.tapTimer = null; } };"
    "const el = { classList: { remove(c) { ref.loadingRemoved = true; } } };"
    "const _clearLongPressTimer = () => { ref.longPressCleared = true; };"
    "const _settleSessionSwipePaint = () => { ref.settleCalls = (ref.settleCalls || 0) + 1; };"
    "const _clearPointerDragState = () => {"
    " const wasDragging = _gestureState === 'dragging' || _swipeTracking;"
    " _gestureState = 'idle'; _clearLongPressTimer(); ref.chokeRan = true;"
    " if (wasDragging) { _settleSessionSwipePaint(); } };"
    "const _finishSessionGesture = () => { ref.finisherRan = true; return !!ref.finisherRet; };"
    "const e = Object.assign({ button: ref.button, ctrlKey: ref.ctrlKey, metaKey: false,"
    " clientX: 10, clientY: 20, pointerType: 'mouse', target: null,"
    " preventDefault() {}, stopPropagation() {} }, ref.eventOver || {});"
    "const s = ref.session;"
    "__BRANCH__"
    "; ref.gestureStateAfter = _gestureState;"
    "; if (_finishSessionGesture(e.clientX, e.clientY, e.target, e.pointerType)) { ref.stopCalled = true; }"
)


def _run_branch_variant(*, gesture_state="dragging", swipe_tracking=True,
                        long_press=False, ctrl=True, select_mode=False,
                        renaming=None, finisher_ret=False, session=None,
                        show_all_profiles=None, active_profile="default",
                        event_over=None):
    """Drive the verbatim Ctrl/Cmd pointerup branch with a chosen closure state."""
    body = _POINTERUP_BRANCH_BODY.replace("__BRANCH__", _pointerup_branch())
    env = [
        "var _sessionSelectMode = %s;" % ("true" if select_mode else "false"),
        "var _renamingSid = %s;" % json.dumps(renaming),
    ]
    if show_all_profiles is not None:
        env += [
            "var _showAllProfiles = %s;" % ("true" if show_all_profiles else "false"),
            "var S = { activeProfile: %s };" % json.dumps(active_profile),
            "var _profileMatchesActiveProfile = function(p, a){"
            " var n = (typeof p === 'string' && p.trim()) ? p.trim() : 'default';"
            " var m = (typeof a === 'string' && a.trim()) ? a.trim() : 'default';"
            " return n === m; };",
            "var _sidebarSessionProfileName = function(x){"
            " return (x && typeof x.profile === 'string') ? x.profile.trim() : ''; };",
            "var showToast = function(msg){ globalThis.__toast = msg; };",
            "var t = function(k){ return 'T:' + k; };",
        ]
    ref = {
        "tapTimer": "PENDING-TAP", "lastTap": 111,
        "gestureState": gesture_state, "swipeTracking": swipe_tracking,
        "longPress": long_press, "button": 0, "ctrlKey": ctrl,
        "finisherRet": finisher_ret,
        "session": session or {"session_id": "test-session-123"},
    }
    if event_over:
        ref["eventOver"] = event_over
    driver = (
        "const makeRunner = new Function("
        "'window','document','_sessionUrlForSid','_newTabOwningProfileAllowed',"
        "'_openSessionUrlInNewTab','_consumeSessionNewTabClick',"
        "'_sessionSelectMode','_renamingSid','_isSessionActionTarget','ref',"
        + json.dumps(body) + ");\n"
        "vm.runInContext(" + json.dumps("\n".join(env)) + ", sandbox);\n"
        "const ref = " + json.dumps(ref) + ";\n"
        "sandbox.opened = null; sandbox.openCalls = 0;\n"
        "try {\n"
        "  makeRunner(sandbox.window, sandbox.document, sandbox._sessionUrlForSid,\n"
        "    sandbox._newTabOwningProfileAllowed, sandbox._openSessionUrlInNewTab,\n"
        "    sandbox._consumeSessionNewTabClick,\n"
        "    vm.runInContext('_sessionSelectMode', sandbox),\n"
        "    vm.runInContext('_renamingSid', sandbox),\n"
        "    sandbox._isSessionActionTarget, ref);\n"
        "} catch (err) { ret.error = String(err && err.message || err); }\n"
        "ret.opened = sandbox.opened; ret.openCalls = sandbox.openCalls;\n"
        "ret.toast = (typeof sandbox.__toast !== 'undefined') ? sandbox.__toast : null;\n"
        "ret.ref = ref;\n"
        "console.log(JSON.stringify(ret));\n"
    )
    return _run_node({"helpers": _helpers(), "driver": driver})


class TestMaintainerFollowUps:
    def test_ctrl_release_over_untouched_row_opens_nothing(self):
        """[SHOULD-FIX #2] A press that never began on this row (idle gesture)
        must not open a tab; the branch is skipped and the verbatim finisher
        early-returns. Red-before: any Ctrl-release over a row opened a tab."""
        out = _run_branch_variant(gesture_state="idle", swipe_tracking=False)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["ref"].get("chokeRan") is not True
        assert out["ref"]["gestureStateAfter"] == "idle"
        assert out["ref"]["finisherRan"] is True

    def test_pen_long_press_menu_then_ctrl_release_opens_nothing(self):
        """[SHOULD-FIX #3] With the pen long-press menu already open, a
        Ctrl-release must not stack a tab on top of it (Greptile P1).
        Red-before: the branch fired regardless of `_longPressMenuOpened`."""
        out = _run_branch_variant(gesture_state="pressing",
                                  swipe_tracking=False, long_press=True)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["ref"].get("chokeRan") is not True

    def test_ctrl_click_with_press_still_opens_one_tab(self):
        """Guard against over-blocking: a real Ctrl+click (press began, no
        long-press menu) still opens exactly one new tab."""
        out = _run_branch_variant(gesture_state="pressing", swipe_tracking=False)
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 1
        assert out["opened"]["u"] == "/session/test-session-123"
        assert out["opened"]["f"] == "noopener"
        assert out["ref"]["chokeRan"] is True

    def test_foreign_profile_session_refused_in_new_tab(self):
        """[CORE] With "show all profiles" on, a row owned by another profile is
        refused (no cookie switch, source tab stays valid): the gesture is
        consumed with a notice. Red-before: it opened a tab and 409'd the
        source tab's next /api/chat/start."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=True, active_profile="alpha",
            session={"session_id": "test-session-123", "profile": "beta"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0 and out["opened"] is None
        assert out["toast"] == "T:session_new_tab_other_profile"
        assert out["ref"]["chokeRan"] is True  # consumed, not fallen through

    def test_same_profile_session_opens_in_new_tab(self):
        """[CORE] A row owned by the active profile still opens normally."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=True, active_profile="alpha",
            session={"session_id": "test-session-123", "profile": "alpha"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 1
        assert out["toast"] is None

    def test_unknown_profile_owner_refused_when_merging_profiles(self):
        """[CORE] An unverifiable owner (no profile field while merging
        profiles) is refused rather than guessed."""
        out = _run_branch_variant(
            gesture_state="pressing", swipe_tracking=False,
            show_all_profiles=True, active_profile="alpha",
            session={"session_id": "test-session-123"})
        assert "error" not in out, out.get("error")
        assert out["openCalls"] == 0
        assert out["toast"] == "T:session_new_tab_other_profile"
