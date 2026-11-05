# Model-catalog provider visibility and ordering

`model_catalog.excluded_providers` and `model_catalog.preferred_providers` are picker-only configuration. They do not change credentials, provider routing, or the active provider/model.

## Configuration

```yaml
model_catalog:
  excluded_providers:
    - anthropic
  preferred_providers:
    - copilot
    - custom:local-pmbp
```

Both lists accept canonical provider ids or aliases, case-insensitively. Duplicate values are ignored, and a missing/empty/malformed list preserves the previous behavior for that list.

## Rules

- An excluded provider is removed even when it is the active provider or the only configured provider. The emergency one-model catalog must not resurrect it as a fallback group.
- Preferred providers lead in their configured order. Every remaining provider keeps the order produced by the legacy sort: active provider, custom providers, keyed providers, then the rest alphabetically.
- A preferred provider outranks the active provider only in display order; it does not become selected implicitly.
- The full `/api/models` builder and `_static_models_catalog_without_live_probes()` both apply the same rules after their legacy group sort. Preference sorting is stable, so an omitted/empty preference preserves existing order exactly.

## Cache identity

These settings live in `config.yaml`, which is already a conservative source-fingerprint axis under [`models-cache-invalidation.md`](models-cache-invalidation.md). Editing either list changes that file's stat identity and invalidates the in-memory/disk model caches. No new source axis or cached-payload schema change is required.

## Verification

`tests/test_model_catalog_provider_preferences.py` exercises both catalog builders for:

- alias-normalized exclusion;
- preferred-provider priority over the active provider;
- an excluded active provider as the only configured provider, including the minimal-fallback path.
