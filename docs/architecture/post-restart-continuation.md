# Private local post-restart continuation

This is an opt-in, **settled-session-only** continuation consumer for one owning
WebUI process on a local POSIX filesystem. It is not general crash recovery,
a Gateway/runner feature, or an HTTP endpoint. No browser must remain open.

## Supported envelope

- The supervisor selects an immutable Git checkout/release and explicitly sets
  `HERMES_WEBUI_RUNTIME_ADAPTER=legacy-direct` or `legacy-journal`, and
  `HERMES_WEBUI_CHAT_BACKEND=legacy`. The consumer has no browser profile context,
  so a config-only default is insufficient. Missing/unknown values are **not** evidence of local
  ownership, even though ordinary chat has legacy defaults.
- The session has an existing WebUI sidecar and profile `state.db` row with
  explicit `source=webui`, matching nonempty `profile_name`, and open lifecycle.
  The workspace, model and provider must already be bound. No fallback, import,
  workspace repair, profile retagging, or compression redirect is authorized.
- Capture the binding only after the previous turn has settled. Persisted
  pending input/active stream metadata is refused, not cleared. This slice does
  **not** resume an interrupted in-flight turn or adopt a live CLI/desktop run.
- Unknown ownership/schema, external sources, Gateway routing, runner mode,
  delegated children, closed/archived/read-only/compressed/missing sessions,
  changed revisions, and aliased/missing workspaces are refused.
- One WebUI instance owns ordinary admissions and the session database. Manual
  concurrent CLI/SQL writes, concurrent administration of bindings/closures,
  shared/network filesystems, multiple owning WebUI servers, and mutable release
  deployments are outside this contract. `flock` protects the private receipt
  transaction; it does **not** fence nonparticipating Agent/database writers.

## Supervisor protocol (private files, not public authentication)

Set `HERMES_WEBUI_CONTINUATION_DIR` to an existing absolute canonical directory
owned by the WebUI UID with mode `0700`. All ancestors must be root/current-UID
owned, not group/world writable, and not symlinks. Records must be regular,
single-link, current-UID-owned `0600` files. Reads/open/create use retained
parent directory descriptors and `O_NOFOLLOW`. Symlinks, FIFOs, hard links,
wrong permissions, oversized and malformed records fail closed.

The **trusted supervisor** owns request/proof publication. This patch supplies
the owning-WebUI consumer, not a supervisor-specific producer or a public arm
API. Deployment tooling must implement this protocol before enabling it. There
is no assumption that an existing controller already emits these records.

1. At a settled checkpoint, obtain `inspect_binding(session_id, profile,
   workspace)` from `api.post_restart_continuation`. It reads the sidecar and
   profile SQLite database without `Session.load()` recovery writes. The result
   binds exact session/profile/workspace/model/provider plus a SHA-256 digest
   of the entire sidecar and durable session/message rows. No binding is inferred
   from the currently selected browser profile.
2. Publish immutable `deployment.json` in the private store **before startup**,
   with exactly `release` (canonical WebUI source directory), `release_revision`
   (full 40/64-character lowercase source commit from the independently verified
   frozen build inventory), and `execution_digest` (the `digest()` of the mapping
   from each execution filename to its SHA-256). The execution files are
   `server.py`, `api/routes.py`, `api/post_restart_continuation.py`,
   `api/runtime_adapter.py`, and `api/streaming.py`. `release_binding()` validates
   the installed path and those bytes against this supervisor-owned record.
   Production startup needs no Git checkout, git/network process, or package
   installation. In a development checkout only, absent `deployment.json` falls
   back to reading Git HEAD metadata plus the same execution digest. Unknown
   package identity fails closed; identity never comes from `request.json`.
   The new owner captures this at startup and rechecks it at admission.
3. Generate one immutable 32-character lowercase hex
   `operation_admission_id`. Atomically publish and fsync `request.json`:

   ```json
   {
     "operation_admission_id": "<32 lowercase hex characters>",
     "controller_operation_id": "<exact independent controller operation ID>",
     "deployment": "<release_binding() object, not a string>",
     "binding": "<inspect_binding() object, not a string>",
     "runtime_adapter": "legacy-direct",
     "message": "Continue the previously requested task."
   }
   ```

   The object placeholders above must be replaced by actual objects. Preserve
   the controller's existing operation ID exactly (1–128 characters from
   letters, digits, `.`, `_`, `:`, `-`); do not normalize or substitute a new
   controller ID to make validation pass. The admission ID is a separate token.
   Do not
   overwrite this request while an operation is in progress. The private
   message is capped at 32768 characters; empty and `[SILENT]` input is refused.
4. Stop/drain the previous owner, launch the selected release, and independently
   establish the controller's successful terminal operation outcome, including
   the exact new owner PID and its `server_started_at` value from HTTP health.
   Health alone is **not** terminal operation proof. Only then atomically publish
   and fsync `terminal.json`:

   ```json
   {
     "status": "terminal_verified",
     "operation_admission_id": "<same admission ID>",
     "controller_operation_id": "<same controller operation ID>",
     "request_digest": "<digest(request)>",
     "owner_pid": 12345,
     "owner_started_at": 1234567890.125,
     "deployment": "<same release_binding() object>"
   }
   ```

   The proof is an assertion by the trusted same-UID supervisor, not a signed
   attestation from an untrusted caller. The consumer checks all bindings; it
   cannot independently prove a supervisor's claimed terminal workflow. Never
   synthesize terminal proof merely because a server responds to health.
5. `server.py` starts a daemon consumer without blocking the HTTP accept loop.
   It waits for missing proof/request and retries live busy admission. A refused
   request stops this startup consumer; it does not repair/rewrite the request.
   In-process `consume_post_restart_continuation(deployment)` is the same private
   entrypoint, useful to a trusted controller integration, not an HTTP route.

## Admission, receipts and ambiguity

With this feature enabled, ordinary `_handle_chat_start`, `start_session_turn`,
`_start_run` and legacy stream starts participate in one per-session reentrant
outer admission edge. It precedes workspace/model/pending mutation. The consumer
holds the same edge through binding revalidation, durable claim and launch.
Ordinary HTTP access/profile checks are not bypassed. An already-busy session is
rejected before ordinary starts can modify its workspace/model. Without the
continuation environment variable, ordinary calls use the unchanged path.

Lock order: private store flock (consumer only), outer session admission,
existing session/stream registry locks. Never acquire the outer edge from inside
an existing session/stream lock. The worker receipt uses only the private store
lock; it does not call back into admission. HTTP response writes are not under
stream/runtime registry locks. Admission lock identities live for the process
lifetime, as do the existing session lock identities.

The first durable record is `<id>.claim.json`, created exclusively and fsynced
with its parent directory **before** loading/recovering the session, mutating
pending metadata or starting a worker. It binds the immutable ID to the exact
request digest. Any subsequent exception, partial claim write, lost response,
or process death is **not permission to replay**. A second consumer or a fresh
process reads the same claim. The PID-sharded turn journal remains observation,
not dedup authority.

The normal accepted path calls the existing `_start_run` adapter selection and
legacy stream start, not a separate worker launcher. The actual executing
thread writes `<id>.entered.json` before calling the Agent worker. If that
receipt fails, the worker is not invoked and the existing launch-failure cleanup
retires its stream/pending state. `<id>.result.json` records the owning route's
result with the same ID. A returned route result is not worker-entry proof;
`entered.json` is not proof of model completion or an exactly-once tool effect.

Keep claims indefinitely for the retry horizon. Never delete a claim or assign
a fresh ID automatically to overcome an ambiguous result. The supervisor/user
must inspect the session and decide on a new action. This provides a reliable
normal launch and conservative no-duplicate behavior, **not eventual exactly-once
execution across arbitrary crashes**.

## Independent verification checkpoint

The parent independently ran the focused and neighboring files below with
Python 3.11.16 through `./scripts/test.sh`: **101 passed**. The tested source
hashes were unchanged before and after execution. Replaying the consumer
presence regression on the original baseline failed at the missing-consumer
assertion; that is feature-absence evidence, not a crash-recovery guarantee.

```sh
./scripts/test.sh tests/test_post_restart_continuation.py \
  tests/test_start_session_turn_runtime_adapter.py \
  tests/test_restart_drain_admission.py \
  tests/test_chat_start_claim_cli_session.py \
  tests/test_wakeup_defer_race.py -q --timeout=30
```

This parent gate used disposable HOME/state directories, an offline guard and
an explicit override of the unrelated autouse HTTP-server fixture. It permitted
only the two inspected fixture Python subprocesses and read-only Git version
queries; it blocked native APIs, unexpected process launches, Agent imports and
outbound sockets in the test process. The actual imported routes path was
verified. The startup helper was exercised as described below, **not full
`server.main()` or a live restart**. Initial parent-harness failures (the Git
metadata allowance and missing disposable session-index directory) were fixed
in the harness, without modifying the implementation to obtain the pass.

Whole-file Ruff reports 24 findings in existing `routes.py`/`server.py`, identical
to the baseline; there are no new findings in the delivered Python files.
Whitespace checks passed. Independent source review and the controller-side
publisher/deployment protocol remain separate gates. This checkpoint does not
establish production cutover readiness or same-session live resumption.

## Verification and contract routing

Runtime/run-state, canonical session ownership and turn-journal contracts apply.
This is an intentional narrowly authorized server-originated continuation, not
a change to the browser's stale-compression no-auto-replay contract.

`tests/test_post_restart_continuation.py` exercises disposable SQLite/sidecars,
real route/adaptor/stream-start helpers with a fake Agent worker, the exact
source-extracted server startup helper (without importing a production server),
both ordinary HTTP/server admission orderings, competing consumers, independent
process file claims, fresh-process receipt reads, unsafe inputs, binding changes,
receipt failures and lost/ambiguous postclaim responses. Source-extracted startup
coverage is not proof of a live supervisor rollout or full HTTP server startup.
Neighboring adapter and restart-drain tests cover compatibility. No test invokes
a live model, service restart, controller operation, signing tool or production
state. Full deployment/controller acceptance must be performed separately.
