# RFC + Implementation Plan: Low Data Mode

Status: approved for implementation (phased). Branch: `feature/low-data-mode`.
Worktree: `~/hermes-webui-lowdata` (base: `origin/master` 3ffbf0b685).

> **For the implementing agent:** read this whole file first. Implement ONE
> phase per commit, in order. Do not start a phase until the previous phase's
> acceptance checks pass. Run tests only through `./scripts/test.sh`. Never run
> against real `~/.hermes` state; never restart any running WebUI. Line numbers
> below are approximate (audited on 3ffbf0b685's sibling branch) — locate code
> by symbol name. If a step here conflicts with what you find in source, STOP
> and record the discrepancy in `docs/rfcs/low-data-mode-notes.md` instead of
> improvising.

---

## 1. Problem

On plane Wi-Fi, weak cellular, or high-latency tunnels the WebUI's live
transport degrades badly:

- `EventSource` on `GET /api/chat/stream` is a long-lived connection that
  captive portals/middleboxes stall silently.
- Each `token` event (`api/streaming.py` `put('token', {'text': …})`) is its
  own SSE frame — high framing overhead, many small writes.
- Every drop triggers reconnect + `/api/chat/stream/status` + replay
  negotiation in `static/messages.js` (`attachLiveStream`, `_wireSSE`,
  `_reattachOrRestoreAfterDeferredStreamError`) — chatty on a lossy link.
- `POST /api/chat/start` has **no client idempotency key**. A request that
  reaches the server but whose response is lost cannot be safely retried →
  duplicate turns or "did it send?" uncertainty.

The bad hop is **browser ↔ WebUI server**. WebUI ↔ agent/gateway is local.
Low Data Mode is therefore a **client-delivery mode**, not a proxy.

## 2. Goals / non-goals

Goals
1. Poll-based, batched, resumable event delivery that tolerates arbitrary
   connection drops with zero lost/duplicated events.
2. Exactly-once message submission across retries.
3. Fewer bytes on the wire: coalesced tokens, gzip, deferred heavy payloads.
4. User-visible toggle (Live / Low Data / Final-only) with honest latency UI.

Non-goals (hard)
- **No change** to run execution, Stop/Steer semantics, compression, the
  STREAMS/ACTIVE_RUNS lock ordering, acceptance fences, or terminal settlement
  (see AGENTS.md). The poll path is a **read-only consumer** of the run journal.
- No new source of truth for run state.
- No offline agent execution; no new dependencies, build tools, or processes.
- Live (SSE) mode remains the default and behaviorally unchanged.

## 3. Verified facts from source (audit, 2026-10-04)

| Fact | Location |
|---|---|
| Every in-process `put(event, data)` journals first, then publishes to the queue under the per-run lock | `api/streaming.py` `put()`; `RunJournalWriter.append_and_publish_sse_event` |
| Terminal events journal via `close_acceptance_fence_and_publish_terminal` | same; `TERMINAL_SSE_EVENTS` |
| Only `metering` is skipped from the journal at write time | `api/run_journal.py` `REPLAY_SKIPPED_SSE_EVENTS = {"metering"}` |
| Steer delivery is journaled (`steer_delivered`) | `api/streaming.py` steer publish helper |
| Cancel is journaled; has a queue-only fallback if journaling raises | `api/streaming.py` cancel path |
| Gateway-backed chat also uses `RunJournalWriter` | `api/gateway_chat.py` |
| **Journal-miss fallback**: if `run_journal is None` or the append raises, event goes to the queue only (no event_id) | tail of `put()` |
| **Runner backend** (`runtime_adapter_runner_enabled()`) streams from `adapter.observe_run(run_id, cursor=…)` with opaque cursors, not the WebUI journal | `api/routes.py` runner-observe SSE path |
| Bounded cursor reads exist | `read_run_events(session_id, run_id, after_seq=)`, `read_session_run_events(session_id, after_event_id=, max_bytes=4MiB, max_rows=4096)` |
| Cursor parse | `_parse_run_journal_event_id`; `_chat_stream_resume_cursor` in routes |
| Event id form `run_id:seq`, opaque to clients | `docs/rfcs/session-sse-contract-v1.md` |
| Cheap change detection | `session_journal_fingerprint(session_id)` |
| Turn journal is per-session-per-pid JSONL `…/{sid}~{pid}.jsonl`; `submitted` event appended in chat start (two call sites) | `api/turn_journal.py`; `api/routes.py` `append_turn_journal_event(... "event": "submitted" ...)` |
| JSON responses >1 KiB gzipped when the client accepts | `api/helpers.py` `j()` / `_accepts_gzip` |
| Service worker pre-caches shell; `/api/*` network-only | `static/sw.js` |
| Client send path | `static/messages.js` `send()`, `api('/api/chat/start', …)` |

### Gaps (must be handled)
- **G1 Journal-miss fallback**: rare, but polling would miss those events.
  Mitigation: poll responses carry `active` + `terminal` from the
  authoritative stream status / run summary. When the run is no longer active
  and the journal has no terminal row, the client runs the existing snapshot
  reconciliation (the same fallback the SSE contract specifies). Never infer
  completion from silence alone.
- **G2 Runner backend**: out of scope for v1. When
  `runtime_adapter_runner_enabled()` is true, `/api/chat/poll` returns
  `409 {"code":"low_data_unsupported_backend"}`; the client falls back to Live
  for that stream with a toast. Fail closed.
- **G3 `metering`** is live-only; acceptable to drop in Low Data Mode (counters
  refresh at turn end via snapshot).
- **G4 Turn journal is per-pid**: dedupe must see all pid files for the
  session. Verify `read_turn_journal(session_id)` globs all pids; if not, add
  a new helper — don't change existing behavior.

## 4. Architecture

```
 browser (Low Data)                     WebUI server (runtime unchanged)
 ─────────────────                      ─────────────────────────────────
 outbox(IndexedDB) ──POST /api/chat/start {client_msg_id}──► dedupe → existing start
 poll loop ─────────GET  /api/chat/poll?session_id&after──► run-journal reader
                                                            (read-only, no registry locks)
 tap-to-expand ─────GET  /api/chat/event?…&event_id──────► one full journal row
 Stop / Steer ──────existing POST endpoints (unchanged)
```

## 5. Phases

One commit per phase; message prefix `feat(low-data):` / `test(low-data):`.

### Phase 0 — Contract routing (docs only)
- Add one bullet for this RFC in `docs/CONTRACTS.md` (match neighbors' style):
  read-only consumer of the run-journal contract; extends `/api/chat/start`
  with an optional field.
- Create `api/low_data.py` with a module docstring only.

Acceptance: diff touches only docs + the empty module.

### Phase 1 — Idempotent submit (`client_msg_id`) — useful in ALL modes

Server (`api/routes.py` `_handle_chat_start` + `api/turn_journal.py`):
1. Accept optional `body["client_msg_id"]`: string matching
   `^[A-Za-z0-9_-]{8,64}$`. Invalid → `400 {"code":"invalid_client_msg_id"}`.
   Absent → today's behavior, unchanged.
2. **Before any session materialize / workspace / model / pending mutation**
   (immediately after the stale-runtime barrier `_agent_runtime_barrier_response`,
   before `_get_or_materialize_session`) look up a prior `submitted` turn-journal
   event for `(session_id, client_msg_id)`.
   - Hit → `200` with the **same response shape** chat start returns today for
     an accepted turn, built from the recorded `stream_id`, plus
     `"deduplicated": true`. Start nothing.
   - Miss → proceed; add `client_msg_id` to the `submitted` payload at **both**
     submitted call sites.
3. Race: two concurrent identical POSTs. Guard with a process-local
   `threading.Lock` keyed by `(profile, session_id, client_msg_id)`, held from
   lookup through the `submitted` append (small dict of locks with cleanup,
   like `_lock_for` in `run_journal.py`). Do NOT take STREAMS_LOCK /
   ACTIVE_RUNS_LOCK. Never write the HTTP response while holding it.
   If the start path's lookup-to-append span crosses code you cannot hold a
   lock over safely, STOP and write it up in the notes file.
4. Scope: only the given session's journal under the active profile. Never
   cross profiles.
5. Helper: `find_submitted_turn(session_id, client_msg_id, *, session_dir=None)
   -> dict | None` in `api/turn_journal.py`.

Client (`static/messages.js` `send()`):
- Mint `client_msg_id` = `crypto.randomUUID()` (fallback: 16 random bytes hex)
  once per user send; include it in the start POST body. On network error
  (no HTTP status), retry up to 3× with backoff 1s/3s/8s reusing the SAME id.
  Never retry on 4xx/5xx with a status. Treat `deduplicated:true` exactly like
  success.
- No other send-path behavior changes.

Tests (`tests/test_low_data_idempotent_start.py`):
- same id twice → one run; second returns `deduplicated:true`, same stream_id.
- different ids → two turns.
- invalid id → 400 and no session mutation (session file unchanged).
- concurrent duplicates (threads + barrier) → exactly one `submitted` row.
- absent id → existing behavior (run `test_chat_start_*`, `test_turn_journal*`).
- static test: `messages.js` start body includes `client_msg_id`.

### Phase 2 — Poll endpoint `GET /api/chat/poll`

Logic in `api/low_data.py`; route wired in the `api/routes.py` GET dispatcher
next to `/api/chat/stream/status`. Same auth / profile handling as
`/api/chat/stream`.

Query params
- `session_id` (required, validated with the existing id validator)
- `after_event_id` (optional, opaque; empty = from the start of the session's
  current/latest run)
- `wait` (int seconds, 0–20, default 0) — long-poll budget
- `detail` = `full` | `lite` (default `lite`)
- `max_bytes` (int, clamped 16 KiB–512 KiB, default 128 KiB)

Algorithm
1. Runner backend enabled → 409 `low_data_unsupported_backend` (G2).
2. Read rows via `read_session_run_events(session_id, after_event_id=…)`,
   filter with `journal_replay_visible`.
3. No rows and `wait>0`: loop sleeping 0.5s, checking
   `session_journal_fingerprint`, until it changes or the deadline passes.
   **Hold no locks while sleeping.** Cap concurrent long-polls per process
   (e.g. 32 via `threading.BoundedSemaphore`, non-blocking acquire); over cap →
   behave as `wait=0`. Release in `finally`.
4. **Coalesce** (pure function `coalesce_events(rows) -> list[dict]`):
   - adjacent `token` rows of the same run → one row, `text` concatenated,
     `event_id` = last merged row's id, `coalesced_from` = count.
   - same for `reasoning`.
   - never merge across a different event type, a run_id change, or a seq gap.
5. **Lite detail**: for `tool`, `tool_complete`, and any payload string field
   >2 KiB (args/result/output/diff/image data), replace the field with
   `{"_deferred": true, "kind": …, "bytes": n, "preview": first 200 chars}`.
   Keep name/status/ids/duration so cards render.
6. Truncate to `max_bytes` serialized; set `truncated: true`, stop at a row
   boundary (never split a row; always include ≥1 row).
7. Response via `j()` (gzip applies):
   ```json
   {"events":[…], "next_event_id":"<last underlying id, or the input cursor>",
    "active":bool, "terminal":"done|cancel|apperror|stream_end|null",
    "stream_id":"…|null", "truncated":bool, "server_time":float}
   ```
   `active` / `stream_id` from the same source `/api/chat/stream/status` uses.
   `terminal` via `select_authoritative_terminal_event` / `latest_run_summary`.
8. Malformed or foreign cursor → fail closed: `409 {"code":"cursor_invalid"}`;
   the client does a full snapshot reload and restarts without a cursor.

`GET /api/chat/event?session_id&event_id` returns one full, un-deferred
journal row (for tap-to-expand). The event must belong to the session; 404
otherwise.

Tests (`tests/test_low_data_poll.py`, disposable `session_dir` fixtures like
`tests/test_run_journal_routes.py`):
- **Parity property test**: synthetic journal (tokens, reasoning, tool,
  tool_complete, steer_delivered, done). For 200 random cursor-split
  sequences, concatenated poll output yields the same ordered event types and
  identical concatenated text as a full replay.
- coalescing boundaries (type change, run change, seq gap).
- lite deferral shape; `/api/chat/event` returns the full payload; wrong
  session → 404.
- `max_bytes` truncation at a row boundary, always ≥1 row.
- long-poll returns early on a new append (a thread appends after 0.3s).
- over the long-poll cap → returns immediately.
- runner backend → 409; malformed cursor → 409.
- `metering` rows never appear.
- static test: `api/low_data.py` doesn't reference `STREAMS_LOCK` /
  `ACTIVE_RUNS_LOCK` / `RunJournalWriter`.

### Phase 3 — Client transport switch

New file `static/low_data.js` (loaded in `index.html` after `messages.js`;
added to the `sw.js` shell list using the existing `?v=` convention).

- Setting `hermes-transport-mode` in localStorage: `live` (default) |
  `lowdata` | `final`. Add a select in the Settings panel (`static/panels.js`,
  near the font-size control) with i18n keys in `static/i18n.js` (English at
  minimum; follow existing key patterns).
- **Behavior-neutral refactor first (own commit):** extract the per-event
  dispatch inside `_wireSSE` into a named function
  `dispatchStreamEvent(name, data, eventId)` used by `_wireSSE`. All existing
  frontend tests must pass unchanged.
- In `attachLiveStream`: if mode ≠ `live`, delegate to
  `attachLowDataStream(activeSid, streamId, uploaded, options)` and return.
  This is the ONLY edit to `attachLiveStream`. The poller calls
  `dispatchStreamEvent` — **do not duplicate rendering logic**.
- Poller: `GET /api/chat/poll` with `wait=15` (`lowdata`) / `wait=20`
  (`final`). On network error back off 2s→4s→8s→15s cap with jitter. Pause while
  `document.visibilityState==='hidden'`; resume on visible/online. Dedupe by
  `event_id` (bounded Set, last 2000).
- `final` mode: buffer coalesced text in memory; render only a
  "Working… (Final-only mode)" placeholder, then terminal events + the final
  text.
- On terminal, or `active:false` without terminal (G1) → call the existing
  post-stream snapshot reconciliation used after SSE `done`.
- Status chip via the existing composer status area: `Low Data · synced 4s
  ago` / `Low Data · offline, retrying in 8s`.
- 409 `low_data_unsupported_backend` → toast + Live for this stream only.
- Stop/Steer unchanged; tooltip "applies on next sync" in Low Data/Final.
- Clear poller timers on session switch, terminal, and mode change.

Tests:
- static tests (pattern: `tests/test_run_journal_frontend_static.py`):
  delegation on mode; `dispatchStreamEvent` used by both paths; `low_data.js`
  in `index.html` and the `sw.js` shell list; i18n keys exist.
- Browser e2e only if the repo's harness supports it (check `TESTING.md`);
  otherwise left for the reviewer's manual validation.

### Phase 4 — Outbox (offline-safe composer)

In `static/low_data.js`:
- IndexedDB store `hermes-outbox` {client_msg_id, session_id, body,
  created_at, attempts}. Write before POST; delete on 2xx (incl.
  deduplicated). On 4xx, delete and show the error. On network error, keep it
  and retry on the `online` event and every 30s while visible.
- Queued items render as the user bubble with a "queued" badge.
- Only in `lowdata`/`final` modes; Live uses Phase 1 retry only.
- Stale-session / compression-continuation 409 → keep the draft and show the
  existing continuation UI; **never auto-replay** (AGENTS.md inactive-session
  rule).

Tests: static tests for store name, delete-on-ack, no auto-replay on 409.

### Phase 5 — Tap-to-expand deferred payloads
- Cards whose payload has `_deferred` show size + preview and a
  "Load (12 KB)" button → `GET /api/chat/event`, then render via the existing
  card renderer.
- After terminal, the normal snapshot reload provides full content anyway.

### Phase 6 — Docs, measurement
- `README.md` usage blurb; `TESTING.md` throttling recipe;
  `docs/UIUX-GUIDE.md` note for the status chip.
- `scripts/low_data_bench.py`: run against an isolated server, replay a
  recorded journal through SSE vs poll; report bytes and request counts.

## 6. State ownership

| Value | Authoritative owner | Low Data role |
|---|---|---|
| Run events, seq, cursor | run journal | read-only consumer |
| Active run / Stop / Steer / fences | runtime registries | unchanged |
| Turn submission identity | turn journal `submitted` row | + `client_msg_id` |
| Outbox | browser IndexedDB | new, client-only, cleared on ack |
| Transport mode | localStorage | new |

Cleanup on every exit: long-poll semaphore released in `finally`; dedupe-lock
entries evicted after release; poller timers cleared on session switch,
terminal, and mode change.

## 7. Guardrails for the implementer
- Do not touch `put()`, `RunJournalWriter`, Stop/Steer, compression, or lock
  code. If you think you need to, stop and write it up in the notes file.
- No new Python/JS dependencies. Vanilla JS only.
- Isolated state for any manual run, from the worktree:
  `HERMES_HOME=/tmp/hermes-lowdata-home HERMES_WEBUI_STATE_DIR=/tmp/hermes-lowdata-state HERMES_WEBUI_PORT=8791 python3 bootstrap.py`
  Never use port 8787 or real state. Kill only processes you started.
- After each phase run `./scripts/test.sh tests/test_low_data_*.py` plus
  neighbors: `tests/test_run_journal*.py`, `tests/test_turn_journal*.py`,
  `tests/test_chat_start_*.py`, `tests/test_steer_delivery_journal.py`, and
  any frontend static tests touching `messages.js`.
- Commit per phase; **do not push**. Do not edit `CHANGELOG.md`.

## 8. Validation checklist (reviewer)
1. Diff audit: no edits to forbidden areas (§7); Live path unchanged except
   the delegation line + behavior-neutral dispatch extraction.
2. Tests green; new tests fail when the implementation is reverted (spot-check).
3. Isolated server + Chrome throttling (300 ms RTT, 150 kbps, offline flaps):
   Live vs Low Data transcripts identical; zero duplicate turns under forced
   retries; Stop takes effect within one poll cycle.
4. Bench: bytes and request counts.
5. Desktop + narrow + mobile screenshots of the setting and status chip.

## 9. Effort estimate
P0 0.1d · P1 0.75d · P2 1.25d · P3 1.5d · P4 0.75d · P5 0.5d · P6 0.5d
≈ 5–5.5 focused days total; usable core (P1–P3) ≈ 3.5d.
