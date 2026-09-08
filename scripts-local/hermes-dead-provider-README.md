# Fallback-Notice Test Rig (dead provider)

One-command scaffolding for testing the persistent fallback banner end-to-end:
a local OpenAI-compatible server that always returns 503, plus a matching
`custom_providers` entry in `~/.hermes/config.yaml`. Pointing a session at it
forces a real provider fallback (dead provider → `fallback_providers` chain)
so the full chain is exercised: agent emission → WebUI matcher → SSE →
persistent `_fallbackNotice` stamp → banner render.

## Redeploy (2 steps)

**1. Start the dead server** (always answers 503 on 127.0.0.1:18082):

```bash
python3 ~/hermes-webui/scripts-local/hermes-dead-provider.py 18082
```

Run it in a background terminal / separate tab. Verify:
`curl -s http://127.0.0.1:18082/v1/chat/completions -X POST -d '{}'` → 503 JSON.

**2. Add the provider to `~/.hermes/config.yaml`** — append inside the
`custom_providers:` list (indent to match sibling `- name:` entries):

```yaml
  - name: vllm-dead-test
    base_url: http://127.0.0.1:18082/v1
    api_key: not-needed
    api_mode: chat_completions
    models:
      dead-model-test:
        context_length: 131072
        max_output_tokens: 4096
        supports_tools: true
        supports_reasoning: false
        model_family: openai
        notes: 'Fallback-notice test provider: dead-end server on 18082 always returns 503. Pair with scripts-local/hermes-dead-provider.py. Safe to remove when not testing.'
    discover_models: false
```

Notes:
- `context_length` must be ≥ 64000 (Hermes minimum) or the agent refuses to start.
- The provider catalog hot-reloads from config changes — no WebUI restart needed.
- Indentation in config.yaml has shifted across upstream refactors; match the
  sibling `- name:` entries rather than copying the above blindly.

## Run the test

1. In a WebUI session: `/model dead-model-test` (or pick `custom:vllm-dead-test`)
2. Send any message
3. Expect 2 retry hiccups (~3s), then the fallback model answers with the
   persistent "⚠️ Model fallback: … using <model> via <provider>" banner stamped
   into the transcript.
4. Settings → "Show fallback notices" must be ON (it's a persisted toggle).

## Teardown

```bash
lsof -nP -iTCP:18082 -sTCP:LISTEN -t | xargs kill
```

…then delete the `vllm-dead-test` block from `config.yaml` (lines from
`- name: vllm-dead-test` through its `discover_models: false`).

## CLI variant (no browser needed)

```bash
hermes chat -q "Say: fallback test complete" --provider custom:vllm-dead-test -m dead-model-test
```

Expect the same fallback in `~/.hermes/logs/agent.log`
(`Fallback activated: dead-model-test → …`). For a callback-level probe that
bypasses the WebUI entirely, the pattern used on 2026-08-27 lives in the
session transcript: construct `AIAgent(provider=..., model=..., 
status_callback=spy, fallback_model=cfg['fallback_providers'])` and run
`run_conversation` — the spy should receive
`('warn', '⚠️ Model fallback: …')`.
