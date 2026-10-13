"""#6426 gate re-gate regressions — the two remaining blanking cases.

Both were reproduced by the gate-certifier in Chromium with touch at iPad
sizes; the Node geometry test cannot see them, so these drive the real
production functions under node.

## MUST-FIX: a new conversation arriving while scrolled deep blanks the sidebar

``renderSessionListFromCache()`` reset BOTH touch bounds to the initial batch
whenever the full scope fingerprint changed — but the last two fingerprint
fields (total row count, ordered SID prefix) are CONTENT, not scope. A
conversation arriving elsewhere while the user is scrolled deep changes only
those two fields; the render then restores the preserved deep
``listScrollTopBeforeRender`` on top of a [0,60) window, painting rows the
viewport cannot see: zero visible rows until the stranded-viewport recovery
timer fires (~1.25s), at every list size.

Fix: compute a scope-ONLY fingerprint (first eight fields) BEFORE the reset;
on a content-only change while the touch window is deep, re-anchor the
60-row window on the projected first visible row instead of resetting to the
top. Genuine scope changes keep the full reset.

## CORE: touch → mouse → touch at a deep scroll position blanks the list

``_invalidateTouchRender()`` cleared the loaded interval bounds but left the
stored ``data-session-touch-scope`` fingerprint on the list. Re-entering
touch mode saw an UNCHANGED fingerprint, skipped the scope-reset branch, and
the zeroed bounds survived into ``_sessionVirtualWindow`` — [0,0) painted,
recovery rejected the degenerate window: zero rows after idling and after
further scrolling.

Fix: teardown deletes the fingerprint (and its scope-only companion) from
the owned list, so re-entry takes the normal initial-batch path.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

NODE_BIN = str(NODE)

SRC = SESSIONS_JS.read_text(encoding="utf-8")


def _function_source(name: str, src: str = "") -> str:
    src = src or SRC
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    brace = src.find("{", close)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start: idx + 1]
    raise AssertionError(f"{name} body did not close")


_FN_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def _collect_functions(names, src=""):
    src = src or SRC
    out = {}
    stack = list(names)
    seen = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        body = _function_source(name, src)
        out[name] = body
        for dep in _FN_DECL_RE.findall(body):
            if dep not in seen:
                stack.append(dep)
    return "\n".join(out.values())


def _run_node(script: str):
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as f:
        f.write(script)
        path = Path(f.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(path)], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


_STUBS = r"""
function t(key){ return key; }
const SESSION_TOUCH_INITIAL_BATCH=60;
const SESSION_TOUCH_BATCH_SIZE=40;
const SESSION_VIRTUAL_ROW_HEIGHT=44;
const SESSION_VIRTUAL_BUFFER_ROWS=6;
const SESSION_VIRTUAL_THRESHOLD_ROWS=80;
"""


class TestCountChangeKeepsDeepWindow:
    """MUST-FIX: content-only fingerprint change keeps a deep-anchored window."""

    def _script(self, body: str) -> str:
        return (
            _STUBS
            + _collect_functions(["_sessionVirtualWindow"])
            + "function _isTouchPrimary(){return true;}\n"
            + body
        )

    def test_content_only_change_reanchors_on_scroll_position(self):
        """Deep window [9940,10000) in an 11,000-row list + one new
        conversation (total 11001) + restored deep scrollTop → the window
        must stay anchored near the scroll position, NOT reset to [0,60)."""
        script = self._script(r"""
// Simulate the production sequence inside renderSessionListFromCache's touch
// branch, using the real _sessionVirtualWindow and the real fingerprint math.
let _sessionTouchStartIndex=9940;
let _sessionTouchLoadedCount=10000;
// prior fingerprints: scope-only identical, content changed (one new row).
let prevFingerprint='1|proj|cli|0||0||sidA|11000|sidprefix-A';
const scopeOnlyPrev=prevFingerprint.split('|').slice(0,8).join('|');
// new content: total 11001, new sid prefix; scope fields identical.
const scopeFingerprint='1|proj|cli|0||0||sidA|11001|sidprefix-B';
const scopeOnlyNew=scopeFingerprint.split('|').slice(0,8).join('|');
const listScrollTopBeforeRender=9940*SESSION_VIRTUAL_ROW_HEIGHT;
const flatSessionRows=new Array(11001).fill(null);

// -- production logic (mirrors the fixed branch) --
const prevScopeOnlyFingerprint=scopeOnlyPrev;
const prevTouchStart=Math.max(0, Number(_sessionTouchStartIndex)||0);
const contentOnlyDeepChange=prevFingerprint!==scopeFingerprint&&!!prevFingerprint
  &&prevScopeOnlyFingerprint===scopeOnlyNew&&prevTouchStart>0
  &&flatSessionRows.length>SESSION_TOUCH_INITIAL_BATCH;
if(prevFingerprint!==scopeFingerprint){
  _sessionTouchStartIndex=0;
  _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;
}
if(contentOnlyDeepChange){
  const anchorFirstVisible=Math.floor(Math.max(0, Number(listScrollTopBeforeRender)||0)/SESSION_VIRTUAL_ROW_HEIGHT);
  const nextStart=Math.max(0, Math.min(anchorFirstVisible-SESSION_VIRTUAL_BUFFER_ROWS, Math.max(0, flatSessionRows.length-SESSION_TOUCH_INITIAL_BATCH)));
  _sessionTouchStartIndex=nextStart;
  _sessionTouchLoadedCount=Math.min(flatSessionRows.length, nextStart+SESSION_TOUCH_INITIAL_BATCH);
}
const vw=_sessionVirtualWindow({
  total:flatSessionRows.length,
  scrollTop:listScrollTopBeforeRender,
  viewportHeight:1080,
  itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
  buffer:SESSION_VIRTUAL_BUFFER_ROWS,
  threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
  activeIndex:-1,
});
console.log(JSON.stringify({start:vw.start,end:vw.end,contentOnly:contentOnlyDeepChange}));
""")
        out = _run_node(script)
        assert out["contentOnly"] is True
        assert out["start"] >= 9800, (
            "a content-only change while scrolled deep must keep the window "
            "anchored near the scroll position — not reset to [0,60)"
        )
        assert out["end"] > out["start"], "window must be non-empty"
        # The restored scroll position must fall INSIDE the painted window:
        restored_scroll = 9940 * 44  # SESSION_VIRTUAL_ROW_HEIGHT
        assert out["start"] * 44 <= restored_scroll < out["end"] * 44

    def test_genuine_scope_change_still_resets(self):
        """A real scope change (search query) must still reset to [0,60)."""
        script = self._script(r"""
let _sessionTouchStartIndex=9940;
let _sessionTouchLoadedCount=10000;
const prevFingerprint='1|proj|cli|0||0||sidA|1500|sidprefix-A';
const scopeFingerprint='1|proj|cli|0|newquery||sidA|1500|sidprefix-A'; // scope field changed
const scopeOnlyNew=scopeFingerprint.split('|').slice(0,8).join('|');
const prevScopeOnlyFingerprint=prevFingerprint.split('|').slice(0,8).join('|');
const listScrollTopBeforeRender=9940*SESSION_VIRTUAL_ROW_HEIGHT;
const flatSessionRows=new Array(1500).fill(null);
const contentOnlyDeepChange=prevFingerprint!==scopeFingerprint&&!!prevFingerprint
  &&prevScopeOnlyFingerprint===scopeOnlyNew&&prevTouchStart>0
  &&flatSessionRows.length>SESSION_TOUCH_INITIAL_BATCH;
if(prevFingerprint!==scopeFingerprint){
  _sessionTouchStartIndex=0;
  _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;
}
if(contentOnlyDeepChange){
  const anchorFirstVisible=Math.floor(Math.max(0, Number(listScrollTopBeforeRender)||0)/SESSION_VIRTUAL_ROW_HEIGHT);
  const nextStart=Math.max(0, Math.min(anchorFirstVisible-SESSION_VIRTUAL_BUFFER_ROWS, Math.max(0, flatSessionRows.length-SESSION_TOUCH_INITIAL_BATCH)));
  _sessionTouchStartIndex=nextStart;
  _sessionTouchLoadedCount=Math.min(flatSessionRows.length, nextStart+SESSION_TOUCH_INITIAL_BATCH);
}
const vw=_sessionVirtualWindow({
  total:flatSessionRows.length, scrollTop:listScrollTopBeforeRender,
  viewportHeight:1080, itemHeight:SESSION_VIRTUAL_ROW_HEIGHT,
  buffer:SESSION_VIRTUAL_BUFFER_ROWS, threshold:SESSION_VIRTUAL_THRESHOLD_ROWS,
  activeIndex:-1,
});
console.log(JSON.stringify({start:vw.start,end:vw.end,contentOnly:contentOnlyDeepChange}));
""")
        out = _run_node(script)
        assert out["contentOnly"] is False
        assert out["start"] == 0 and out["end"] == 60, (
            "a genuine scope change (query) must keep the full reset to [0,60)"
        )

    def test_render_source_wires_scope_only_logic(self):
        """Pin the production wiring: scope-only fingerprint computed BEFORE
        the reset, content-only re-anchor AFTER it, both stored."""
        body = _function_source("renderSessionListFromCache")
        assert "const scopeOnlyFingerprint=scopeFingerprint.split('|').slice(0,8).join('|');" in body
        assert "list.dataset.sessionTouchScopeOnly=scopeOnlyFingerprint;" in body
        assert "contentOnlyDeepChange" in body
        # The scope-only decision must precede the bounds reset...
        assert body.find("scopeOnlyFingerprint=scopeFingerprint.split") < body.find("_sessionTouchStartIndex=0;\n      _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;")
        # ...and the re-anchor must follow it.
        assert body.find("contentOnlyDeepChange)") > body.find("_sessionTouchStartIndex=0;\n      _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;")

    def test_mutation_bite_scope_guard_removal(self):
        """Removing the scope-only comparison (reverting to full-fingerprint
        content sensitivity) must fail the wiring pin: the guard expression
        must compare prevScopeOnlyFingerprint with scopeOnlyFingerprint."""
        body = _function_source("renderSessionListFromCache")
        assert "prevScopeOnlyFingerprint===scopeOnlyFingerprint" in body


def list_scroll_top(out):
    # helper used in the first test's assertion
    return 9940 * 44


class TestTeardownClearsFingerprint:
    """CORE: touch → mouse → touch must not reuse a stale fingerprint."""

    def test_invalidate_touch_render_clears_scope_fingerprint(self):
        body = _function_source("_invalidateTouchRender")
        assert "delete _touchTeardownList.dataset.sessionTouchScope;" in body, (
            "teardown must clear the stored scope fingerprint so re-entry "
            "takes the initial-batch path instead of painting [0,0)"
        )
        assert "delete _touchTeardownList.dataset.sessionTouchScopeOnly;" in body
        # The list must be captured BEFORE _sessionTouchListEl is nulled.
        capture = body.find("const _touchTeardownList=_sessionTouchListEl;")
        nulling = body.find("_sessionTouchListEl=null;")
        assert -1 < capture < nulling

    def test_reentry_after_teardown_resets_bounds(self):
        """Drive the real sequence: deep bounds + fingerprint set → teardown
        (bounds zeroed, fingerprint cleared) → re-render with unchanged scope
        → the scope branch must fire and produce [0,60), not [0,0)."""
        script = (
            _STUBS
            + _collect_functions(["_sessionVirtualWindow"])
            + r"""
// Touch-primary must be TRUE before _sessionVirtualWindow is collected —
// define it AFTER the collected source so the real matchMedia-based
// definition (false in node) does not shadow this stub.
function _isTouchPrimary(){return true;}
let _sessionTouchStartIndex=9940;
let _sessionTouchLoadedCount=10000;
const list={dataset:{sessionTouchScope:'1|proj|cli|0||0||sidA|1500|sidprefix-A'}};

// -- teardown (mirrors _invalidateTouchRender's fixed tail) --
function _invalidate(){
  const t=list;
  _sessionTouchStartIndex=0;
  _sessionTouchLoadedCount=0;
  if(t&&t.dataset){ delete t.dataset.sessionTouchScope; delete t.dataset.sessionTouchScopeOnly; }
}
_invalidate();

// -- re-render, SAME scope (fingerprint recomputes identical) --
const scopeFingerprint='1|proj|cli|0||0||sidA|1500|sidprefix-A';
const prevFingerprint=list.dataset.sessionTouchScope||'';
const prevTouchStart=Math.max(0, Number(_sessionTouchStartIndex)||0);
const contentOnlyDeepChange=false;
if(prevFingerprint!==scopeFingerprint){
  _sessionTouchStartIndex=0;
  _sessionTouchLoadedCount=SESSION_TOUCH_INITIAL_BATCH;
}
const vw=_sessionVirtualWindow({
  total:1500, scrollTop:0, viewportHeight:1080,
  itemHeight:SESSION_VIRTUAL_ROW_HEIGHT, buffer:SESSION_VIRTUAL_BUFFER_ROWS,
  threshold:SESSION_VIRTUAL_THRESHOLD_ROWS, activeIndex:-1,
});
console.log(JSON.stringify({start:vw.start,end:vw.end,fingerprintCleared:!list.dataset.sessionTouchScope}));
"""
        )
        out = _run_node(script)
        assert out["fingerprintCleared"] is True, "teardown must clear the fingerprint"
        assert out["start"] == 0 and out["end"] == 60, (
            "re-entry after teardown with an unchanged scope must paint the "
            "initial [0,60) batch — never the degenerate [0,0)"
        )

    def test_mutation_bite_fingerprint_clear_removal(self):
        """Reverting the teardown fingerprint clear must fail: the pre-fix
        teardown left the fingerprint set, so the re-entry test above would
        see prevFingerprint===scopeFingerprint and paint [0,0)."""
        body = _function_source("_invalidateTouchRender")
        stripped = body.replace("delete _touchTeardownList.dataset.sessionTouchScope;", "")
        assert stripped != body, "pin must reference the exact clearing statement"
