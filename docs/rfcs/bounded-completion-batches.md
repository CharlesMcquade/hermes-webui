# Bounded completion batches

## Scope

Repair redundant idle process-completion turns without changing active AIAgent execution or persistence hooks. Existing browser completion observations remain enabled. This does not implement live tool-result injection.

## Contract

- WebUI queue ownership is not core result consumption. Claim the WebUI route before acknowledging the core queue to prevent competing drains stealing deferred results.
- The existing core consumed bit (set by native wait/read_log) suppresses an unnecessary wakeup. Read-only poll does not. Arbitrary result-file reads cannot be inferred as process consumption.
- On idle teardown, atomically claim pending entries and start one continuation with all outstanding results. Preserve every process ID on admission conflict or transient failure. Successful admission acknowledges each ID.
- Keep the session pending marker while dispatch is in flight. Release queue ownership when the result is consumed, successfully admitted, or deliberately suppressed by existing deleted/paused-session policy.
- No new daemon, scheduler, polling interval, private agent hooks, config knobs, frontend cache strategy, provider change, or capacity reduction.

## Limits

This batches entries accumulated while a turn is active; it does not debounce future, separately arriving idle completions. Pending queues remain process-local, as on the baseline. A process crash after queue claim is not made durable by this patch. Existing turn admission is the acknowledgment boundary, not proof of completed inference. A transient admission failure is requeued for the existing next-turn/idle drain; this change adds no autonomous retry timer.

## Tests

`test_background_completion_delivery.py`, `test_completion_batch_lifecycle.py`, and `test_completion_core_consumption.py` cover burst dispatch, wait vs poll, ownership races, status failures, retry identity, in-flight state, and repeated-batch cleanup. Existing wakeup, pause, ring-buffer, coalescing, and reconnect gates remain applicable.

## Paired config-save change

The selected release also calls core's existing name-aware reference traversal with a new optional string policy. WebUI saves protect source-owned credential references across environment rotation, support a complete replacement reference, and fail closed on ambiguous named lists or an incompatible core. CLI behavior is unchanged when no policy is passed. Roll out the pinned core/WebUI pair together. Do not combine this release with the rejected prose redactor or asset redesign.
