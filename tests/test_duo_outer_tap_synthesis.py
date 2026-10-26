"""Duo outer-display tap synthesis: executable decision core + source contracts.

iOS 27 foldable standalone PWA (iPhone Duo): during/after fold screen handoffs
WebKit stops synthesizing mousedown/mouseup/click even though pointer events
still dispatch to correct targets, leaving every click-activated control dead
(titlebar hamburger, drawer Close X, overlay). The bridge in boot.js completes
clean taps with a synthetic click when no trusted click follows; the session
row gesture machine finishes taps by endpoint distance when inflated jitter
promoted them to 'dragging'. Live-device evidence: nesquena/hermes-webui#8531.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(source, name):
    match = re.search(r"^function " + re.escape(name) + r"\([^\n]*\)\s*\{.*?^}", source, re.M | re.S)
    assert match, f"Missing function {name}"
    return match.group(0)


def _constants(source, prefix):
    found = []
    for match in re.finditer(r"^const (" + re.escape(prefix) + r"\w*)=([^;]+);", source, re.M):
        found.append(f"const {match.group(1)}={match.group(2)};")
    assert found, f"Missing constants {prefix}*"
    return "\n".join(found)


def _decide(arm, up):
    """Run the shipped _pwaTapClickShouldSchedule in Node."""
    boot = (ROOT / "static/boot.js").read_text(encoding="utf-8")
    source = (
        _constants(boot, "_PWA_TAP_CLICK_")
        + "\n"
        + _function(boot, "_pwaTapClickShouldSchedule")
        + r"""
const arm={pointerId:7,pointerType:'touch',clientX:34,clientY:26,timeStamp:1000,excluded:false};
const up={pointerId:7,pointerType:'touch',button:0,clientX:36,clientY:28,timeStamp:1120,excluded:false};
function run(a,u){return _pwaTapClickShouldSchedule(a,u);}
"""
        + "console.log(JSON.stringify(run("
        + json.dumps(arm) + "," + json.dumps(up) + ")));"
    )
    result = subprocess.run([NODE, "-e", source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_clean_tap_schedules_synthetic_click():
    assert _decide(
        {"pointerId": 7, "pointerType": "touch", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": False},
        {"pointerId": 7, "pointerType": "touch", "button": 0, "clientX": 36, "clientY": 28, "timeStamp": 1120, "excluded": False},
    ) is True


@pytest.mark.parametrize("dx,dy", [(15, 0), (0, 15), (-20, 3)])
def test_travel_beyond_threshold_is_not_a_tap(dx, dy):
    assert _decide(
        {"pointerId": 7, "pointerType": "touch", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": False},
        {"pointerId": 7, "pointerType": "touch", "button": 0, "clientX": 34 + dx, "clientY": 26 + dy, "timeStamp": 1120, "excluded": False},
    ) is False


def test_long_press_is_not_a_tap():
    assert _decide(
        {"pointerId": 7, "pointerType": "touch", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": False},
        {"pointerId": 7, "pointerType": "touch", "button": 0, "clientX": 34, "clientY": 26, "timeStamp": 1800, "excluded": False},
    ) is False


def test_mouse_pointer_never_schedules():
    assert _decide(
        {"pointerId": 7, "pointerType": "mouse", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": False},
        {"pointerId": 7, "pointerType": "mouse", "button": 0, "clientX": 34, "clientY": 26, "timeStamp": 1120, "excluded": False},
    ) is False


def test_secondary_button_never_schedules():
    assert _decide(
        {"pointerId": 7, "pointerType": "touch", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": False},
        {"pointerId": 7, "pointerType": "touch", "button": 2, "clientX": 34, "clientY": 26, "timeStamp": 1120, "excluded": False},
    ) is False


@pytest.mark.parametrize("which", ["arm", "up"])
def test_excluded_surface_never_schedules(which):
    arm = {"pointerId": 7, "pointerType": "touch", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": which == "arm"}
    up = {"pointerId": 7, "pointerType": "touch", "button": 0, "clientX": 34, "clientY": 26, "timeStamp": 1120, "excluded": which == "up"}
    assert _decide(arm, up) is False


def test_pointer_id_mismatch_never_schedules():
    assert _decide(
        {"pointerId": 7, "pointerType": "touch", "clientX": 34, "clientY": 26, "timeStamp": 1000, "excluded": False},
        {"pointerId": 9, "pointerType": "touch", "button": 0, "clientX": 34, "clientY": 26, "timeStamp": 1120, "excluded": False},
    ) is False


def test_missing_observations_never_schedule():
    assert _decide(None, {"pointerId": 7, "button": 0}) is False
    assert _decide({"pointerId": 7, "pointerType": "touch", "excluded": False}, None) is False


def test_boot_bridge_source_contract():
    boot = (ROOT / "static/boot.js").read_text(encoding="utf-8")
    # Installer is gated to the standalone PWA with a coarse pointer.
    assert "_isPwaStandalone()" in boot
    assert "matchMedia('(pointer: coarse)').matches" in boot
    # Every bridge listener is capture+passive (must never disturb scroll or
    # the app's own gesture machines).
    for listener in ("'pointerdown'", "'pointermove'", "'pointercancel'", "'pointerup'"):
        pattern = re.compile(r"window\.addEventListener\(" + re.escape(listener) + r",.*?\{capture:true,passive:true\}", re.S)
        assert pattern.search(boot), f"bridge listener {listener} must be capture+passive"
    # The confirm listener only honors trusted clicks; the synthetic dispatch
    # is a bubbling, cancelable MouseEvent.
    assert "if(ev.isTrusted)trustedClick=true" in boot
    assert "new MouseEvent('click',{bubbles:true,cancelable:true" in boot
    assert "_PWA_TAP_CLICK_DELAY_MS=220" in boot
    # Row tap surfaces stay with the row gesture machine.
    assert ".session-item .session-text" in boot


def test_session_row_endpoint_tap_source_contract():
    sessions = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    assert "const SESSION_TAP_ENDPOINT_PX = 28" in sessions
    # Endpoint check is guarded against tracked horizontal swipes and uses the
    # shared constant.
    assert re.search(r"const _endpointTap=!_swipeTracking\s*&&Math\.abs\(clientX-_pointerDownX\)<=SESSION_TAP_ENDPOINT_PX", sessions)
    # A promoted-but-tap-like gesture completes as a tap; the drag paint settles.
    assert re.search(r"const wasDragging=\(_gestureState==='dragging'\|\|_swipeTracking\)&&!_endpointTap", sessions)
    assert "if(_endpointTap&&_gestureState==='dragging')_settleSessionSwipePaint();" in sessions
