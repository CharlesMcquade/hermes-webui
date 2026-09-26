"""Opt-in durable late-child assessment ahead of ordinary WebUI delivery.

The Agent ledger owns admission and settlement. This module never ACKs an
ordinary delivery claim for a suppressed child.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)
REVIEW_TIMEOUT_SECONDS = 20


@contextmanager
def owned_delegation_ledger(session_id: str):
    """Resolve the owner's profile before ANY ledger access, including retries.

    The WebUI can carry a different Agent-home override from its import/request
    thread. A lookup in that home can return an unrelated completion or pretend
    an enrolled one is missing. Unknown ownership must never select a ledger.
    """
    from api.models import get_session
    from api.profiles import get_hermes_home_for_profile, _is_root_profile, _validate_profile_name
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    session = get_session(session_id)
    if session is None or str(getattr(session, "session_id", "")) != session_id:
        raise ValueError("Cannot resolve delegation ledger without its owner session")
    profile = getattr(session, "profile", None)
    if profile is not None and not isinstance(profile, str):
        raise ValueError("Invalid delegation owner profile")
    profile = profile or ""
    if profile and not _is_root_profile(profile):
        # get_hermes_home_for_profile deliberately falls back to default for an
        # invalid name. A delegation must not turn that safety fallback into a
        # cross-profile ledger lookup.
        _validate_profile_name(profile)
    home = get_hermes_home_for_profile(profile)
    if not home:
        raise ValueError("Cannot resolve delegation ledger owner profile")
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def late_result_disposition(session_id: str, delegation_id: str) -> str:
    """Fail open to ordinary delivery if the opt-in ledger is unavailable."""
    try:
        from tools.async_delegation import late_result_disposition as disposition
        with owned_delegation_ledger(session_id):
            return disposition(session_id, delegation_id)
    except Exception:
        logger.warning("Late delegation disposition unavailable", exc_info=True)
        return "wake"


def _session_fence(session):
    """Fingerprint the exact persisted conversation and turn identity under its lock."""
    if session is None:
        return None
    fields = ("session_id", "profile", "messages", "active_stream_id",
              "pending_user_message", "pending_user_source", "updated_at")
    values = {key: getattr(session, key, None) for key in fields}
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).digest()


def _review(parent_agent, history, parent_goal, report):
    """Call the detached Agent reviewer; never treat a partial report as assessed."""
    from agent.late_delegation_review import review_late_delegation_result

    child_result = report.get("result") if isinstance(report, dict) else None
    if not isinstance(child_result, (dict, list, str)):
        return "uncertain"
    child_text = (child_result if isinstance(child_result, str) else
                  json.dumps(child_result, ensure_ascii=False, sort_keys=True))
    return review_late_delegation_result(
        parent_agent, parent_goal, child_text, history,
        timeout=REVIEW_TIMEOUT_SECONDS,
    )


def _parent_goal(history):
    """Use the latest real request, not a synthetic child/process notification."""
    for msg in reversed(history):
        if (isinstance(msg, dict) and msg.get("role") == "user"
                and msg.get("_source") not in ("delegation_wakeup", "process_wakeup")):
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def assess_late_result(session_id: str, delegation_id: str) -> str:
    """Return suppressed, wake, or retry; only explicit fenced no_change suppresses.

    `retry` means the triage token could not be settled: do not race it with an
    ordinary claim. Its durable lease and restore sweep recover the record.
    """
    try:
        with owned_delegation_ledger(session_id):
            return _assess_late_result_owned(session_id, delegation_id)
    except Exception:
        logger.warning("Late delegation owner/ledger unavailable; retrying", exc_info=True)
        return "retry"


def _assess_late_result_owned(session_id: str, delegation_id: str) -> str:
    from tools.async_delegation import admit_late_result, settle_late_result, get_durable_delegation
    from api.config import _get_session_agent_lock, SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
    from api.models import get_session
    from api.profiles import get_hermes_home_for_profile
    from api.routes import webui_gateway_chat_enabled, get_config
    from api.background_process import _session_has_active_turn

    token = admit_late_result(session_id, delegation_id)
    if token is None:
        return "wake"
    decision = "wake"
    try:
        lock = _get_session_agent_lock(session_id)
        with lock:
            session = get_session(session_id)
            fence = _session_fence(session)
            if (session is not None and str(session.session_id) == session_id
                    and not _session_has_active_turn(session_id)
                    and not webui_gateway_chat_enabled(get_config())):
                profile_home = str(get_hermes_home_for_profile(getattr(session, "profile", None)))
                with SESSION_AGENT_CACHE_LOCK:
                    entry = SESSION_AGENT_CACHE.get(session_id)
                    agent = entry[0] if entry else None
                    if (agent is None or getattr(agent, "_webui_profile_home", None) != profile_home
                            or getattr(agent, "session_id", None) != session_id):
                        agent = None
                history = list(session.messages) if agent is not None else None
            else:
                agent = None
                history = None
        report = get_durable_delegation(delegation_id)
        goal = _parent_goal(history or [])
        if (agent is not None and history is not None and report is not None
                and report.get("result") is not None and goal):
            if _review(agent, history, goal, report) == "no_change":
                with lock:
                    current = get_session(session_id)
                    if (current is not None and _session_fence(current) == fence
                            and not _session_has_active_turn(session_id)
                            and not webui_gateway_chat_enabled(get_config())):
                        decision = "suppress"
                    # Settlement under the session lock closes the concurrent
                    # parent-turn check/settle window.
                    if settle_late_result(session_id, delegation_id, token, decision):
                        return "suppressed" if decision == "suppress" else "wake"
                    return "retry"
    except Exception:
        logger.warning("Late delegation assessment failed; waking parent", exc_info=True)
    try:
        return "wake" if settle_late_result(session_id, delegation_id, token, "wake") else "retry"
    except Exception:
        logger.warning("Late delegation wake settlement failed", exc_info=True)
        return "retry"
