"""Terminal SSE payloads must not surface consumed OOB steer wrappers (#7600 gap).

A mid-turn ``/steer`` reaches the Agent as a ``steer_user_row`` — a standalone
typed user row wrapped in the ``[OUT-OF-BAND USER MESSAGE …]`` control frame.
Settlement (#7600) unwraps typed rows before persistence, but the terminal
``done``/``apperror`` session payload embeds the live conversation BEFORE
settlement runs, and the Agent's embedded copy of the steer row can arrive
without its ``display_kind: 'steer'`` typing (observed with the Codex backend:
run-journal ``done`` event for session 60a3104a4258, message index 1446). The
typed-row-only unwrap then skips it and the settled transcript renders the raw
control wrapper to the user.

``_session_payload_with_full_messages`` is the shared builder for every terminal
SSE session payload (``done``, ``apperror``, cancel, gateway chat), so the scrub
lives there: any user row whose ENTIRE content is exactly one complete,
well-formed OOB frame is unwrapped; rows are copied, never mutated.
"""
from __future__ import annotations

import copy

from api.models import Session
from api.streaming import (
    _scrub_oob_wrapped_user_rows_for_payload,
    _session_payload_with_full_messages,
)

OOB_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
OOB_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
OOB_BLOCK = f"{OOB_OPEN}\nYou have to install it in /Applications\n{OOB_CLOSE}"


def _payload_messages(session):
    return _session_payload_with_full_messages(session)["messages"]


def test_payload_scrub_unwraps_untyped_oob_user_row():
    """The production shape: an untyped user row that is exactly one OOB frame."""
    messages = [
        {"role": "user", "content": "install 1password", "timestamp": 1},
        {"role": "assistant", "content": "working", "timestamp": 2},
        {"role": "user", "content": OOB_BLOCK, "timestamp": 3},
        {"role": "assistant", "content": "Done.", "timestamp": 4},
    ]
    scrubbed = _scrub_oob_wrapped_user_rows_for_payload(copy.deepcopy(messages))

    assert scrubbed[2]["content"] == "You have to install it in /Applications"
    assert "OUT-OF-BAND" not in str(scrubbed)


def test_payload_scrub_is_non_mutating():
    """Live session rows are settlement-owned: the scrub copies, never mutates."""
    steer_row = {"role": "user", "content": OOB_BLOCK, "timestamp": 3}
    messages = [
        {"role": "user", "content": "install 1password", "timestamp": 1},
        steer_row,
    ]
    _scrub_oob_wrapped_user_rows_for_payload(messages)

    assert steer_row["content"] == OOB_BLOCK, "live row was mutated by the payload scrub"


def test_payload_scrub_preserves_quoted_and_malformed_rows_byte_for_byte():
    """Prose that quotes a frame, nested/multiple/incomplete frames stay intact."""
    quoted = (
        "our bot log shows this block, is that normal?\n"
        f"{OOB_BLOCK}\n"
        "the docs say the gateway adds it"
    )
    nested = f"{OOB_OPEN}\nhello {OOB_CLOSE} world\n{OOB_CLOSE}"
    multiple = f"{OOB_CLOSE} and {OOB_BLOCK}"
    incomplete = "[OUT-OF-BAND USER MESSAGE — truncated"
    typed_steer = {
        "role": "user",
        "display_kind": "steer",
        "content": OOB_BLOCK,
        "timestamp": 3,
    }

    messages = [
        {"role": "user", "content": quoted, "timestamp": 1},
        {"role": "user", "content": nested, "timestamp": 2},
        {"role": "user", "content": multiple, "timestamp": 3},
        {"role": "user", "content": incomplete, "timestamp": 4},
        typed_steer,
        {"role": "tool", "content": f"output\n\n{OOB_BLOCK}", "timestamp": 5},
    ]
    scrubbed = _scrub_oob_wrapped_user_rows_for_payload(copy.deepcopy(messages))

    assert scrubbed[0]["content"] == quoted
    assert scrubbed[1]["content"] == nested
    assert scrubbed[2]["content"] == multiple
    assert scrubbed[3]["content"] == incomplete
    # Typed steer rows are settlement's job (#7600); the payload scrub leaves them
    # byte-for-byte rather than double-processing.
    assert scrubbed[4]["content"] == OOB_BLOCK
    assert scrubbed[4]["display_kind"] == "steer"
    # Tool rows are never touched.
    assert scrubbed[5]["content"] == f"output\n\n{OOB_BLOCK}"


def test_session_payload_with_full_messages_scrubs_oob_wrapper():
    """The shared terminal-payload builder emits the settled, unwrapped shape."""
    display = [
        {"role": "user", "content": "install 1password", "timestamp": 1},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}], "timestamp": 2},
        {"role": "tool", "tool_call_id": "call-1", "content": "installed", "timestamp": 3},
        # The Agent's embedded copy: wrapped content, display_kind lost in transit.
        {"role": "user", "content": OOB_BLOCK, "timestamp": 4},
        {"role": "assistant", "content": "Done.", "timestamp": 5},
    ]
    session = Session(session_id="60a3104a4258", title="oob payload scrub", messages=copy.deepcopy(display))
    session.context_messages = copy.deepcopy(display)

    payload = _session_payload_with_full_messages(session)
    rows = payload["messages"]

    assert payload["message_count"] == len(rows)
    steer_rows = [m for m in rows if "install it in /Applications" in str(m.get("content"))]
    assert len(steer_rows) == 1
    assert steer_rows[0]["content"] == "You have to install it in /Applications"
    assert "OUT-OF-BAND" not in str(rows)
    # The live session object is untouched.
    assert session.messages[3]["content"] == OOB_BLOCK
