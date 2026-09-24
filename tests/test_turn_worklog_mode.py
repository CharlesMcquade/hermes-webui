"""Opt-in Turn Worklog presentation contracts (isolated, no live state)."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "static/ui.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static/messages.js").read_text(encoding="utf-8")
ANCHORS = ROOT / "static/assistant_turn_anchors.js"
NODE = shutil.which("node")


@pytest.fixture(scope="session")
def test_server():
    """Override the suite's autouse server: these tests only execute static JS."""
    yield None


def _body(src, name):
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for pos in range(brace, len(src)):
        depth += (src[pos] == "{") - (src[pos] == "}")
        if depth == 0:
            return src[brace + 1:pos]
    raise AssertionError(f"unterminated {name}")


def _node(script):
    proc = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_anchor_scene_preserves_explicit_turn_worklog_on_projection_and_restore():
    """The scene, not current settings, owns the persisted presentation choice."""
    script = f"""
const fs=require('fs'), vm=require('vm');
const sandbox={{window:{{}}}}; vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ANCHORS))},'utf8'),sandbox);
const api=sandbox.window.HermesAssistantTurnAnchors;
const registry=api.createAssistantTurnAnchorRegistry({{session_id:'sid',turn_id:'turn'}});
api.applyAssistantTurnAnchorSourceEvents(registry,[
  {{event:'tool',payload:{{tool_call_id:'t1',name:'terminal'}},event_id:'run:1',seq:1}},
  {{source_type:'settled_message',payload:{{role:'assistant',id:'final',content:'Answer'}}}}
],{{run_id:'run',stream_id:'stream'}});
const scene=api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'turn_worklog'}});
const restored=api.reconcileAssistantTurnAnchorActivityScene
  ? api.reconcileAssistantTurnAnchorActivityScene({{scene,mode:'turn_worklog'}}) : null;
console.log(JSON.stringify({{sceneMode:scene.mode,rows:scene.activity_rows.length,restoredMode:restored&&restored.mode}}));
"""
    result = _node(script)
    assert result["rows"] > 0
    assert result["sceneMode"] == "turn_worklog", "explicit mode must not silently downgrade to compact"


def test_live_turn_worklog_uses_inline_chronology_not_a_top_level_group():
    dispatch = _body(UI, "renderLiveAnchorActivityScene")
    flat = _body(UI, "_renderLiveAnchorActivitySceneTransparent")
    assert "_renderLiveAnchorActivitySceneTurnWorklog" in dispatch
    assert "_anchorSceneRowsForRendering(scene,{settled:false})" in flat
    assert "_anchorSceneNodeForRow(row,{live:true,settled:false})" in flat
    assert "if(renderedRows.length&&!turnWorklog) _syncTransparentEventControls(turn)" in flat
    assert "ensureActivityGroup(" not in flat
    assert "ensureLiveWorklogContainer(" not in flat
    assert "data-anchor-row-id" in flat


def test_settled_turn_worklog_defaults_closed_but_error_without_final_opens():
    settled = _body(UI, "_renderSettledAnchorSceneForMessage")
    group = _body(UI, "ensureActivityGroup")
    assert "turnWorklog:isTurnWorklogMode()" in settled
    assert "_anchorSceneHasErroredTerminalState" in settled
    assert "_anchorSceneRowsForRendering" in settled
    assert "_anchorSceneWorklogGroup" in settled
    assert "window._worklogDetailsExpandedByDefault===true&&!opts.turnWorklog" in group
    assert "_materializeDeferredWorklogRows" in UI


def test_pure_final_scene_does_not_create_empty_turn_worklog_shell():
    settled = _body(UI, "_renderSettledAnchorSceneForMessage")
    assert "_anchorSceneSceneHasWorklogWorthyRows" in settled
    attach = _body(MESSAGES, "_attachProjectedAnchorSceneToLastAssistant")
    assert "_anchorSceneHasWorklogWorthyRows" in attach


@pytest.mark.parametrize("origin", ["compact_worklog", "transparent_stream", "hide_all_activity", "turn_worklog"])
def test_server_reload_keeps_distinct_repeated_interim_updates(origin):
    """Read-side scene repair must not dedupe separate progress events by text."""
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Checking source", "tool_calls": [{"id": "tool-a", "name": "read_file", "args": {"path": "fixture.txt"}}]},
        {"role": "assistant", "content": "final answer"},
    ]
    scene = {
        "version": "activity_scene_v1", "mode": origin, "final_answer": "final answer",
        "activity_rows": [
            {"role": "prose", "source_event_type": "interim_assistant", "row_id": "progress-a", "local_id": "progress-a", "text": "Checking source"},
            {"role": "tool", "source_event_type": "tool_completed", "row_id": "tool-a", "tool_call_id": "tool-a", "tool": {"id": "tool-a", "name": "read_file", "done": True}},
            {"role": "prose", "source_event_type": "interim_assistant", "row_id": "progress-b", "local_id": "progress-b", "text": "Checking source"},
        ],
    }
    repaired = routes._complete_hydrated_anchor_scene(messages, scene, 2, stream_id="stream")
    rows = repaired["activity_rows"]
    progress = [row for row in rows if row.get("role") == "prose"]
    assert [row["row_id"] for row in progress] == ["progress-a", "progress-b"]
    assert [row.get("role") for row in rows if row.get("role") in ("prose", "tool")] == ["prose", "tool", "prose"]
    assert repaired["mode"] == origin
    assert any(row.get("role") == "tool" for row in rows)


def test_turn_worklog_restore_and_mode_switch_have_explicit_paths():
    assert "turn_worklog" in _body(UI, "chatActivityMode")
    assert "turn_worklog" in _body(MESSAGES, "_anchorSceneActiveMode")
    assert "turnWorklog:isTurnWorklogMode()" in _body(UI, "_renderSettledAnchorSceneForMessage")
    assert "_rehydrateDeferredWorklogsFromCache" in UI
