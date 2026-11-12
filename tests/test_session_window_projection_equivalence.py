"""Windowed GET /api/session must equal the full delivered projection sliced.

This is the equivalence oracle for moving projection after window selection.
Expectations come from the actual ``msg_limit=all`` response, not a second
redaction/projection implementation. Short lorem payloads avoid the documented
transport-only clipping in ``_delivered_window_projection``.
"""
import json
from collections import OrderedDict
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules
SID = "window-projection-equivalence"


class _GetHandler:  # Copied from test_webui_state_db_reconciliation.py.
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    @property
    def query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, *args, **kwargs):
        pass


def _rows():
    rows = []
    for turn in range(75):
        call_id = f"call_{turn}"
        rows.extend([
            {"role": "user", "content": f"lorem question {turn}", "timestamp": 1000.0 + turn * 4},
            {"role": "assistant", "content": f"lorem call {turn}", "timestamp": 1001.0 + turn * 4,
             "tool_calls": [{"id": call_id, "type": "function", "function": {
                 "name": "terminal", "arguments": json.dumps({"command": f"printf lorem-{turn}"})}}]},
            {"role": "tool", "tool_call_id": call_id, "content": f"lorem result {turn}",
             "timestamp": 1002.0 + turn * 4},
            {"role": "assistant", "content": f"lorem answer {turn}", "timestamp": 1003.0 + turn * 4},
        ])
    rows[149]["content"] = "lorem credentials sk-" + "a" * 40 + " and ghp_" + "b" * 36
    return rows


@pytest.fixture
def session_get(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.routes as routes
    import api.session_ops as session_ops

    # Fixture placement mirrors _install_test_session in the reconciliation tests.
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    models.Session(session_id=SID, title="Lorem", workspace=str(tmp_path), model="test-model",
                   messages=_rows(), created_at=1000.0, updated_at=1300.0).save(touch_updated_at=False)
    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": True})
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **k: True)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *a, **k: {})
    monkeypatch.setattr(routes, "find_run_summary", lambda *a, **k: None)
    monkeypatch.setattr(session_ops, "regeneration_state", lambda _s: ([], []))
    monkeypatch.setattr(session_ops, "regeneration_authority", lambda *a, **k: None)
    routes._display_merge_cache.clear()

    def get(query):
        handler = _GetHandler(f"/api/session?session_id={SID}&messages=1&resolve_model=0&{query}")
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        return handler.response_json["session"]

    return get


def _expected(full, **window):
    import api.routes as routes

    rows, offset = routes._message_window_for_display(full["messages"], **window)
    return json.loads(json.dumps(rows)), offset


def _assert_slice(get, query, **window):
    full = get("msg_limit=all")
    assert len(full["messages"]) == 300
    expected, offset = _expected(full, **window)
    actual = get(query)
    assert json.loads(json.dumps(actual["messages"])) == expected
    assert actual["_messages_offset"] == offset
    assert actual["_messages_offset"] + len(actual["messages"]) == offset + len(expected)
    return full, actual


def test_limit_30_equals_full_projection_slice(session_get):
    _assert_slice(session_get, "msg_limit=30", msg_limit=30)


def test_limit_100_equals_full_projection_slice(session_get):
    _assert_slice(session_get, "msg_limit=100", msg_limit=100)


def test_before_midpoint_limit_30_equals_full_projection_slice(session_get):
    _assert_slice(session_get, "msg_before=151&msg_limit=30", msg_limit=30, msg_before=151)


def test_boundary_inside_tool_group_preserves_full_slice(session_get):
    full, actual = _assert_slice(
        session_get, "msg_before=151&msg_limit=1", msg_limit=1, msg_before=151
    )
    assert actual["_messages_offset"] == 149
    assert [m["role"] for m in actual["messages"]] == ["assistant", "tool"]
    assert actual["messages"] == full["messages"][149:151]
    assert actual["messages"][1]["tool_call_id"] == "call_37"


def test_redaction_inside_window_equals_full_projection_slice(session_get):
    full, actual = _assert_slice(
        session_get, "msg_before=151&msg_limit=1", msg_limit=1, msg_before=151
    )
    rendered = json.dumps(actual["messages"])
    assert "sk-" + "a" * 40 not in rendered
    assert "ghp_" + "b" * 36 not in rendered
    assert actual["messages"][0]["content"] == full["messages"][149]["content"]
    assert actual["messages"][0]["content"] != _rows()[149]["content"]
    assert "..." in actual["messages"][0]["content"]


def test_after_paging_equals_full_projection_slice(session_get):
    import api.routes as routes

    full = session_get("msg_limit=all")
    anchor = 149
    # The echoed proof must describe only the held prefix [0, anchor + 1).
    held = session_get(f"msg_before={anchor + 1}&msg_limit=30")
    anchor_fp = routes._append_anchor_fingerprint(full["messages"][anchor])
    query = (f"msg_after={anchor}&anchor_fp={anchor_fp}&transcript_generation=0"
             f"&prefix_fp={held['prefix_fp']}&msg_limit=30")
    actual = session_get(query)
    expected, offset = _expected(full, msg_limit=30, msg_after=anchor)
    assert actual["_append_only"] is True
    assert json.loads(json.dumps(actual["messages"])) == expected
    assert actual["_messages_offset"] == offset == anchor + 1
