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
    "through the Hermes WebUI. You are the VOICE FRONT-END for a much more "
    "capable agent (also Verity) that has tools, memory, files, and the web. "
    "Rules: (1) For anything requiring facts, tools, memory, personal data, "
    "files, code, home automation, or actions, call the ask_verity function "
    "with a clear self-contained question — do not guess or answer from your "
    "own knowledge. (2) Only answer directly for trivial conversational "
    "fillers (greetings, acknowledgements, repeating what was just said). "
    "(3) While ask_verity is running, if the user speaks, respond briefly and "
    "naturally. (4) Summarize tool results conversationally and concisely — "
    "speak like a person, not a report. Keep responses short unless asked."
)

ASK_VERITY_TOOL = {
    "type": "function",
    "name": "ask_verity",
    "description": (
        "Ask the full Hermes agent (Verity) anything. It has terminal, web, "
        "files, memory, smart home, email, and every other tool. Use for ALL "
        "substantive requests. May take from seconds up to a few minutes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Self-contained question or instruction for the agent.",
            }
        },
        "required": ["question"],
    },
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

    session_config = json.dumps(
        {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": VOICE_INSTRUCTIONS,
            "tools": [ASK_VERITY_TOOL],
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
