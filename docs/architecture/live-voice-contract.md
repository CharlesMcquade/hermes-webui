# Live voice: turn and run ownership

Live voice uses OpenAI Realtime over WebRTC as a conversational front end to
Hermes, not as a second executor. The implementation lives in
`api/voice_live.py` and `static/voice_live.js`.

## Audio and explicit reply holds

- The server requests `gpt-realtime-2.1`, `gpt-live-transcribe`, far-field noise
  reduction, and low-eagerness semantic VAD. Both automatic response creation
  and automatic interruption are disabled. The browser requests echo
  cancellation, noise suppression, automatic gain control, and mono capture as
  **ideal**, not mandatory, constraints (support differs by browser/device).
- The browser orders completed transcriptions by committed input item before
  considering a response. Incidental speech-start events never cancel playback.
- Ordinary completed speech queues until generation **and** WebRTC playback have
  finished. This deliberately trades instant barge-in for speakerphone stability.
- Playback is reserved by response ID as soon as audio content is announced,
  including when `response.done` precedes `output_audio_buffer.started`. Only a
  matching `output_audio_buffer.stopped`/`cleared` releases actual playback.
  An unstarted reservation can also be released when the terminal response
  snapshot contains no audio (an announced part was abandoned, not played).
  Late/duplicate/foreign events cannot release or resurrect another response.
  These events describe the server's audio buffer; they do not measure physical
  device output latency.
- Background results and progress stay in a connection-owned context queue until
  the same safe boundary used for replies. No progress/result context is injected
  during generation, playback, user speech, or pending committed transcription.
  Only the latest pending progress per run is retained; a final result replaces
  that run's pending progress. Progress piggybacks on the next reply rather than
  causing an unsolicited spoken turn at every poll. A result requests one reply,
  coalesced with other waiting input, immediately after the boundary opens.
  Function-call outputs remain protocol settlement messages, not unsolicited
  background context; their spoken continuation uses this same response gate.
- A recognized explicit hold such as `don't reply until I say "Spider-Man"`
  suppresses all new replies, progress narration, and new tool dispatch. It also
  cancels active generation and clears active playback. The declaration itself
  does not release the hold. A later matching transcript releases it; case,
  punctuation, hyphens, and the `Spider-Man`/`Spiderman` spelling are normalized.
  Related supported forms include `stay quiet until I say ...` and
  `wait to respond until I say ...`.
- The Realtime prompt also tells the model to choose `wait_for_user` silently
  for non-addressed conversation. That tool is settled without another
  response-create loop. It does not put ambient chatter into the chat transcript.
- The gate exists for the current connection only. A reconnect resets it.
  Recognition depends on successful transcription; there is no enrolled-speaker
  identity check and no guarantee for arbitrary paraphrases. Another speaker
  saying the release phrase can release the gate. This is not speaker isolation.

## Responses and tool calls

- There is one client response scheduler, fenced by active response ID,
  generation, playback state, and outstanding tool settlement. Duplicate or late
  response completions must not release a newer response's lock.
- Complete function calls are recognized from arguments-done, output-item-done,
  and response-done events. Each `call_id` executes and emits its matching
  `function_call_output` at most once per connection, even when events overlap.
  Invalid arguments produce a tool error rather than hanging settlement.
- `agent_ask` settles as soon as the normal chat-start path accepts the run. Its
  result is a run handle, **not** a success claim about the requested task. An
  asynchronous watcher supplies the eventual answer without blocking other
  voice controls. Other calls are `agent_status`, `agent_steer`, `agent_stop`, and
  the local `wait_for_user`.
- Tool HTTP requests are bounded and are never automatically retried (their
  side effects may already have happened). A failed tool-result send ends the
  voice connection visibly. An unconfirmed ask directs the model to check
  status/chat before repeating it. A rejected response-create may retry once;
  persistent rejection or failed generation ends voice with a visible error.
- Small spoken exchanges are mirrored as paired user/assistant turns.
  `agent_ask` persists through chat-start instead of adding a duplicate user row.

## Hermes owns the work

- Launch uses `_handle_chat_start`; all configured Hermes tools, skills, model,
  and memory remain available through that executor. One active run per session
  is the existing chat contract. This is not an implementation of ChatGPT or
  Codex's private app internals, nor a separate exposed copy of every tool.
- Steering uses `_handle_chat_steer`. Acceptance confirms **delivery**, not
  application. Stop delegates to the authoritative `/api/chat/cancel` path,
  including its persistence-failure result rather than inventing success.
- `POST /api/voice/live/status` with `{session_id, stream_id}` reads the exact
  run's journal after ownership validation. Its terminal state comes from
  `select_authoritative_terminal_event`. A verified final answer comes only
  from the selected `done` event's session snapshot, never the mutable session
  tail. Missing, foreign, malformed, cancelled, failed, or result-less runs must
  not masquerade as completed answers. Omitting `stream_id` retains the existing
  active-run status behavior.
- Progress uses that same journal's tool/interim events. No separate runtime
  journal is invented in the session GET payload.
- Disconnect fences all outstanding asynchronous work by generation/session,
  including already-queued data-channel callbacks, clears pending context and
  input/playback reservations, closes audio resources, and stops watching. It
  does **not** cancel a Hermes run; accepted work remains in chat. Confirmed run
  cancellation discards its pending progress and fences in-flight poll results.
  Setup is serialized so an old microphone or bind promise cannot take ownership
  of a new call.
- Existing privilege behavior is unchanged: connect temporarily enables session
  YOLO, disconnect restores the remembered prior value, and pagehide attempts a
  restore beacon. This is not a new approval policy or a durable lease across
  a server crash.

## Verification

```sh
./scripts/test.sh tests/test_live_voice_realtime.py tests/test_live_voice_result.py -q
node --check static/voice_live.js
```

The Node/VM cases drive the shipped JavaScript through fake browser/provider
edges: ordered gate/release, response-scoped playback drain, content announced
before playback starts, abandoned empty audio, failed replies, overlapping tool
settlement, malformed arguments, stale completion, reconnect, and run-result
arrival while held or while user transcription is pending. They also exercise
latest-only progress, result-over-progress replacement, cancellation cleanup,
and failed update delivery. Backend cases use real isolated journals and test
exact-run ownership, terminal precedence, malformed rows, and no transcript-tail
fallback.

A live provider smoke using synthesized 24 kHz speech verified session-config
acceptance, input transcription, zero automatic responses before the client
request, an `agent_status` call, and a spoken response after a supplied test tool
result. That smoke used WebSocket transport.

A separate real Chrome/OpenAI WebRTC smoke exercised the shipped JavaScript with
synthetic silent input and mocked Hermes task endpoints. A result became ready
while a long reply was speaking; no result context or next response was sent
until that reply's `output_audio_buffer.stopped`. Both provider runs completed
without cancel/clear commands or provider errors; remote audio recordings decoded
and were non-silent. The final-code run sent the queued context 0.2 ms after the
prior drain event and received the next audio-start event 658.8 ms after it.
Those are one-run control-event timings, not measured loudspeaker latency or a
latency guarantee. The test did not compare audible quality against the old code.

Mocked tools and synthetic input do not prove real action execution or physical
speakerphone performance. Physical iPhone acceptance still requires a refreshed
client (and a backend restart only if its Python config is not yet loaded),
followed by concurrent nearby speech, an explicit hold/release, and an authorized
real Hermes task. This handoff change is browser-only and needs no server restart.
