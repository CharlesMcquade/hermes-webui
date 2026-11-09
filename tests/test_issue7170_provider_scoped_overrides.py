"""Provider-scoped reasoning overrides retain PR #7170's resolution contract."""

import sys

import pytest

from api import config as cfg


@pytest.fixture(params=("core", "fallback"), autouse=True)
def _exercise_core_and_fallback_paths(request, monkeypatch):
    """Scoped selection must run before either canonical matcher path."""
    if request.param == "fallback":
        monkeypatch.setitem(sys.modules, "hermes_constants", None)
    # Isolate key selection from provider capability probes and clamps.
    monkeypatch.setattr(
        cfg, "coerce_reasoning_effort_for_model", lambda effort, *args, **kwargs: effort
    )


def _config(overrides, global_effort="low"):
    return {
        "agent": {
            "reasoning_effort": global_effort,
            "reasoning_overrides": overrides,
        }
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"prov:m1": "medium", "m1": "high"}, "medium"),
        ({"prov:m1": "medium"}, "medium"),
        ({"m1": "high"}, "high"),
        ({}, "low"),
    ],
    ids=("scoped-beats-bare", "scoped-beats-global", "bare-control", "global-control"),
)
def test_provider_scoped_override_precedence_from_repro(overrides, expected):
    assert cfg.configured_reasoning_effort_for_model(
        _config(overrides), model_id="m1", provider_id="prov"
    ) == expected


@pytest.mark.parametrize(
    "overrides",
    [{"prov:m1": "medium", "m1": "high"}, {"prov:m1": "medium"}],
    ids=("bare-remains-provider-independent", "unmatched-scope-uses-global"),
)
def test_provider_scope_does_not_leak_to_another_provider(overrides):
    expected = "high" if "m1" in overrides else "low"
    assert cfg.configured_reasoning_effort_for_model(
        _config(overrides), model_id="m1", provider_id="other"
    ) == expected


def test_scoped_key_is_case_insensitive_and_preserves_colons_in_identity():
    config_data = _config({"CUSTOM:Example:M1:free": "medium", "m1:free": "high"})
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="m1:free", provider_id="custom:example"
    ) == "medium"


def test_explicit_session_effort_still_precedes_scoped_config():
    config_data = _config({"prov:m1": "medium", "m1": "high"})
    resolve = cfg.resolve_session_reasoning_effort
    assert resolve(config_data, session_effort="xhigh", model_id="m1", provider_id="prov") == "xhigh"
    assert resolve(config_data, session_effort="", model_id="m1", provider_id="prov") == ""
    assert resolve(config_data, session_effort=None, model_id="m1", provider_id="prov") == "medium"


def test_scoped_disable_uses_core_boolean_semantics():
    assert cfg.configured_reasoning_effort_for_model(
        _config({"prov:m1": False, "m1": "high"}),
        model_id="m1",
        provider_id="prov",
    ) == "none"


def test_bare_qualified_and_dot_dash_aliases_remain_tolerant():
    config_data = _config({"openai/gpt-5.4-mini": "low"}, global_effort="high")
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="gpt-5-4-mini", provider_id="openai"
    ) == "low"


def test_selected_scoped_effort_still_passes_through_coercion(monkeypatch):
    config_data = _config({"prov:m1": "medium", "m1": "high"})
    calls = []

    def _coerce(effort, model_id=None, **kwargs):
        calls.append((effort, model_id, kwargs))
        return "minimal"

    monkeypatch.setattr(cfg, "coerce_reasoning_effort_for_model", _coerce)
    assert cfg.configured_reasoning_effort_for_model(
        config_data, model_id="m1", provider_id="prov"
    ) == "minimal"
    assert calls == [
        (
            "medium",
            "m1",
            {"provider_id": "prov", "base_url": None, "config_data": config_data},
        )
    ]
