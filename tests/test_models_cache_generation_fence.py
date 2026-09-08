"""Regression tests: stale out-of-band models-cache publication (issue #4756).

The bounded live-catalog rebuild (``HERMES_WEBUI_MODELS_REBUILD_BUDGET``,
default 4s) runs ``_build_available_models_uncached`` on a daemon worker. When
the probe overruns the budget, the foreground returns a fallback and the
worker publishes out-of-band when it finishes.

Race found by the flaky ``test_named_provider_uses_name_as_group_header``
failure (Sep 6 2026 loop-fix session): the out-of-band publication captured
the config BEFORE ``invalidate_models_cache()`` / a config.yaml edit, and
landed AFTER the invalidation — so the cache holds a catalog built from
pre-invalidation state, stamped with a NEW timestamp/fingerprint. Every later
caller gets the stale catalog until the 24h TTL expires.

Fix contract: the worker's publication must be refused when the cache was
invalidated after the rebuild started. A monotonic generation counter bumps on
every invalidation; the worker only publishes if the generation still matches
the one captured at rebuild start.

These tests drive the REAL ``get_available_models`` bounded path with a
stubbable rebuild hook, no network, no 4s sleeps (monkeypatched budget).
"""
import time

import pytest

import api.config as config


@pytest.fixture(autouse=True)
def _fast_budget(monkeypatch):
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.15)
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_over_budget_publication_refused_after_invalidate(monkeypatch):
    """Worker finishes AFTER invalidate_models_cache(): its stale result must
    NOT land in the cache — the next caller rebuilds fresh."""
    calls = {"n": 0}

    def slow_rebuild():
        calls["n"] += 1
        time.sleep(0.5)  # worker-side: far over the 0.15s budget
        return {
            "active_provider": None,
            "default_model": "stale-model",
            "configured_model_badges": {},
            "groups": [{"provider": "Stale", "models": [{"id": "stale-model", "label": "stale"}]}],
        }

    original = config._invoke_models_rebuild

    def hook(fn):
        return original(slow_rebuild)

    monkeypatch.setattr(config, "_invoke_models_rebuild", hook)

    first = config.get_available_models()
    # Foreground served the fallback (worker still running).
    assert calls["n"] == 1
    groups_now = [g["provider"] for g in first.get("groups", [])]

    # The caller invalidates while the worker is still building...
    config.invalidate_models_cache()

    # ...and the worker's stale publication must never land.
    # The out-of-band publish runs ~0.5s after start; give it ample time.
    landed = _wait_for(lambda: config._available_models_cache is not None)
    assert not landed or [g["provider"] for g in config._available_models_cache.get("groups", [])] != ["Stale"], (
        "stale out-of-band publication landed after invalidate_models_cache() — "
        "generation fence missing or broken"
    )
    assert groups_now is not None


def test_over_budget_publication_lands_when_not_invalidated(monkeypatch):
    """Without an intervening invalidation the out-of-band publication still
    warms the cache (the over-budget feature must keep working)."""

    def slow_rebuild():
        time.sleep(0.4)
        return {
            "active_provider": None,
            "default_model": "slow-model",
            "configured_model_badges": {},
            "groups": [{"provider": "Warm", "models": [{"id": "slow-model", "label": "slow"}]}],
        }

    original = config._invoke_models_rebuild

    def hook(fn):
        return original(slow_rebuild)

    monkeypatch.setattr(config, "_invoke_models_rebuild", hook)

    first = config.get_available_models()
    assert [g["provider"] for g in first.get("groups", [])] != ["Warm"], (
        "foreground must serve the fast fallback when the probe overruns budget"
    )
    assert _wait_for(
        lambda: config._available_models_cache is not None
        and [g["provider"] for g in config._available_models_cache.get("groups", [])] == ["Warm"]
    ), "out-of-band publication must still warm the cache when nothing invalidated it"


def test_generation_counter_bumps_on_invalidate():
    gen_before = config._models_cache_generation
    config.invalidate_models_cache()
    assert config._models_cache_generation == gen_before + 1
