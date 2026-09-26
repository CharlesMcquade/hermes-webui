"""The retired Realtime voice bridge must stay unreachable after overlay replay.

Historical implementation and tests remain in the tree for provenance, but
credential discovery must never make its browser/API entry points available.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from api import extension_auth, routes


ROOT = Path(__file__).resolve().parents[1]
LIVE_ACTIONS = (
    "sdp", "connect", "disconnect", "turn", "ask", "steer", "status", "stop", "usage",
)


@pytest.mark.parametrize("action", ("capability",) + LIVE_ACTIONS)
def test_retired_get_routes_reject_before_voice_import(action):
    handler = Mock()
    parsed = SimpleNamespace(path=f"/api/voice/live/{action}")
    with patch.object(routes, "j", return_value=True) as response, patch(
        "api.voice_live.handle_voice_live_capability", side_effect=AssertionError("voice activated")
    ):
        assert routes.handle_get(handler, parsed) is True
    response.assert_called_once_with(handler, {"error": "live_voice_retired"}, status=410)


@pytest.mark.parametrize("action", ("capability",) + LIVE_ACTIONS)
def test_retired_post_routes_reject_without_reading_body(action):
    handler = Mock()
    parsed = SimpleNamespace(path=f"/api/voice/live/{action}")
    with patch.object(routes, "j", return_value=True) as response, patch.object(
        routes, "arm_connection_close_if_body_pending"
    ) as close_body, patch.object(routes, "read_body", side_effect=AssertionError("body read")):
        assert routes.handle_post(handler, parsed) is True
    close_body.assert_called_once_with(handler)
    response.assert_called_once_with(handler, {"error": "live_voice_retired"}, status=410)


def test_retired_routes_return_410_even_with_configured_key():
    # Exercise the real JSON response writer; do not mint a provider session.
    handler = Mock()
    handler._pending_set_cookies = []
    handler.headers = {"Content-Length": "2"}
    with patch("api.voice_live._resolve_openai_key", return_value="fixture-only") as key_lookup:
        assert routes.handle_get(handler, SimpleNamespace(path="/api/voice/live/capability")) is True
    key_lookup.assert_not_called()
    handler.send_response.assert_called_once_with(410)
    assert json.loads(handler.wfile.write.call_args.args[0]) == {"error": "live_voice_retired"}

    handler = Mock()
    handler._pending_set_cookies = []
    handler.headers = {"Content-Length": "2"}
    with patch("api.voice_live._resolve_openai_key", return_value="fixture-only") as key_lookup:
        assert routes.handle_post(handler, SimpleNamespace(path="/api/voice/live/connect")) is True
    key_lookup.assert_not_called()
    handler.send_response.assert_called_once_with(410)
    assert handler.close_connection is True
    assert json.loads(handler.wfile.write.call_args.args[0]) == {"error": "live_voice_retired"}


def test_retired_client_and_extension_have_no_voice_entry_points():
    shell = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btnLiveVoice"' not in shell
    assert 'src="static/voice_live.js' not in shell
    assert 'id="btnVoiceMode"' in shell  # ordinary voice mode remains
    assert not routes._csrf_exempt_path("/api/voice/live/disconnect")
    for route in ("voice/live/capability",) + tuple(f"voice/live/{x}" for x in LIVE_ACTIONS):
        assert route not in extension_auth.CONTROL_GET
        assert route not in extension_auth.CONTROL_POST
