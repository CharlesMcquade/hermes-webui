# Operating overlays and credential references

## Contract routing

Runtime prompt composition: `api/streaming.py`, `api/gateway_chat.py`.
Configuration persistence: `api/config.py`.
Related index: `docs/CONTRACTS.md`. No session schema, transport, or capability-budget change.

## Contract change

Direct and gateway-backed sessions resolve the operating overlay from the session's profile. Explicit session personality selection overrides the profile selection. Native precedence applies when available: known named personality supersedes `agent.system_prompt`; neutral/unknown selection uses that manual overlay. This is default guidance, not an unconditional security boundary.

On older/standalone installs without the native resolver, configured personality definitions and the manual overlay remain available. Built-in-only personalities require the companion resolver. No import failure is allowed to disable ordinary chat. Gateway prompt/prefill lookup uses the session profile explicitly and does not silently use another profile if lookup raises; gateway transport and reasoning configuration are unchanged by this patch.

The shared builder does not mutate supplied configuration or persisted messages. Identical inputs produce identical output. Existing frozen agent prefixes are not forcibly rewritten. Worker regressions pin one forwarded overlay; native downstream composition across every possible companion version is outside this WebUI-only fixture.

Configuration saves preserve unchanged `${VAR}` templates. Credential-named fields and secret-named references remain reference-owned even if a rotation/thread change means their runtime values differ. A complete replacement reference is allowed; expanded or partly expanded secret templates are restored. To change an inline value at a protected field, first change its secret source rather than silently materializing a credential through an unrelated save. Non-secret value changes remain ordinary edits when the paired helper is available. If the companion helper is absent or incompatible, all configuration saves fail closed with an error, including non-secret-only saves; no file is written.

The name classifier is deliberately conservative, not an arbitrary-secret detector. Newly introduced literal credentials, or secrets hidden under non-secret names that change between read/save, are not made safe by this mechanism. The independent live-secret scanner remains required.

## Verification

`test_operating_overlay_delivery.py`, `test_overlay_compatibility.py`, `test_config_env_reference_roundtrip.py`, plus existing real streaming/gateway worker and surface-context tests cover these boundaries. Synthetic fixtures only; tests must not print live credentials on failure.
