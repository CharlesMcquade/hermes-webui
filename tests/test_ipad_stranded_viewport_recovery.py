"""Stranded-viewport recovery regression tests (#6426 re-gate CORE finding).

Reproduction: touch-primary sidebar with a bounded deep-active window
([start, loaded) far below the total), then a jump to scrollTop=0. The DOM
window stays at the deep interval, zero rows are visible, the top sentinel is
outside the viewport, and no scroll-driven batch can reach a position outside
the window — the sidebar stays blank forever. These tests pin the recovery
hook: scroll events arm a deferred, revalidated repair that re-anchors the
touch bounds around the live scroll position without rebuilding DOM during a
momentum gesture.
"""
import json

from tests.test_ipad_sidebar_scroll_stuck import (  # noqa: F401
    SESSIONS_JS,
    _extract_fn,
    _node_tests,
    _run_node_vm,
)


def test_stranded_recovery_wired_into_touch_scroll_path():
    """The touch early-return of _scheduleSessionVirtualizedRender must call
    the recovery hook — without it, a stranded viewport has no repair path
    (master's window recalculation is disabled by the same early-return).
    """
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    touch_idx = fn.find("if(_isTouchPrimary())")
    assert touch_idx >= 0, "Touch early-return must exist"
    touch_block = fn[touch_idx:touch_idx + 400]
    assert "_recoverStrandedTouchViewport(" in touch_block, \
        "Touch scroll path must invoke the stranded-viewport recovery hook"


def test_recovery_timer_released_on_invalidation():
    """_invalidateTouchRender is the unified teardown — it must clear the
    recovery timer so a pending repair cannot fire against torn-down state.
    """
    fn = _extract_fn(SESSIONS_JS, "_invalidateTouchRender")
    assert "_strandedTouchRecoveryTimer" in fn, \
        "Unified invalidation must clear the stranded-recovery timer"
    assert "clearTimeout(_strandedTouchRecoveryTimer)" in fn


def test_recovery_never_renders_inline_on_scroll_event():
    """The scroll-event entry must NOT rebuild DOM synchronously (that is the
    momentum-freeze class of bug this PR exists to fix). The repair may only
    run inside the deferred timer callback.
    """
    fn = _extract_fn(SESSIONS_JS, "_recoverStrandedTouchViewport")
    assert "setTimeout(" in fn, "Recovery must be deferred, not inline"
    timer_cb_start = fn.find("setTimeout(")
    timer_cb = fn[timer_cb_start:]
    assert timer_cb.count("renderSessionListFromCache(") >= 1, \
        "The deferred callback performs the repair render"
    # No render call may appear BEFORE the setTimeout (inline path).
    pre_timer = fn[:timer_cb_start]
    assert "renderSessionListFromCache(" not in pre_timer, \
        "Scroll-event path must never render inline"


def test_repair_reanchors_bounds_instead_of_preserving_deep_window():
    """The repair must write _sessionTouchStartIndex/_sessionTouchLoadedCount
    around the live scroll position before rendering. A plain from-cache
    render would PRESERVE the stale deep window (unchanged-scope fingerprint),
    which is exactly the stranded state.
    """
    fn = _extract_fn(SESSIONS_JS, "_recoverStrandedTouchViewport")
    cb_idx = fn.find("setTimeout(")
    cb = fn[cb_idx:]
    assert "_sessionTouchStartIndex=" in cb, \
        "Repair must re-anchor the canonical start bound"
    assert "_sessionTouchLoadedCount=" in cb, \
        "Repair must re-anchor the canonical loaded bound"
    assert "firstVisible" in cb, \
        "Re-anchor must key off the live scroll position"
    # The start must be derived from firstVisible with a buffer, and clamped
    # so the window cannot exceed the list.
    assert "SESSION_TOUCH_INITIAL_BATCH" in cb
    assert "SESSION_VIRTUAL_BUFFER_ROWS" in cb


@_node_tests
def test_production_stranded_top_jump_recovers_with_bounded_window():
    """Production-path regression for the exact re-gate reproduction:
    200 sessions, deep-active window [140,200), jump to scrollTop=0 → the
    repair re-anchors the bounds to a bounded top window and re-renders.
    Uses the REAL _touchViewportStranding + _recoverStrandedTouchViewport +
    _touchNextBatchDirection extracted from static/sessions.js.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

// ── Minimal browser mocks ──────────────────────────────────────────────
const timers = [];
let _now = 1000000;
const realSetTimeout = setTimeout;
const realClearTimeout = clearTimeout;
// Route the production setTimeout/clearTimeout through fakes so the test
// fires the repair synchronously without waiting 1250ms of wall clock.
const sandboxSetTimeout = function(fn, ms) {
  const id = timers.length + 1;
  timers.push({id, fn, ms, armedAt: _now, cancelled: false});
  return id;
};
const sandboxClearTimeout = function(id) {
  const t = timers.find(t => t.id === id);
  if (t) t.cancelled = true;
};

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 140;
let _sessionTouchLoadedCount = 200;
let _sessionTouchTotalCount = 200;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;
let _strandedTouchRecoveryTimer = 0;
let _sessionListLastScrollAt = 0;
let _pointerActive = false;
let renderCalls = [];
let renderOptsLog = [];

function _isTouchPrimary() { return true; }
function _isSessionListTouchScrolling() {
  const now = Date.now();
  return Boolean(
    _pointerActive ||
    (_sessionListLastScrollAt && now - _sessionListLastScrollAt < SESSION_LIST_TOUCH_INTERACTION_IDLE_MS)
  );
}
function _deferRenderSessionListFromCache() { renderOptsLog.push('deferred'); }

function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 900, bottom: 940, left: 0, right: 300, width: 300, height: 40};
  }};
}

// A list whose geometry matches the stranding reproduction: scrollTop=0,
// 200 rows worth of scrollable height, clientHeight 600.
const list = {
  scrollTop: 0,
  clientHeight: 600,
  getBoundingClientRect() {
    return {top: 0, bottom: 600, left: 0, right: 300, width: 300, height: 600};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('none');
    if (sel === '[data-touch-sentinel]') return makeSentinel('');
    return null;
  },
};

// Real boundary helpers, extracted from production.
const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));
// _recoverStrandedTouchViewport references renderSessionListFromCache and the
// timer globals — bind the sandbox timers for it.
const _origSetTimeout = globalThis.setTimeout;
const _origClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = sandboxSetTimeout;
globalThis.clearTimeout = sandboxClearTimeout;
eval(extractFunc('_recoverStrandedTouchViewport').replace(
  'renderSessionListFromCache({force:true});',
  'renderCalls.push({start:_sessionTouchStartIndex, loaded:_sessionTouchLoadedCount}); ' +
  'renderOptsLog.push("force");'
));

// ── Wire production state to the reproduction ─────────────────────────
_sessionTouchListEl = list;
_touchRenderState = {
  gen: _sessionTouchGen,
  list: list,
  flatRows: new Array(200),
  itemHeight: SESSION_VIRTUAL_ROW_HEIGHT,
};

// The scroll event that reveals the blank viewport:
list.scrollTop = 0;
_sessionListLastScrollAt = Date.now();
_recoverStrandedTouchViewport(list);

const armedCount = timers.filter(t => !t.cancelled).length;
// Fire the armed repair synchronously (gesture long since decayed).
_sessionListLastScrollAt = 0;
for (const t of timers) {
  if (!t.cancelled) { t.cancelled = true; t.fn(); }
}

console.log(JSON.stringify({
  armedCount,
  renderCalls,
  renderOptsLog,
  timersTotal: timers.length,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["armedCount"] == 1, \
        f"Exactly one repair must arm on the stranding scroll event, got {result}"
    assert len(result["renderCalls"]) == 1, \
        f"Repair must render exactly once, got {result}"
    assert result["renderOptsLog"] == ["force"], \
        f"Repair render must be a forced from-cache render, got {result}"
    window = result["renderCalls"][0]
    assert window["start"] == 0, \
        f"Re-anchor must move the window start to the top of the list, got {window}"
    assert window["loaded"] > 0, \
        f"Re-anchored window must paint rows, got {window}"
    # SESSION_TOUCH_INITIAL_BATCH = 60 — the re-anchored window must be a
    # bounded initial batch, never a full-list synchronous paint.
    assert window["loaded"] <= 60, \
        f"Re-anchored window must stay bounded, got {window}"


@_node_tests
def test_production_visible_sentinel_blocks_recovery():
    """If a directional sentinel is visible (batch machinery still covers the
    position), recovery must NOT arm — no racing the incremental path.
    """
    source = f"""
const SESSIONS_JS = {SESSIONS_JS!r};
""" + """
function extractFunc(name) {
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = SESSIONS_JS.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = SESSIONS_JS.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < SESSIONS_JS.length) {
    if (SESSIONS_JS[i] === '{') depth++;
    else if (SESSIONS_JS[i] === '}') depth--;
    i++;
  }
  return SESSIONS_JS.slice(start, i);
}

const timers = [];
const sandboxSetTimeout = function(fn, ms) {
  timers.push({fn, cancelled: false});
  return timers.length;
};
const sandboxClearTimeout = function(id) {
  if (timers[id - 1]) timers[id - 1].cancelled = true;
};

const SESSION_LIST_TOUCH_INTERACTION_IDLE_MS = 1200;
const SESSION_TOUCH_INITIAL_BATCH = 60;
const SESSION_TOUCH_BATCH_SIZE = 40;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 8;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
let _sessionTouchGen = 1;
let _sessionTouchStartIndex = 140;
let _sessionTouchLoadedCount = 200;
let _sessionTouchTotalCount = 200;
let _sessionTouchListEl = null;
let _touchRenderState = null;
let _touchBatchPending = false;
let _touchContinuousBatchOwner = null;
let _strandedTouchRecoveryTimer = 0;
let _sessionListLastScrollAt = 0;
let _pointerActive = false;
let renderCalls = [];

function _isTouchPrimary() { return true; }
function _isSessionListTouchScrolling() { return false; }

// Visible TOP sentinel intersecting the viewport: the IntersectionObserver
// path is live at this position, so recovery must stand down.
function makeSentinel(display) {
  return {style: {display: display}, getBoundingClientRect() {
    return {top: 10, bottom: 50, left: 0, right: 300, width: 300, height: 40};
  }};
}
const list = {
  scrollTop: 0,
  clientHeight: 600,
  getBoundingClientRect() {
    return {top: 0, bottom: 600, left: 0, right: 300, width: 300, height: 600};
  },
  querySelector(sel) {
    if (sel === '[data-touch-sentinel-top]') return makeSentinel('');
    if (sel === '[data-touch-sentinel]') return makeSentinel('none');
    return null;
  },
};

const bndStart = extractFunc('_touchStartBoundaryNearViewport');
const bndLoaded = extractFunc('_touchLoadedBoundaryNearViewport');
eval(bndStart);
eval(bndLoaded);
eval(extractFunc('_touchNextBatchDirection'));
eval(extractFunc('_sentinelIntersectsViewport'));
eval(extractFunc('_touchViewportStranding'));
const _origSetTimeout = globalThis.setTimeout;
const _origClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = sandboxSetTimeout;
globalThis.clearTimeout = sandboxClearTimeout;
eval(extractFunc('_recoverStrandedTouchViewport').replace(
  'renderSessionListFromCache({force:true});',
  'renderCalls.push(1);'
));

_sessionTouchListEl = list;
_touchRenderState = {gen: _sessionTouchGen, list: list, flatRows: new Array(200), itemHeight: SESSION_VIRTUAL_ROW_HEIGHT};

list.scrollTop = 0;
_sessionListLastScrollAt = Date.now();
_recoverStrandedTouchViewport(list);

console.log(JSON.stringify({
  armed: timers.filter(t => !t.cancelled).length,
  renderCalls: renderCalls.length,
}));
"""
    result = json.loads(_run_node_vm(source))
    assert result["armed"] == 0 and result["renderCalls"] == 0, \
        f"Visible top sentinel must veto recovery, got {result}"
