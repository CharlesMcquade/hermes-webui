"""POST /api/session/new must not block on a cold model-catalog rebuild.

session/new only normalizes the requested model/provider, so it resolves from
the cached catalog (prefer_cached_catalog=True). With no cache at all it keeps
the request verbatim. Non-forced followers of an in-flight rebuild wait at
most the live-rebuild budget instead of 60 s.
"""

from __future__ import annotations

import threading
import time

_CATALOG = {
    "active_provider": "openai-codex",
    "default_model": "gpt-5.4",
    "configured_model_badges": {},
    "groups": [
        {"provider": "OpenAI Codex", "provider_id": "openai-codex",
         "models": [{"id": "gpt-5.4", "label": "gpt-5.4"}]},
    ],
}


def test_cold_cache_session_new_does_not_block_and_keeps_request(monkeypatch):
    from api import config as cfg
    import api.routes as routes

    cfg.invalidate_models_cache()
    release = threading.Event()

    def _slow_rebuild(_builder):
        release.wait(timeout=6.0)  # stand-in for a 6 s provider fetch
        return dict(_CATALOG)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_rebuild, raising=True)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None, raising=True)
    try:
        t0 = time.monotonic()
        model, provider = routes._session_model_state_from_request(
            "@zzprov:some-model", "zzprov"
        )
        elapsed = time.monotonic() - t0
    finally:
        release.set()
        time.sleep(0.05)
        cfg.invalidate_models_cache()
    assert elapsed < 1.0, f"session/new model resolve blocked {elapsed:.2f}s"
    assert (model, provider) == ("@zzprov:some-model", "zzprov")


def test_warm_cache_selection_unchanged(monkeypatch):
    import api.routes as routes

    def _warm(prefer_cache=False, force_refresh=False):
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _CATALOG.items()}

    monkeypatch.setattr(routes, "get_available_models", _warm, raising=True)
    stale = "gemini/gemini-2.5-pro"
    before = routes._resolve_compatible_session_model_state(stale, None)
    after = routes._session_model_state_from_request(stale, None)
    assert before[2] is True, "known stale model should still be normalized"
    assert after == (before[0], before[1])
