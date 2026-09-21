# PR #7318 UI evidence

Captured from an isolated WebUI instance using disposable `HERMES_HOME` and `HERMES_WEBUI_STATE_DIR`; no live user state or service was touched.

## Scenario A — persisted malformed generated title self-heals

A session begins with a persisted generated title containing a `Good title options:` menu. The real title eligibility, persistence, SSE delivery, and browser listener paths replace it with `Repository Layout Guide`.

- `A-before-desktop.png` / `A-after-desktop.png`
- `A-before-narrow.png` / `A-after-narrow.png`
- `A-before-mobile.png` / `A-after-mobile.png`

## Scenario B — fresh malformed candidate remains unresolved

A session begins with the provisional title `hello there`. A malformed multi-option candidate is rejected; the provisional title remains and `llm_title_generated` remains false.

- `B-after-desktop.png`
- `B-after-narrow.png`
- `B-after-mobile.png`

## Boundaries

The isolated environment had no healthy external model provider. Scenario A patches the title route to return a deterministic valid title. Scenario B stubs `generate_title_raw_via_agent` to return a deterministic malformed candidate. In both scenarios the raw output still passes through the production sanitizer/classifier, and the downstream eligibility, persistence, SSE channel, and browser listener paths are production code.

`evidence-results.json` records the observed server events, persisted state, and browser frames. The 404/400 console entries are expected unavailable optional endpoints in the isolated harness and did not affect the title flow.
