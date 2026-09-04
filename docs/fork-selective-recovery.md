# Fork change record: selective recovery

Classification: upstream-candidate (separate logical PRs required).
Maintenance owner: fork maintainer. Upstream status: not-filed.
Private details removed: yes.
Release baseline: `0b1de4fc8b7c0588211517741feccf1622f4c392` (owned deployment
branch). This is a fork release, not a PR against current upstream. Before
contribution, fetch upstream, derive a fresh base, split the concerns below,
and rerun verification on the resulting diffs without unrelated fork history.

## Completion ownership and bounded batches

Problem / actual behavior: completions accumulated during active work can
produce a chain of redundant idle continuations; early acknowledgment conflates
queue ownership with delivered/consumed results.
Expected: one continuation for the accumulated unconsumed batch; preserve each
process identity on transient admission failures and suppress results consumed
through native wait/read-log behavior.
Root cause: per-entry dispatch and competing queue drains with premature ACK.
Change: claim route ownership before ACK, batch pending entries, retire ownership
at disposition. No private active-agent persistence wrapper.
Reproduction / verification: `tests/test_background_completion_delivery.py`,
`tests/test_completion_batch_lifecycle.py`,
`tests/test_completion_core_consumption.py`, `tests/test_wakeup_defer_race.py`.
Tests exercise conflict/error branches, wait versus poll, and 100 batches of
queue cleanup. See [bounded-completion-batches](rfcs/bounded-completion-batches.md).
Compatibility / risk: admission is not successful inference. Queues remain
in-memory. Separately arriving idle completions are not debounced. Registry
contract failure can permit duplicate delivery; stale active-turn completions
retain the existing policy. No crash-durable exactly-once promise.

## Operating overlay delivery

Problem: direct/gateway prompt paths can omit configured guidance or use the
wrong process-global profile when building a session overlay.
Expected: stable, session-profile-owned prompt composition, with native
personality precedence and explicit compatibility behavior.
Change: shared native resolver integration and explicit session-home lookup.
Reproduction / verification: `tests/test_operating_overlay_delivery.py`,
`tests/test_overlay_compatibility.py`, and
`tests/test_webui_gateway_chat_backend.py`.
Compatibility / risk: fallback personality-name matching is case-insensitive;
the older inline path was case-sensitive. This is a compatibility-path behavior
change, not a security boundary or a claim of complete prompt-cache benchmarking.

## Reference-owned config saves

Problem: expanded credentials can be persisted by unrelated config saves,
especially after source rotation or named-provider reordering.
Expected: reference-owned fields retain raw templates; complete replacement
references remain allowed; ordinary non-secret edits retain existing semantics.
Change: use core's shared named traversal with an optional credential-reference
policy, failing closed for ambiguous names or an incompatible helper.
Reproduction / verification: `tests/test_config_env_reference_roundtrip.py` and
`tests/test_config_reference_identity.py`. Synthetic fixtures exercise rotation,
reordering, partial expansion, and failure without file mutation.
Compatibility / risk: requires the core hook introduced in `691bf22139`; new
literal credentials and secrets under unrecognized names are not covered by
this conservative classifier. See [overlay/reference contract](operating-overlay-contract.md).

## Publication hygiene, scope, and rollback

The original unpublished release accidentally tracked a local `.venv` symlink.
The publication branch omits that artifact from its entire new history; the
ignore rule covers both directories and symlinks. Application source and test
behavior are unchanged from the qualified deployed implementation; stale test
comments and the incompatible-helper documentation are corrected. The original local
history is retained locally for recovery, not published as an ancestor.

Excluded: prose-redactor expansion, asset/service-worker redesign, active-turn
persistence wrappers, polling intervals, and provider/capacity changes.

Rollback: restore the WebUI baseline in a separately approved release before
removing the companion core config hook. No schema migration is introduced.
Do not delete or replace a live virtual environment when aligning Git metadata.
Reclassification trigger: upstream adopts equivalent behavior or supplies a
stable API replacing these compatibility seams; remove the superseded delta.

Final publication requires independent review of the complete staged tree,
regression verification, history/path inspection, and exact remote-ref readback.
