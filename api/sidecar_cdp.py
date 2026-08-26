"""In-process CDP relay queue for browser sidecar extensions.

The Hermes Agent Chrome Extension registers over authenticated WebUI HTTP
requests from a signed-in tab, long-polls for commands, executes them through
``chrome.debugger``, and posts responses back here. Agent tools running in the
same WebUI process import this module directly so they can enqueue a command and
wait for the browser-side response without needing Chrome remote-debugging flags
or the newer gateway WebSocket route.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import queue
import threading
import time
import uuid

_RELAY_STALE_SECONDS = 120.0
_MAX_POLL_TIMEOUT_SECONDS = 30.0
_MAX_COMMAND_TIMEOUT_SECONDS = 300.0
_MAX_PENDING_COMMANDS_PER_RELAY = 100
_MAX_QUEUED_COMMANDS_PER_RELAY = 100


@dataclass
class _PendingCommand:
    command_id: str
    created_at: float
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str = ""


@dataclass
class _Relay:
    relay_id: str
    name: str = ""
    extension_id: str = ""
    version: str = ""
    transport: str = ""
    peer: str = ""
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    commands: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=_MAX_QUEUED_COMMANDS_PER_RELAY)
    )
    pending: dict[str, _PendingCommand] = field(default_factory=dict)


_LOCK = threading.RLock()
_RELAYS: dict[str, _Relay] = {}


def _clamp_timeout(value: Any, *, default: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        parsed = default
    return max(0.001, min(parsed, maximum))


def _public_relay(relay: _Relay) -> dict[str, Any]:
    return {
        "relay_id": relay.relay_id,
        "name": relay.name,
        "extension_id": relay.extension_id,
        "version": relay.version,
        "transport": relay.transport,
        "peer": relay.peer,
        "registered_at": relay.registered_at,
        "last_seen": relay.last_seen,
        "queued_commands": relay.commands.qsize(),
        "pending_commands": len(relay.pending),
    }


def _prune_stale_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    stale = [
        relay_id
        for relay_id, relay in _RELAYS.items()
        if now - relay.last_seen > _RELAY_STALE_SECONDS and not relay.pending
    ]
    for relay_id in stale:
        _RELAYS.pop(relay_id, None)


def _resolve_relay_locked(relay_id: str | None = None) -> _Relay:
    _prune_stale_locked()
    if relay_id:
        relay = _RELAYS.get(str(relay_id))
        if relay is None:
            raise RuntimeError("CDP relay not found")
        return relay
    if not _RELAYS:
        raise RuntimeError("No CDP relay extension is connected")
    if len(_RELAYS) > 1:
        raise RuntimeError("Multiple CDP relays are connected; specify relay_id")
    return next(iter(_RELAYS.values()))


def register_relay(info: dict[str, Any] | None = None, *, peer: str = "") -> dict[str, Any]:
    """Register or replace one browser extension relay."""
    info = dict(info or {})
    relay_id = uuid.uuid4().hex[:12]
    extension_id = str(info.get("extension_id") or "")[:160]
    now = time.time()
    relay = _Relay(
        relay_id=relay_id,
        name=str(info.get("name") or "")[:160],
        extension_id=extension_id,
        version=str(info.get("version") or "")[:80],
        transport=str(info.get("transport") or "")[:80],
        peer=str(peer or info.get("peer") or "")[:160],
        registered_at=now,
        last_seen=now,
    )
    with _LOCK:
        if extension_id:
            for existing_id, existing in list(_RELAYS.items()):
                if existing.extension_id == extension_id:
                    _fail_pending_locked(existing, "CDP relay replaced")
                    _RELAYS.pop(existing_id, None)
        _RELAYS[relay_id] = relay
        return {"ok": True, "relay_id": relay_id, "status": "registered"}


def unregister_relay(relay_id: str | None) -> dict[str, Any]:
    relay_id = str(relay_id or "").strip()
    if not relay_id:
        raise ValueError("relay_id required")
    with _LOCK:
        relay = _RELAYS.pop(relay_id, None)
        if relay is not None:
            _fail_pending_locked(relay, "CDP relay unregistered")
    return {"ok": True, "relay_id": relay_id, "status": "unregistered"}


def list_relays() -> list[dict[str, Any]]:
    with _LOCK:
        _prune_stale_locked()
        return [_public_relay(relay) for relay in _RELAYS.values()]


def poll(relay_id: str | None, *, timeout_ms: Any = 25000) -> dict[str, Any]:
    relay_id = str(relay_id or "").strip()
    if not relay_id:
        raise ValueError("relay_id required")
    timeout_seconds = _clamp_timeout(
        float(timeout_ms or 0) / 1000.0,
        default=25.0,
        maximum=_MAX_POLL_TIMEOUT_SECONDS,
    )
    with _LOCK:
        relay = _RELAYS.get(relay_id)
        if relay is None:
            raise RuntimeError("CDP relay not found")
        relay.last_seen = time.time()
        command_queue = relay.commands
    try:
        command = command_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        with _LOCK:
            relay = _RELAYS.get(relay_id)
            if relay is not None:
                relay.last_seen = time.time()
        return {"ok": True, "relay_id": relay_id, "command": None}
    with _LOCK:
        relay = _RELAYS.get(relay_id)
        if relay is not None:
            relay.last_seen = time.time()
    return {"ok": True, "relay_id": relay_id, "command": command}


def _normalize_special_method(method: str) -> str:
    method = str(method or "").strip()
    if method == "cdp.listTabs":
        return "__listTabs__"
    if method == "cdp.detach":
        return "__detach__"
    return method


def send_command(
    *,
    method: str,
    params: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    timeout: Any = 30.0,
    relay_id: str | None = None,
) -> Any:
    """Queue a CDP command for a connected relay and wait for its response.

    This function intentionally preserves the caller-provided target verbatim.
    It does not insert an implicit active-tab fallback; the extension enforces
    target and unsafe-command policy at the browser boundary.
    """
    method = _normalize_special_method(method)
    if not method:
        raise ValueError("method required")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    if target is not None and not isinstance(target, dict):
        raise ValueError("target must be an object")
    timeout_seconds = _clamp_timeout(
        timeout,
        default=30.0,
        maximum=_MAX_COMMAND_TIMEOUT_SECONDS,
    )
    command_id = f"cdp-{uuid.uuid4().hex[:12]}"
    pending = _PendingCommand(command_id=command_id, created_at=time.time())
    command = {
        "command_id": command_id,
        "method": method,
        "params": params or {},
        "target": target,
        "timeout": timeout_seconds,
    }
    with _LOCK:
        relay = _resolve_relay_locked(relay_id)
        if len(relay.pending) >= _MAX_PENDING_COMMANDS_PER_RELAY:
            raise RuntimeError("CDP relay has too many pending commands")
        relay.pending[command_id] = pending
        try:
            relay.commands.put_nowait(command)
        except queue.Full as exc:
            relay.pending.pop(command_id, None)
            raise RuntimeError("CDP relay command queue is full") from exc
    if not pending.event.wait(timeout=timeout_seconds):
        with _LOCK:
            relay = _RELAYS.get(str(relay_id or "")) if relay_id else None
            if relay is None:
                try:
                    relay = _resolve_relay_locked(relay_id)
                except RuntimeError:
                    relay = None
            if relay is not None:
                relay.pending.pop(command_id, None)
        raise RuntimeError(f"CDP command timed out after {timeout_seconds:g}s")
    if pending.error:
        raise RuntimeError(f"CDP relay error: {pending.error}")
    return pending.result or {}


def respond(
    *,
    relay_id: str | None,
    command_id: str | None,
    ok: bool,
    result: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    relay_id = str(relay_id or "").strip()
    command_id = str(command_id or "").strip()
    if not relay_id:
        raise ValueError("relay_id required")
    if not command_id:
        raise ValueError("command_id required")
    with _LOCK:
        relay = _RELAYS.get(relay_id)
        if relay is None:
            return {"ok": False, "error": "CDP relay not found"}
        relay.last_seen = time.time()
        pending = relay.pending.pop(command_id, None)
    if pending is None:
        return {"ok": False, "error": "CDP command is no longer pending"}
    if ok:
        pending.result = result
    else:
        pending.error = str(error or "CDP command failed")
    pending.event.set()
    return {"ok": True, "command_id": command_id}


def _fail_pending_locked(relay: _Relay, reason: str) -> None:
    for pending in list(relay.pending.values()):
        pending.error = reason
        pending.event.set()
    relay.pending.clear()


def _reset_for_tests() -> None:
    with _LOCK:
        for relay in list(_RELAYS.values()):
            _fail_pending_locked(relay, "reset")
        _RELAYS.clear()
