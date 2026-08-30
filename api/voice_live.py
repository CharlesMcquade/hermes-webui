"""Live voice (OpenAI Realtime over WebRTC) backend for the WebUI.

POST /api/voice/live/sdp
    JSON body: {"sdp": "<webrtc offer sdp>", "voice": "marin"?}
    Forwards the offer to https://api.openai.com/v1/realtime/calls using the
    server-side OPENAI_API_KEY (never exposed to the browser) and returns the
    answer SDP as text/plain JSON: {"ok": true, "sdp": "..."}.

GET /api/voice/live/capability
    {"available": bool} — whether an OpenAI API key is configured.

Design notes:
- The session config (model, tools, instructions) is constructed SERVER-side.
  The browser cannot inject instructions or tools; it may only pick a voice
  from a small allowlist.
- The realtime model is deliberately given exactly one function tool,
  ``ask_verity``, and instructed to route any substantive request through it.
  The frontend executes that tool by calling the existing synchronous
  /api/chat endpoint, so the voice layer is a thin conversational shim over
  the full Hermes agent (tools, memory, skills intact).
- Auth mirrors /api/tts: when WebUI auth is enabled the session cookie is
  required. A small per-client rate limit bounds abuse.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

from api.helpers import j, bad, read_body

REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls"
REALTIME_MODEL = "gpt-realtime"
ALLOWED_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
DEFAULT_VOICE = "marin"

VOICE_INSTRUCTIONS = (
    "You are Verity, a warm, dry-witted voice assistant for Charles, speaking "
    "through the Hermes WebUI. You are the AGENT SURFACE: you ARE the agent "
    "for this conversation, and a full Hermes agent is your toolbox — it has "
    "terminal, files, web, memory, email, smart home, everything.\n"
    "CONTEXT: You are given a digest of the user's current chat session. Use "
    "it — never ask what you were just talking about; you already know.\n"
    "TOOLS — think of yourself as directing the agent, not waiting on it:\n"
    "(1) agent_ask: launch a task on the agent. Returns immediately with a "
    "run handle. Use for anything requiring facts, tools, files, actions, or "
    "multi-step work. Phrase it as a complete, self-contained instruction.\n"
    "(2) agent_status: check what the run is doing right now (current step, "
    "recent tools). Call it when the user asks what's happening or you want "
    "an update before speaking.\n"
    "(3) agent_steer: refine the ACTIVE run mid-flight — 'actually only "
    "direct flights', 'also check Tuesday'. Delivered at the next tool "
    "boundary without restarting. If it fails, tell the user plainly.\n"
    "(4) agent_stop: cancel the active run ('never mind', 'actually stop').\n"
    "Only ONE run per session at a time. agent_ask returns {busy:true} if "
    "one is already running — steer it instead of restarting.\n"
    "WHILE A RUN IS ACTIVE: narrate progress as it arrives ([voice progress] "
    "notes), keep chatting naturally, take refinements via agent_steer, and "
    "summarize the result conversationally when the run completes.\n"
    "STYLE: sound like a person, not a report. Short by default. Errors: "
    "say what failed and suggest the next step."
)

ASK_VERITY_TOOL = {
    "type": "function",
    "name": "agent_ask",
    "description": (
        "Launch a task on the full Hermes agent (terminal, web, files, "
        "memory, email, smart home — everything). Returns a run handle "
        "immediately; the run continues in the background and you narrate "
        "its progress. May take from seconds up to a few minutes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Complete, self-contained task or question for the agent.",
            }
        },
        "required": ["question"],
    },
}

AGENT_STATUS_TOOL = {
    "type": "function",
    "name": "agent_status",
    "description": (
        "Check what the agent run is doing right now: active or finished, "
        "current step, recent tools used. Use when the user asks for a "
        "status or before giving an unsolicited update."
    ),
    "parameters": {"type": "object", "properties": {}},
}

AGENT_STEER_TOOL = {
    "type": "function",
    "name": "agent_steer",
    "description": (
        "Refine the ACTIVE agent run mid-flight without restarting it — "
        "e.g. 'actually only direct flights', 'skip that step'. Delivered "
        "at the agent's next tool boundary. Fails if no run is active."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The course-correction for the in-flight run.",
            }
        },
        "required": ["text"],
    },
}

AGENT_STOP_TOOL = {
    "type": "function",
    "name": "agent_stop",
    "description": (
        "Cancel the ACTIVE agent run entirely ('never mind', 'actually "
        "stop', 'forget it'). Fails softly if no run is active."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _read_env_file_key(name: str) -> str:
    try:
        env_path = Path(os.path.expanduser("~/.hermes")) / ".env"
        if not env_path.exists():
            return ""
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return val
    except Exception:
        pass
    return ""


def _resolve_openai_key() -> str:
    return (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or _read_env_file_key("OPENAI_API_KEY")
    )


def handle_voice_live_capability(handler):
    return j(handler, {"available": bool(_resolve_openai_key())})


_RATE_LOCK = threading.Lock()
_RATE_LAST: dict[str, float] = {}
_RATE_WINDOW = 2.0  # seconds between session mints per client


def _rate_limited(client: str) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        last = _RATE_LAST.get(client, 0.0)
        if now - last < _RATE_WINDOW:
            return True
        _RATE_LAST[client] = now
        if len(_RATE_LAST) > 256:
            cutoff = now - 60.0
            for k in [k for k, v in _RATE_LAST.items() if v < cutoff]:
                _RATE_LAST.pop(k, None)
    return False


def handle_voice_live_sdp(handler):
    """Mint a Realtime WebRTC call: forward the browser's SDP offer to OpenAI."""
    if handler.command != "POST":
        return bad(handler, "POST required", 405)

    from api.auth import is_auth_enabled, parse_cookie, verify_session

    if is_auth_enabled():
        cv = parse_cookie(handler)
        if not (cv and verify_session(cv)):
            return bad(handler, "unauthorized", 401)

    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    sdp = str(data.get("sdp") or "")
    if not sdp.startswith("v=0"):
        return bad(handler, "sdp offer required", 400)
    if len(sdp) > 256 * 1024:
        return bad(handler, "sdp too large", 400)

    voice = str(data.get("voice") or DEFAULT_VOICE).strip().lower()
    if voice not in ALLOWED_VOICES:
        voice = DEFAULT_VOICE

    client = str(getattr(handler, "client_address", ("?",))[0])
    if _rate_limited(client):
        return bad(handler, "rate limited — try again shortly", 429)

    api_key = _resolve_openai_key()
    if not api_key:
        return bad(handler, "no OpenAI API key configured on this server", 503)

    # Optional session binding: the frontend binds the call first
    # (/api/voice/live/connect) and can pass the digest to enrich instructions.
    instructions = VOICE_INSTRUCTIONS
    digest = str(data.get("digest") or "").strip()
    if digest:
        instructions = (
            instructions
            + "\n\nCURRENT SESSION DIGEST (live context — you already know this):\n"
            + digest[:6000]
        )

    session_config = json.dumps(
        {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": instructions,
            "tools": [
                ASK_VERITY_TOOL,
                AGENT_STATUS_TOOL,
                AGENT_STEER_TOOL,
                AGENT_STOP_TOOL,
            ],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                },
                "output": {"voice": voice},
            },
        }
    )

    # multipart/form-data body: fields "sdp" and "session"
    boundary = "hermesvoice" + os.urandom(8).hex()
    parts = []
    for name, value in (("sdp", sdp), ("session", session_config)):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode("utf-8")

    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        REALTIME_CALLS_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "OpenAI-Safety-Identifier": "hermes-webui-voice",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            answer_sdp = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print(f"[webui] voice live: OpenAI {e.code}: {detail}", flush=True)
        return j(handler, {"error": f"OpenAI realtime error ({e.code})"}, status=502)
    except Exception as e:
        print(f"[webui] voice live: {e}", flush=True)
        return j(handler, {"error": "failed to reach OpenAI realtime"}, status=502)

    if not answer_sdp.startswith("v=0"):
        print(f"[webui] voice live: unexpected answer: {answer_sdp[:200]}", flush=True)
        return j(handler, {"error": "unexpected response from OpenAI realtime"}, status=502)

    return j(handler, {"ok": True, "sdp": answer_sdp})


# ── v2: session binding, digest, transcript mirror, YOLO handoff ─────────────


def _require_webui_session(handler, sid: str):
    """Validate a session id for voice use. Returns (session, error_response)."""
    from api.models import is_safe_session_id

    sid = str(sid or "").strip()
    if not sid or not is_safe_session_id(sid):
        return None, bad(handler, "invalid session_id", 400)
    try:
        from api.routes import _get_or_materialize_session
        s = _get_or_materialize_session(sid)
    except Exception:
        return None, bad(handler, "Session not found", 404)
    if s is None:
        return None, bad(handler, "Session not found", 404)
    return s, None


def _build_session_digest(s) -> str:
    """Compact digest of the current session for the realtime instructions."""
    lines: list[str] = []
    title = str(getattr(s, "title", "") or "")
    if title and title != "Untitled":
        lines.append(f"Current session title: {title}")
    ws = str(getattr(s, "created_workspace", "") or "")
    if ws:
        lines.append(f"Workspace: {ws}")
    msgs = list(getattr(s, "messages", None) or [])
    recent = [m for m in msgs if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()][-8:]
    if recent:
        lines.append("Recent conversation (oldest first):")
        for m in recent:
            c = " ".join(str(m.get("content") or "").split())
            if len(c) > 400:
                c = c[:400] + "…"
            lines.append(f"- {m.get('role')}: {c}")
    else:
        lines.append("The session has no prior conversation; this is a fresh start.")
    return "\n".join(lines)


def _auth_ok(handler) -> bool:
    from api.auth import is_auth_enabled, parse_cookie, verify_session

    if not is_auth_enabled():
        return True
    cv = parse_cookie(handler)
    return bool(cv and verify_session(cv))


def handle_voice_live_connect(handler):
    """POST /api/voice/live/connect

    Binds a realtime call to a session: {session_id, voice?}.
    Enables session YOLO for the duration of the call (explicit user opt-in via
    the voice button) and returns the session digest so the browser can pass
    richer instructions at SDP mint time. Prior YOLO state is stored server-side
    (survives page refresh) and restored by disconnect.
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _require_webui_session(handler, data.get("session_id"))
    if err is not None:
        return err

    from api.route_approvals import (
        enable_session_yolo,
        is_session_yolo_enabled,
    )

    sid = s.session_id
    with _BIND_LOCK:
        prior = _VOICE_BINDINGS.get(sid)
        if prior is not None:
            # Rebind (e.g. page refresh mid-call): the YOLO state currently on
            # is OURS from the previous binding, so the true pre-call state is
            # the stored one — never re-read the flag we enabled ourselves.
            was_enabled = bool(prior.get("yolo_was_enabled"))
        else:
            was_enabled = bool(is_session_yolo_enabled(sid))
        _VOICE_BINDINGS[sid] = {"yolo_was_enabled": was_enabled, "ts": time.time()}
    if not was_enabled:
        enable_session_yolo(sid)
    yolo_now = bool(is_session_yolo_enabled(sid))

    print(f"[webui] voice live: session {sid} bound (yolo={yolo_now}, was={was_enabled})", flush=True)
    return j(
        handler,
        {
            "ok": True,
            "session_id": sid,
            "yolo_enabled": yolo_now,
            "yolo_was_enabled": was_enabled,
            "digest": _build_session_digest(s),
        },
    )


# Live voice call bindings: session_id -> {yolo_was_enabled, ts}. Server-side
# ownership of the YOLO handoff so a page refresh (which kills the browser JS
# before disconnect fires) cannot orphan a session with YOLO left on.
_VOICE_BINDINGS: dict[str, dict] = {}
_BIND_LOCK = threading.Lock()


def handle_voice_live_disconnect(handler):
    """POST /api/voice/live/disconnect — {session_id}.

    Restores the session's pre-call YOLO state from the server-side binding
    record (also reachable via sendBeacon on page unload — CSRF-exempt because
    the worst case is RESTRICTING privilege, never granting it). Idempotent.
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _require_webui_session(handler, data.get("session_id"))
    if err is not None:
        return err

    from api.route_approvals import disable_session_yolo, set_session_yolo_enabled, is_session_yolo_enabled

    sid = s.session_id
    with _BIND_LOCK:
        binding = _VOICE_BINDINGS.pop(sid, None)
    was_enabled = bool(binding.get("yolo_was_enabled")) if binding else None
    if was_enabled is None:
        # No binding (never connected, already unbound, or server restarted):
        # leave YOLO exactly as it is — do not guess.
        pass
    elif was_enabled:
        set_session_yolo_enabled(sid, True)
    else:
        disable_session_yolo(sid)

    print(f"[webui] voice live: session {sid} unbound (yolo now={is_session_yolo_enabled(sid)})", flush=True)
    return j(handler, {"ok": True, "yolo_enabled": bool(is_session_yolo_enabled(sid))})


def handle_voice_live_turn(handler):
    """POST /api/voice/live/turn — {session_id, user_text, assistant_text?}

    Appends a voice exchange to the session transcript WITHOUT triggering an
    agent turn. The user's spoken question (input transcription) and the voice
    model's spoken reply are mirrored so the visible transcript, session
    search, and memory all see voice activity.
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _require_webui_session(handler, data.get("session_id"))
    if err is not None:
        return err

    user_text = str(data.get("user_text") or "").strip()
    assistant_text = str(data.get("assistant_text") or "").strip()
    if not user_text and not assistant_text:
        return bad(handler, "user_text or assistant_text required", 400)

    now = time.time()
    added = 0
    if user_text:
        s.messages.append({
            "role": "user",
            "content": "[voice] " + user_text[:8000],
            "id": _next_message_id(s),
            "timestamp": now,
        })
        added += 1
    if assistant_text:
        s.messages.append({
            "role": "assistant",
            "content": "[voice] " + assistant_text[:8000],
            "id": _next_message_id(s),
            "timestamp": now,
        })
        added += 1
    if added:
        try:
            s.save()
        except Exception as e:
            print(f"[webui] voice live: transcript mirror save failed: {e}", flush=True)
            return j(handler, {"error": "failed to persist transcript"}, status=500)
    return j(handler, {"ok": True, "appended": added})


def _next_message_id(s) -> int:
    ids = [m.get("id") for m in getattr(s, "messages", []) if isinstance(m.get("id"), int)]
    return (max(ids) + 1) if ids else len(getattr(s, "messages", []) or []) + 1


def handle_voice_live_ask(handler):
    """POST /api/voice/live/ask — {session_id, question}

    Non-blocking deep lane: fires the session's real agent turn via the same
    machinery the composer uses (POST /api/chat/start), so the run uses the
    session's model, tools, skills, and memory — and returns {stream_id}
    immediately. The bridge polls /api/chat/stream/status and fetches the
    final answer via GET /api/session when the stream finishes.
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _require_webui_session(handler, data.get("session_id"))
    if err is not None:
        return err

    question = str(data.get("question") or "").strip()
    if not question:
        return bad(handler, "question required", 400)

    # Busy-guard: if the session already has an active stream, do NOT spawn a
    # concurrent turn (fixes the relay-collision failure mode). Report it so
    # the voice model can tell the user instead of hanging.
    current_stream_id = getattr(s, "active_stream_id", None)
    if current_stream_id:
        from api.routes import STREAMS, STREAMS_LOCK, _active_stream_blocks_chat_start

        with STREAMS_LOCK:
            running = current_stream_id in STREAMS
        if running and _active_stream_blocks_chat_start(s, current_stream_id):
            return j(
                handler,
                {
                    "busy": True,
                    "stream_id": current_stream_id,
                    "message": "The agent is still working on the previous request.",
                },
            )

    # Reuse the real chat-start path by calling the route handler directly.
    # This keeps all invariants (locks, stale-stream cleanup, workspace,
    # model resolution) identical to a typed composer turn.
    from api.routes import _handle_chat_start

    class _ProxyHandler:
        """Minimal proxy so _handle_chat_start writes into our JSON response."""

        def __init__(self, inner, body):
            self._inner = handler
            self.command = "POST"
            self.client_address = getattr(handler, "client_address", ("127.0.0.1", 0))
            self._body = json.dumps(body).encode()
            self.headers = {"Content-Length": str(len(self._body)), "Content-Type": "application/json"}
            self.rfile = io.BytesIO(self._body)
            self.wfile = io.BytesIO()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def send_response(self, status):
            self.status = status

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    proxy = _ProxyHandler(handler, {"session_id": s.session_id, "message": question})
    try:
        _handle_chat_start(proxy, {"session_id": s.session_id, "message": question})
    except Exception as e:
        print(f"[webui] voice live: ask dispatch failed: {e}", flush=True)
        return j(handler, {"error": "failed to start agent turn"}, status=500)

    resp = {}
    raw = proxy.wfile.getvalue()
    try:
        resp = json.loads(raw.decode()) if raw else {}
    except Exception:
        resp = {}
    status = getattr(proxy, "status", 200) or 200

    if status >= 400:
        # 409 with active_stream_id = busy; surface as structured busy, not error
        if status == 409 and resp.get("active_stream_id"):
            return j(handler, {"busy": True, "stream_id": resp["active_stream_id"],
                               "message": "The agent is already working on a request."}, status=200)
        return j(handler, {"error": resp.get("error") or f"chat start failed ({status})"}, status=502)

    if not resp.get("stream_id"):
        return j(handler, {"error": "chat start returned no stream_id"}, status=502)

    return j(handler, {"ok": True, "stream_id": resp["stream_id"], "session_id": s.session_id})


# ── realtime-as-agent-surface: steer / status / cancel ────────────────────────
#
# Design: the realtime model is the agent SURFACE (orchestrator); the Hermes
# agent is the toolbox executor. Voice gets lifecycle control over the session's
# single agent run: launch (ask), watch (progress), steer mid-flight, cancel.
# One run per session at a time is a feature, not a limit — the run IS the
# session's agent activity, identical to what a typed turn would do.


def _voice_binding_sid(handler, data):
    s, err = _require_webui_session(handler, data.get("session_id"))
    if err is not None:
        return None, err
    return s, None


def handle_voice_live_steer(handler):
    """POST /api/voice/live/steer — {session_id, text}

    Injects course-correction into the session's ACTIVE agent run without
    interrupting it (same machinery as /steer in the composer). Refines an
    in-flight request: "actually use the direct flight", "also check Tuesday".
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _voice_binding_sid(handler, data)
    if err is not None:
        return err

    text = str(data.get("text") or "").strip()
    if not text:
        return bad(handler, "text required", 400)

    from api.streaming import _handle_chat_steer

    class _ProxyHandler:
        def __init__(self, inner, body):
            self._inner = handler
            self.command = "POST"
            self.client_address = getattr(handler, "client_address", ("127.0.0.1", 0))
            self._body = json.dumps(body).encode()
            self.headers = {"Content-Length": str(len(self._body)), "Content-Type": "application/json"}
            self.rfile = io.BytesIO(self._body)
            self.wfile = io.BytesIO()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def send_response(self, status):
            self.status = status

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    proxy = _ProxyHandler(handler, {"session_id": s.session_id, "text": text})
    try:
        _handle_chat_steer(proxy, {"session_id": s.session_id, "text": text})
    except Exception as e:
        print(f"[webui] voice live: steer failed: {e}", flush=True)
        return j(handler, {"accepted": False, "error": "steer failed"}, status=500)

    raw = proxy.wfile.getvalue()
    try:
        resp = json.loads(raw.decode()) if raw else {}
    except Exception:
        resp = {}
    if resp.get("accepted"):
        return j(handler, {"ok": True, "stream_id": resp.get("stream_id")})
    return j(handler, {
        "ok": False,
        "reason": resp.get("fallback") or "not_accepted",
        "message": "No active run to steer" if resp.get("fallback") in ("not_running", "stream_dead", "session_not_found")
                   else "The agent could not accept steering right now",
    })


def handle_voice_live_status(handler):
    """POST /api/voice/live/status — {session_id}

    Compact orchestration view for the realtime model: is a run active, what
    step is it on (from the run journal), recent tool names.
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _voice_binding_sid(handler, data)
    if err is not None:
        return err

    sid = s.session_id
    active_stream_id = getattr(s, "active_stream_id", None)
    running = False
    if active_stream_id:
        from api.routes import STREAMS, STREAMS_LOCK

        with STREAMS_LOCK:
            running = active_stream_id in STREAMS

    out = {
        "ok": True,
        "session_id": sid,
        "active": running,
        "stream_id": active_stream_id if running else None,
    }

    if running:
        try:
            from api.run_journal import find_run_summary, read_run_events

            summary = find_run_summary(active_stream_id)
            if summary:
                out["last_event"] = summary.get("last_event")
                out["last_seq"] = summary.get("last_seq")
                journal = read_run_events(sid, active_stream_id)
                events = [e for e in (journal.get("events") or []) if isinstance(e, dict)]
                # last few tool names + latest interim assistant text
                tool_names = []
                last_interim = ""
                for e in events:
                    ev = e.get("event") or e.get("type")
                    payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                    if ev == "tool" and payload.get("name"):
                        tool_names.append(str(payload["name"]))
                    elif ev == "interim_assistant" and payload.get("text"):
                        last_interim = str(payload["text"])[:200]
                out["recent_tools"] = tool_names[-5:]
                out["latest_step"] = last_interim
        except Exception as e:
            print(f"[webui] voice live: status journal read failed: {e}", flush=True)

    return j(handler, out)


def handle_voice_live_stop(handler):
    """POST /api/voice/live/stop — {session_id}

    Cancels the session's active agent run (voice "stop" / "never mind").
    Same machinery as the composer's Stop button.
    """
    if handler.command != "POST":
        return bad(handler, "POST required", 405)
    if not _auth_ok(handler):
        return bad(handler, "unauthorized", 401)
    try:
        data = read_body(handler)
    except Exception:
        return bad(handler, "invalid request body", 400)

    s, err = _voice_binding_sid(handler, data)
    if err is not None:
        return err

    sid = s.session_id
    active_stream_id = getattr(s, "active_stream_id", None)
    if not active_stream_id:
        return j(handler, {"ok": True, "cancelled": False, "message": "No active run"})

    from api.routes import STREAMS, STREAMS_LOCK

    with STREAMS_LOCK:
        running = active_stream_id in STREAMS
    if not running:
        return j(handler, {"ok": True, "cancelled": False, "message": "No active run"})

    from api.streaming import cancel_stream

    try:
        result = cancel_stream(active_stream_id)
    except Exception as e:
        print(f"[webui] voice live: stop failed: {e}", flush=True)
        return j(handler, {"ok": False, "error": "cancel failed"}, status=500)

    cancelled = bool(result.get("cancelled")) if isinstance(result, dict) else bool(result)
    return j(handler, {"ok": True, "cancelled": cancelled, "stream_id": active_stream_id})
