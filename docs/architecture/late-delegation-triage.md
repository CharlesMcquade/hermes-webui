# WebUI late-child triage routing

Late delegation assessment is opt-in. Agent's `tools.async_delegation` ledger is
the authority for enrollment, exact parent ownership, atomic admission, lease,
and durable disposition. Ordinary, unfinalized background work is unchanged.
The WebUI background completion drain checks the exact parent/delegation ID
before taking an ordinary claim. An enrolled result is admitted atomically with
`admit_late_result`; a competing or unexpired claim remains retryable. The
next-turn process drain does not age-drop, append, or ACK pending/claimed
opted-in completions; it leaves them to the background assessor.

For a claimed result, the local WebUI reviewer consumes the parent's session
history and durable child report via
`agent.late_delegation_review.review_late_delegation_result(parent_agent, parent_goal, child_result, history, timeout=20)`.
The latest genuine user request supplies `parent_goal`; the complete durable
result is serialized as `child_result` (never silently truncated). A detached
cache-parity fork reads the completed parent snapshot without tools or session
writes and returns only `no_change | needs_parent | uncertain`. No local-cache
fallback is allowed for a Gateway-owned parent.
Only the exact session's cached agent, stamped with its resolved profile home,
is eligible. Gateway-owned turns and missing agent/context/reviewer default to
`wake`. The reviewer runs outside the session mutation lock with a bounded
20-second timeout. WebUI snapshots the parent session under the per-session lock,
then rechecks its conversation/turn revision and active-run state under that
same lock before settling `suppress`. Any other verdict, error, timeout, or
revision change settles `wake` (releasing the claim for ordinary delivery).
If settlement fails, no ordinary ACK is issued: the ledger lease and restore
sweep own retry. Suppression retains Agent's durable result but creates no
wakeup turn, visible SSE event, or `[SILENT]` transcript row. The exact parent
can later read that result via `delegate_task(action="inspect", delegation_ids=[...])`.
Background claim/settlement and the private review worker use the exact session's
resolved profile home, not an ambient WebUI startup/request home.

The Gateway transcript merge must retain `delegation_wakeup` provenance on the
stream-owned user row even when `state.db` has already supplied an untagged copy
of that same turn. Match the active stream token and an explicitly tagged result
row before stamping the retained display row; matching message text alone is not
proof of origin. The WebUI display filter hides tagged rows only, leaving model
context intact. Historical untagged rows are not reclassified on load.

For isolated integration against an Agent feature checkout, set
`HERMES_AGENT_TRIAGE_SOURCE` to that checkout when running
`./scripts/test.sh tests/test_late_delegation_crossrepo.py tests/test_late_delegation_real_fork.py`;
the tests load it in child interpreters with disposable `HERMES_HOME`. The
real-fork test uses a loopback-only stub provider and requires `requests` and
`openai` in the isolated WebUI worktree venv (`.venv/bin/python -m pip install
requests openai`). Do not aim the runner at a live home or WebUI state directory.
