"""Shared-Agent title failures re-enter the adopted #7539 recovery contract."""
from contextlib import nullcontext
import types
from unittest.mock import MagicMock, patch

import pytest

import api.streaming as streaming


@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("shared_status", ("shared_title_empty", "shared_title_error"))
def test_failed_shared_candidate_uses_raw_recovery(monkeypatch, route, shared_status):
    """Only a shared success is final; its empty/error outcomes are not a
    transport/config verdict and must not bypass the real schema path."""
    shared = MagicMock(return_value=(None, shared_status, ""))
    monkeypatch.setattr(streaming, "_generate_shared_session_title", shared)
    monkeypatch.setattr(streaming, "_title_provider_name_ambiguous", lambda *_args: False)
    raw_status = "llm_aux_retry" if route == "aux" else "llm_retry"
    raw_function = streaming.generate_title_raw_via_aux if route == "aux" else streaming.generate_title_raw_via_agent
    raw = MagicMock(return_value=("Schema Recovered Title", raw_status))
    monkeypatch.setattr(streaming, raw_function.__name__, raw)

    agent = object()
    if route == "aux":
        result = streaming._generate_llm_session_title_via_aux("Fix login.", "Inspect login.")
    else:
        result = streaming._generate_llm_session_title_for_agent(agent, "Fix login.", "Inspect login.")

    assert result == ("Schema Recovered Title", raw_status, "")
    shared.assert_called_once_with("Fix login.")
    raw.assert_called_once()


@pytest.mark.parametrize("route", ("aux", "agent"))
def test_shared_open_vocab_success_is_not_generated_twice(monkeypatch, route):
    shared_title = "[Mobile] Login button repair"
    shared = MagicMock(return_value=(shared_title, "shared_title", ""))
    monkeypatch.setattr(streaming, "_generate_shared_session_title", shared)
    monkeypatch.setattr(streaming, "_title_provider_name_ambiguous", lambda *_args: False)
    aux_raw = MagicMock(side_effect=AssertionError("shared success must not reach aux raw"))
    agent_raw = MagicMock(side_effect=AssertionError("shared success must not reach agent raw"))
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", aux_raw)
    monkeypatch.setattr(streaming, "generate_title_raw_via_agent", agent_raw)

    if route == "aux":
        result = streaming._generate_llm_session_title_via_aux("Fix login.", "Inspect login.")
    else:
        result = streaming._generate_llm_session_title_for_agent(object(), "Fix login.", "Inspect login.")

    assert result == (shared_title, "shared_title", "")
    shared.assert_called_once_with("Fix login.")
    aux_raw.assert_not_called()
    agent_raw.assert_not_called()


@pytest.mark.parametrize(
    ("message", "raw_status"),
    (
        ("HTTP 401 Unauthorized: token rejected response_format", "llm_error_aux"),
        ("connection error: normalized schema URL failed json_schema", "llm_error_aux"),
    ),
)
def test_shared_failure_preserves_raw_transport_classification(monkeypatch, message, raw_status):
    """Normalized URL/token failures still use the raw route's transport class;
    naming schema fields does not turn them into successful schema recovery or
    into invalid model output."""
    from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config

    calls = []
    monkeypatch.setattr(
        streaming,
        "_generate_shared_session_title",
        MagicMock(return_value=(None, "shared_title_error", "")),
    )

    def fail_normalized_route(**kwargs):
        calls.append(kwargs)
        raise RuntimeError(message)

    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=fail_normalized_route, create=True):
            result = streaming._generate_llm_session_title_via_aux("Fix login.", "Inspect login.")

    assert result == (None, raw_status, "")
    # Both bounded raw prompts report the same transport class; neither schema
    # failure is re-sent as a compatibility success or an invalid trace.
    assert len(calls) == 2
    assert all("response_format" in call["extra_body"] for call in calls)


@pytest.mark.parametrize("raw_status", ("llm_invalid_trace_aux", "llm_empty_aux", "llm_length_aux"))
def test_raw_failure_status_survives_shared_empty(monkeypatch, raw_status):
    monkeypatch.setattr(
        streaming,
        "_generate_shared_session_title",
        MagicMock(return_value=(None, "shared_title_empty", "")),
    )
    monkeypatch.setattr(streaming, "_title_provider_name_ambiguous", lambda *_args: False)
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", MagicMock(return_value=(None, raw_status)))

    assert streaming._generate_llm_session_title_via_aux(
        "Fix login.", "Inspect login."
    ) == (None, raw_status, "")


def test_manual_regeneration_keeps_raw_schema_recovery_success(monkeypatch):
    """A raw LLM success after shared failure is final, not replaced by the
    deterministic exchange-summary fallback."""
    session = types.SimpleNamespace(
        session_id="shared-recovery-manual",
        messages=[
            {"role": "user", "content": "Fix login."},
            {"role": "assistant", "content": "Inspect login."},
        ],
    )
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(
        streaming,
        "_generate_shared_session_title",
        MagicMock(return_value=(None, "shared_title_error", "")),
    )
    monkeypatch.setattr(streaming, "_title_provider_name_ambiguous", lambda *_args: False)
    monkeypatch.setattr(
        streaming,
        "generate_title_raw_via_aux",
        MagicMock(return_value=("Recovered Manual Title", "llm_aux_retry")),
    )
    fallback = MagicMock(side_effect=AssertionError("raw recovery success must not use local fallback"))
    monkeypatch.setattr(streaming, "_fallback_title_from_exchange", fallback)

    assert streaming.generate_session_title_for_session(session) == (
        "Recovered Manual Title", "llm_aux_retry", "",
    )
    fallback.assert_not_called()


def test_provider_ambiguity_refusal_precedes_shared_generation(monkeypatch):
    monkeypatch.setattr(streaming, "_title_provider_name_ambiguous", lambda *_args: True)
    shared = MagicMock(side_effect=AssertionError("provider refusal must be final"))
    raw = MagicMock(side_effect=AssertionError("provider refusal must be final"))
    monkeypatch.setattr(streaming, "_generate_shared_session_title", shared)
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", raw)

    assert streaming._generate_llm_session_title_via_aux(
        "Fix login.", "Inspect login."
    ) == (None, "provider_name_ambiguous_aux", "")
    shared.assert_not_called()
    raw.assert_not_called()
