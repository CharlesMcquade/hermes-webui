"""Realtime voice was RESTORED in this fork (2026-10-13) — its entry points must stay live.

Upstream retired the Realtime voice bridge; the fork overlay restores it. These
tests pin the restored surface: browser/API entry points are wired up, the
handlers exist, and the fail-safe CSRF exemption for disconnect remains.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from api import extension_auth, routes


ROOT = Path(__file__).resolve().parents[1]
LIVE_ACTIONS = (
    "sdp", "connect", "disconnect", "turn", "ask", "steer", "status", "stop", "usage",
)
ROUTES = ("capability",) + LIVE_ACTIONS


@pytest.mark.parametrize("action", ("capability",))
def test_voice_live_get_routes_dispatch_to_voice_module(action):
    handler = Mock()
    parsed = SimpleNamespace(path=f"/api/voice/live/{action}")
    target = f"handle_voice_live_{action}"
    with patch.object(routes, "j", return_value=True) as response, patch(
        f"api.voice_live.{target}"
    ) as dispatched:
        assert routes.handle_get(handler, parsed) is not None
    dispatched.assert_called_once_with(handler)


@pytest.mark.parametrize("action", LIVE_ACTIONS)  # capability is GET-only
def test_voice_live_post_routes_dispatch_after_body_read(action):
    handler = Mock()
    parsed = SimpleNamespace(path=f"/api/voice/live/{action}")
    target = f"handle_voice_live_{action}"
    with patch.object(routes, "j", return_value=True) as response, patch.object(
        routes, "_handle_extension_sidecar_proxy", return_value=False
    ), patch.object(
        extension_auth, "request_body_too_large", return_value=False
    ), patch.object(
        routes, "_check_csrf", return_value=True
    ), patch.object(
        routes, "read_body", return_value=b"{}"
    ) as read_body, patch.object(
        routes, "arm_connection_close_if_body_pending"
    ), patch(f"api.voice_live.{target}") as dispatched:
        assert routes.handle_post(handler, parsed) is not None
    # Dispatch is direct — the voice handlers own their body parsing.
    dispatched.assert_called_once_with(handler)
    read_body.assert_not_called()


def test_disconnect_is_failsafe_csrf_exempt_and_connect_is_not():
    # Beacon-based disconnect can only RESTORE pre-call approval state, so it
    # is exempt; connect enables YOLO and must keep CSRF protection.
    assert routes._csrf_exempt_path("/api/voice/live/disconnect")
    assert not routes._csrf_exempt_path("/api/voice/live/connect")


def test_voice_live_handlers_exist():
    import api.voice_live as voice_live

    for action in ROUTES:
        assert callable(getattr(voice_live, f"handle_voice_live_{action}"))


def test_restored_client_and_extension_have_voice_entry_points():
    shell = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btnLiveVoice"' in shell
    assert 'src="static/voice_live.js' in shell
    assert 'id="btnVoiceMode"' in shell  # ordinary voice mode remains
    # Extension control scope: GET routes are read-only probes; POST routes
    # carry the mutating voice actions.
    for route in ("voice/live/capability", "voice/live/status", "voice/live/usage"):
        assert route in extension_auth.CONTROL_GET
    for route in (
        "voice/live/connect", "voice/live/disconnect", "voice/live/ask",
        "voice/live/sdp", "voice/live/steer", "voice/live/stop", "voice/live/turn",
        "voice/live/usage",
    ):
        assert route in extension_auth.CONTROL_POST
