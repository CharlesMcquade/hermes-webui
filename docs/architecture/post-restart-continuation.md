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
5. `server.py` retains an owned daemon consumer without blocking the HTTP accept loop.
   It waits for missing proof/request and retries live busy admission. A refused
   request stops this startup consumer; it does not repair/rewrite the request.
   In-process `consume_post_restart_continuation(deployment)` is the same private
   entrypoint, useful to a trusted controller integration, not an HTTP route.

## Admission, receipts and ambiguity

With this feature enabled, ordinary `_handle_chat_start`, `_handle_goal_command`,
`start_session_turn`, `_start_run` and legacy stream starts participate in one per-session reentrant
outer admission edge. It precedes workspace/model/pending mutation. The consumer
holds the same edge through binding revalidation, durable claim and launch.
Ordinary HTTP access/profile checks are not bypassed. An already-busy session is
rejected before ordinary starts can modify its workspace/model. Without the
continuation environment variable, ordinary calls use the unchanged path.

New-goal kickoff text acquires that edge before profile retagging, model
resolution, explicit-pick signature stamping, or goal updates. Active-run
status/pause/resume/clear/stop/done controls retain their normal semantics; they
are serialized but not rejected as new kickoffs.

Lock order: private store flock (consumer only), outer session admission,
consumer-owner claim edge (consumer only), existing session/stream registry locks. Never acquire the outer edge from inside
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
retires its stream/pending state. Detachment and worker-participant retirement
share `STREAMS_LOCK`, using the existing cancellation settlement helper. If
Stop has registered cancel and worker participants, each retires only its own
participation; neither retirement order leaves a phantom worker or fence.
`<id>.result.json` records the owning route's
result with the same ID. A returned route result is not worker-entry proof;
`entered.json` is not proof of model completion or an exactly-once tool effect.

Keep claims indefinitely for the retry horizon. Never delete a claim or assign
a fresh ID automatically to overcome an ambiguous result. The supervisor/user
must inspect the session and decide on a new action. This provides a reliable
normal launch and conservative no-duplicate behavior, **not eventual exactly-once
execution across arbitrary crashes**.

## Graceful owner shutdown

The startup consumer is created inside the HTTP server's teardown scope, after
construction and signal-handler installation. `server.main()` retains both a
`ConsumerOwner` and the consumer thread. Shutdown closes the owner claim edge,
wakes a waiting consumer, and joins it before HTTP server teardown. A signal
callback only dispatches the normal shutdown helper; it does not acquire the
owner edge or join the consumer on an interrupted main-thread stack.

Validation is not admission. Immediately before the irrevocable claim write,
the consumer takes the owner edge and holds it through claim, route launch and
result recording. If owner closure wins, even a previously validated request
returns `stopped` without a claim or session mutation. If admission wins,
shutdown waits for that transaction and joins the consumer. That accepted run
then has the **existing worker shutdown semantics**: joining the consumer is
not waiting for model completion, provider/tool execution, or the worker-entry
receipt thread. An accepted/ambiguous claim is never replayed. A hung disk or
nonparticipating private-store lock holder can delay graceful joining; forced
termination still has the documented ambiguity semantics.

The optional owner argument on the private synchronous entrypoint supports
trusted embedding. Such callers must share their lifecycle owner and close/join
it in their own teardown; a standalone call without an owner is not a managed
startup consumer. This is not a new public endpoint or controller protocol.

## Reviewed-blocker repair evidence

The repair adds deterministic receipt/Stop barriers through **real
`cancel_stream`**, tests both cancel/worker retirement orderings, and tests goal
and continuation winners through the real route admission and stream launcher
with fixture workers. Losing starts leave model/profile/signature/goal/pending
state untouched. Goal controls are checked with the feature both on and off.

Actual imported `server.main()` is exercised with all startup dependencies
stubbed and a fake HTTP server (no socket). Tests cover waiting proof followed
by shutdown and later proof, validated-before-claim losing to shutdown, and
claim-admission winning before shutdown. These prove the Python lifecycle
wiring, not full dependency startup, native signal delivery, live HTTP health,
a supervisor rollout, or live resumption.

Before repair, the two Stop cases failed with a remaining `{'worker'}`
participant, goal cases failed at mutation-before-admission / missing shared
admission, and actual-main shutdown failed because teardown saw a live
consumer. The guarded offline verification commands are the five-file gate
below plus `tests/test_goal_silent_ingress_suppression.py`, and a separate
`tests/test_goal_command_webui.py -k 'not profile_goal'` gate. The latter excludes
five native-Agent integration cases deliberately, not as claimed passes.

Caller audit: chat HTTP (including regeneration), server wakeups, and adapter
legacy delegates reach the serialized same-session entries. `/goal` was the
same-session kickoff caller missing the earlier guard. `/btw` and `/background`
launch distinct new hidden session IDs rather than restarting the bound parent;
they are not same-session admission participants. `/btw`'s pre-existing stale
parent-stream cleanup and concurrent session administration are not expanded
into a general session-write fencing protocol here. External Agent/CLI writers,
Gateway/runner launches and live worker goal continuation remain outside this
settled-session/local-legacy contract.

## Independent repair verification checkpoint

The parent independently reran the expanded gate on Python **3.11.16** through
`./scripts/test.sh`: **148 passed, 5 deliberately deselected** native-Agent goal
cases. A separate neighboring gate covering
`tests/test_issue6869_gateway_launch_failure.py` and
`tests/test_stale_reaper_settlement.py` passed **9 tests**, including successor
ownership and deleted-session preservation. Source hashes matched the delivered
repair and frozen review snapshot; `api/streaming.py` is unchanged.

A parent regression replay compiled the exact pre-repair consumer, routes and
server blobs from `f7fd6ed7dc7f0faf45fa1c7e66363009a19e609d` at their original
module paths using a test-only import loader. The new tests then reproduced
**five expected failures**: both Stop retirement orders retained `worker`, both
goal admission tests failed, and actual-main teardown observed a live consumer.
This source-code replay does not verify a baseline deployment or release identity.
No product files were replaced for the replay.

The parent guard blocked native loading, Agent imports, unexpected subprocesses,
outbound connections and listening sockets. The unrelated autouse HTTP server
was suppressed; the import-time conftest ephemeral loopback port reservation was
allowed and immediately closed. An initial runner-only failure overblocked that
reservation; only the parent guard changed before rerunning. Actual-main tests
still use fake HTTP and stub startup dependencies. Whole-file Ruff retained
**24 baseline / 24 final findings, with no additions**. The final combined gate
on committed repair `fb41e6625dddf9d023724f21dccd25d3720032b5` passed **157 tests,
5 deliberately deselected** on Python 3.11.16. Controller publishing, full startup
and live same-session restart remain open.

## Focused repair review closure

Both source-only reviewers in `deleg_419f8eb7` found no concrete supported-scope
blocker. The parent independently inspected the cited cleanup, cancellation
registration/retirement, earliest goal admission, owner claim/close and actual-main
teardown paths. The executable and test bytes match the frozen reviewed snapshot,
prior parent test receipt and committed repair `fb41e6625dddf9d023724f21dccd25d3720032b5`.
The frozen documentation predates only the parent verification section above; its
supported contract is unchanged. Together with the retained five old failures and
157-test repaired gate, this closes the three original findings **offline**, within
the single-owner, settled-session, explicit-local legacy scope.

Cleanup-before-Stop-registration is supported by the shared-lock source
interleaving, not a dedicated new barrier test. Neighboring successor tests cover
canonical/stale-object preservation, not a fully concurrent successor launch.
Actual-main tests invoke captured signal callbacks with fake HTTP and stubbed
startup; they do not prove native signal delivery or pre-`serve_forever()` behavior
of the real HTTP server. If startup raises before serving after a shutdown helper
has started, that daemon helper can remain blocked in `httpd.shutdown()`. The main
finalizer still closes/joins the consumer before teardown, so this observation
does not reopen the consumer-admission race; helper termination remains a separate
full-startup verification limit. Cooperative Store/session completion is required.

A previously claimed worker may enter after consumer join and HTTP teardown. Join
settles claim/launch, not worker entry or model completion. Missing entry evidence
never licenses replay. This documentation-only closeout adds no test execution or
live restart/deployment evidence and does not satisfy cutover readiness.

## Earlier independent verification checkpoint

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
source-extracted startup helper plus actual imported `server.main()` with
stubbed dependencies and fake HTTP ownership, both ordinary HTTP/server
admission orderings, competing consumers, independent
process file claims, fresh-process receipt reads, unsafe inputs, binding changes,
receipt failures and lost/ambiguous postclaim responses. Source-extracted startup
coverage is not proof of a live supervisor rollout or full HTTP server startup.
Neighboring adapter and restart-drain tests cover compatibility. No test invokes
a live model, service restart, controller operation, signing tool or production
state. Full deployment/controller acceptance must be performed separately.
