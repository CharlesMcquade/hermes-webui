"""CDP sidecar relay queue contract tests."""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


class _FakeHandler:
    def __init__(self, body: dict | None = None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.rfile = io.BytesIO(raw)
        self.wfile = self
        self.body = bytearray()
        self.status = None
        self.sent_headers = []
        self.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:8787",
        }
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    @property
    def response_json(self):
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture(autouse=True)
def _reset_sidecar_cdp(monkeypatch):
    from api import routes, sidecar_cdp

    sidecar_cdp._reset_for_tests()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda handler, parsed, body=None, method="GET", emit_error=True: True,
    )
    yield
    sidecar_cdp._reset_for_tests()


def _post(path: str, body: dict | None = None) -> _FakeHandler:
    from api import routes

    handler = _FakeHandler(body)
    handled = routes.handle_post(handler, SimpleNamespace(path=path, query=""))
    assert handled is True or handled is None
    return handler


def _get(path: str) -> _FakeHandler:
    from api import routes

    handler = _FakeHandler({})
    handled = routes.handle_get(handler, urlparse(path))
    assert handled is True or handled is None
    return handler


def test_sidecar_cdp_module_round_trips_command_response():
    from api import sidecar_cdp

    relay = sidecar_cdp.register_relay(
        {"name": "test relay", "extension_id": "ext-a", "version": "1.0"},
        peer="127.0.0.1",
    )
    relay_id = relay["relay_id"]
    result_holder: dict[str, object] = {}

    def send():
        try:
            result_holder["result"] = sidecar_cdp.send_command(
                method="Runtime.evaluate",
                params={"expression": "document.title"},
                target={"tabId": 42},
                timeout=2,
                relay_id=relay_id,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            result_holder["error"] = exc

    thread = threading.Thread(target=send)
    thread.start()
    command = sidecar_cdp.poll(relay_id, timeout_ms=2000)["command"]
    assert command["method"] == "Runtime.evaluate"
    assert command["params"] == {"expression": "document.title"}
    assert command["target"] == {"tabId": 42}

    assert sidecar_cdp.respond(
        relay_id=relay_id,
        command_id=command["command_id"],
        ok=True,
        result={"tabId": 42, "result": {"value": "Hermes"}},
    )["ok"] is True
    thread.join(timeout=3)

    assert "error" not in result_holder
    assert result_holder["result"] == {"tabId": 42, "result": {"value": "Hermes"}}


def test_sidecar_cdp_preserves_missing_target_instead_of_active_fallback():
    from api import sidecar_cdp

    relay_id = sidecar_cdp.register_relay({"extension_id": "ext-a"})["relay_id"]
    result_holder: dict[str, object] = {}

    def send():
        try:
            sidecar_cdp.send_command(
                method="Runtime.evaluate",
                params={"expression": "1 + 1"},
                target=None,
                timeout=2,
                relay_id=relay_id,
            )
        except BaseException as exc:
            result_holder["error"] = exc

    thread = threading.Thread(target=send)
    thread.start()
    command = sidecar_cdp.poll(relay_id, timeout_ms=2000)["command"]
    assert command["method"] == "Runtime.evaluate"
    assert command["target"] is None

    sidecar_cdp.respond(
        relay_id=relay_id,
        command_id=command["command_id"],
        ok=False,
        error="CDP command target is required; refusing implicit active-tab fallback",
    )
    thread.join(timeout=3)

    assert "implicit active-tab fallback" in str(result_holder["error"])


def test_sidecar_cdp_http_routes_register_poll_respond_and_list():
    register = _post(
        "/api/sidecar/cdp/register",
        {
            "name": "Hermes Agent Chrome Extension",
            "extension_id": "japh-test",
            "version": "0.3.1",
            "transport": "webui-tab-long-poll",
        },
    )
    assert register.status == 200
    relay_id = register.response_json["relay_id"]

    relays = _get("/api/sidecar/cdp/relays")
    assert relays.status == 200
    assert relays.response_json["relays"][0]["relay_id"] == relay_id

    result_holder: dict[str, _FakeHandler] = {}

    def command_request():
        result_holder["handler"] = _post(
            "/api/sidecar/cdp/command",
            {"method": "cdp.listTabs", "timeout": 2, "relay_id": relay_id},
        )

    thread = threading.Thread(target=command_request)
    thread.start()
    deadline = time.time() + 3
    while time.time() < deadline:
        if relays := _get("/api/sidecar/cdp/relays").response_json.get("relays"):
            if relays[0].get("pending_commands") == 1:
                break
        time.sleep(0.01)

    poll = _post("/api/sidecar/cdp/poll", {"relay_id": relay_id, "timeout_ms": 2000})
    assert poll.status == 200
    command = poll.response_json["command"]
    assert command["method"] == "__listTabs__"
    assert command["target"] is None

    response = _post(
        "/api/sidecar/cdp/respond",
        {
            "relay_id": relay_id,
            "command_id": command["command_id"],
            "ok": True,
            "result": {"tabs": [{"tabId": 1, "title": "Example"}]},
        },
    )
    assert response.status == 200
    assert response.response_json["ok"] is True
    thread.join(timeout=3)

    command_handler = result_holder["handler"]
    assert command_handler.status == 200
    assert command_handler.response_json == {
        "ok": True,
        "result": {"tabs": [{"tabId": 1, "title": "Example"}]},
    }


def test_sidecar_cdp_get_relays_honors_api_visibility_guard(monkeypatch):
    from api import routes

    _post("/api/sidecar/cdp/register", {"extension_id": "ext-a"})

    def deny(handler, parsed, body=None, method="GET", emit_error=True):
        handler.send_response(403)
        handler.end_headers()
        return False

    monkeypatch.setattr(routes, "_guard_request_session_visibility", deny)

    response = _get("/api/sidecar/cdp/relays")

    assert response.status == 403
    assert response.body == bytearray()


def test_sidecar_cdp_http_command_without_relay_fails_closed():
    response = _post(
        "/api/sidecar/cdp/command",
        {"method": "Runtime.evaluate", "target": {"active": True}, "timeout": 0.01},
    )

    assert response.status == 200
    assert response.response_json["ok"] is False
    assert "No CDP relay extension is connected" in response.response_json["error"]


def test_sidecar_cdp_http_command_lists_relays_without_blocking():
    relay_id = _post("/api/sidecar/cdp/register", {"extension_id": "ext-a"}).response_json[
        "relay_id"
    ]

    response = _post("/api/sidecar/cdp/command", {"method": "cdp.listRelays"})

    assert response.status == 200
    assert response.response_json["ok"] is True
    assert response.response_json["relays"][0]["relay_id"] == relay_id
