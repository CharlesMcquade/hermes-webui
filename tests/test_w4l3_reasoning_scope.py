"""S02: provider/model-scoped reasoning effort resolution.

Freezes the override-key serialization (``provider:model``, provider
lowercased) and the legacy model-only map migration. Precedence at actual
execution: explicit run request > provider:model scoped override > model-only
override > global ``agent.reasoning_effort`` > provider default (None).
"""
import pytest

from api import config


def _write_cfg(tmp_path, monkeypatch, cfg):
    cfg_path = tmp_path / "config.yaml"
    import yaml
    cfg_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    return cfg_path


def test_missing_resolver_symbol_is_the_gap():
    assert hasattr(config, "resolve_reasoning_effort_for_request"), (
        "S02 canonical resolver must exist in api/config.py"
    )


def test_requested_effort_wins_over_stored_overrides(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {
        "reasoning_effort": "low",
        "reasoning_overrides": {"m1": "high", "prov:m1": "medium"},
    }})
    resolved = config.resolve_reasoning_effort_for_request(
        model_id="m1", provider_id="prov", requested="xhigh")
    assert resolved == "xhigh"


@pytest.mark.parametrize("provider", ["Prov", "PROV", "prov"])
def test_provider_scoped_key_serialization_is_frozen(tmp_path, monkeypatch, provider):
    _write_cfg(tmp_path, monkeypatch, {"agent": {
        "reasoning_effort": "low",
        "reasoning_overrides": {"m1": "high", "prov:m1": "medium"},
    }})
    for model_id in ("m1", "prov:m1"):
        resolved = config.resolve_reasoning_effort_for_request(
            model_id=model_id, provider_id=provider)
        # provider:model scoped override beats model-only and global; the
        # model id itself may carry the provider prefix — the resolver strips it.
        assert resolved == "medium", (model_id, provider)


def test_model_only_map_migrates_when_no_provider_key(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {
        "reasoning_effort": "low",
        "reasoning_overrides": {"m1": "high"},
    }})
    assert config.resolve_reasoning_effort_for_request(
        model_id="m1", provider_id="other") == "high"


def test_global_fallback_and_unset(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {"reasoning_effort": "low"}})
    assert config.resolve_reasoning_effort_for_request(
        model_id="m1", provider_id="prov") == "low"
    _write_cfg(tmp_path, monkeypatch, {})
    assert config.resolve_reasoning_effort_for_request(
        model_id="m1", provider_id="prov") is None


def test_missing_or_invalid_values_fall_through_safely(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {
        "reasoning_effort": "low",
        "reasoning_overrides": {"m1": "bogus", "prov:m1": "high"},
    }})
    # Invalid model-only override falls through to the global default.
    assert config.resolve_reasoning_effort_for_request(model_id="m1") == "low"
    # Invalid requested effort is ignored, stored scoped value still applies.
    assert config.resolve_reasoning_effort_for_request(
        model_id="m1", provider_id="prov", requested="turbo") == "high"


def test_same_model_slug_across_providers(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {
        "reasoning_overrides": {"gpt-x": "low", "openai:gpt-x": "high", "nous:gpt-x": "minimal"},
    }})
    assert config.resolve_reasoning_effort_for_request(
        model_id="gpt-x", provider_id="openai") == "high"
    assert config.resolve_reasoning_effort_for_request(
        model_id="gpt-x", provider_id="nous") == "minimal"


def test_set_reasoning_effort_writes_frozen_provider_scoped_key(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {}})
    monkeypatch.setattr(config, "reload_config", lambda: None)
    config.set_reasoning_effort("high", model_id="m1", provider_id="OpenAI")
    cfg = config._load_yaml_config_file(config._get_config_path())
    overrides = cfg["agent"]["reasoning_overrides"]
    assert overrides == {"openai:m1": "high"}
    # Read path round-trips.
    assert config.resolve_reasoning_effort_for_request(
        model_id="m1", provider_id="openai") == "high"
    # Clearing removes only the scoped key.
    config.set_reasoning_effort("", model_id="m1", provider_id="openai")
    cfg = config._load_yaml_config_file(config._get_config_path())
    assert "reasoning_overrides" not in (cfg.get("agent") or {})


def test_namespaced_custom_ids_are_not_mangled(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"agent": {}})
    monkeypatch.setattr(config, "reload_config", lambda: None)
    config.set_reasoning_effort("low", model_id="slash/model:id", provider_id="custom")
    cfg = config._load_yaml_config_file(config._get_config_path())
    assert cfg["agent"]["reasoning_overrides"] == {"custom:slash/model:id": "low"}
