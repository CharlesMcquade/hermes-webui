"""Provider exclusion and ordering preferences in every model-catalog builder."""

from __future__ import annotations

import sys
import types

import api.config as config
import api.profiles as profiles
import pytest


def _install_fake_hermes_cli(monkeypatch):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda _pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    config.invalidate_models_cache()


def _catalog_config(model_catalog):
    return {
        "model": {"provider": "anthropic", "default": "claude-sonnet-4-6"},
        "providers": {
            "anthropic": {"api_key": "test-anthropic"},
            "copilot": {"api_key": "test-copilot"},
            "openai-api": {"api_key": "test-openai"},
        },
        "custom_providers": [],
        "model_catalog": model_catalog,
    }


@pytest.fixture
def isolated_catalog(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_cfg_has_in_memory_overrides", lambda: True)
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime

    def _install(model_catalog):
        config.cfg.clear()
        config.cfg.update(_catalog_config(model_catalog))
        config._cfg_mtime = 0.0
        config.invalidate_models_cache()

    yield _install
    config.invalidate_models_cache()
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


def _provider_ids(payload):
    return [group.get("provider_id") for group in payload.get("groups", [])]


@pytest.mark.parametrize("builder", [
    config.get_available_models,
    config._static_models_catalog_without_live_probes,
])
def test_excluded_provider_is_hidden(builder, isolated_catalog):
    isolated_catalog({"excluded_providers": ["claude-code"]})

    provider_ids = _provider_ids(builder())

    assert "anthropic" not in provider_ids, provider_ids
    assert "copilot" in provider_ids, provider_ids
    assert "openai-api" in provider_ids, provider_ids


@pytest.mark.parametrize("builder", [
    config.get_available_models,
    config._static_models_catalog_without_live_probes,
])
def test_preferred_provider_outranks_active_provider(builder, isolated_catalog):
    isolated_catalog({"preferred_providers": ["github"]})

    provider_ids = _provider_ids(builder())

    assert provider_ids[0] == "copilot", provider_ids
    assert "anthropic" in provider_ids[1:], provider_ids


@pytest.mark.parametrize("builder", [
    config.get_available_models,
    config._static_models_catalog_without_live_probes,
])
def test_excluding_only_provider_does_not_leak_through_minimal_fallback(builder, isolated_catalog):
    isolated_catalog({"excluded_providers": ["anthropic"]})
    config.cfg["providers"].pop("copilot")
    config.cfg["providers"].pop("openai-api")
    config.invalidate_models_cache()

    provider_ids = _provider_ids(builder())

    assert "anthropic" not in provider_ids, provider_ids
    assert not provider_ids, provider_ids
