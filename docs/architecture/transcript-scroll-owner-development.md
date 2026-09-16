# Transcript scroll-owner development candidate

**Status: not approved for live deployment.** This branch records a rollback and
an exercised renderer prototype, not a completed scrolling fix.

## Scope

The separate rollback commit removes the net failed #7591 experiment in
`4764f2e2..30029e1a` from ui.js, sessions.js, and its associated test additions.
It retains independent loaded-history preservation and live-row improvements.
No saved virtualization preference is changed.

The candidate stages virtual-window DOM updates, retains unchanged keyed rows,
captures the current reader after history awaits, and uses a shared synchronous
position correction. It distinguishes measured zero-height collapsed activity
from unmeasured rows, aligns ranges with assistant-turn boundaries, and requests
older history on outward wheel input when no native scroll event is possible.

State layers: loaded S.messages is still owned by sessions.js; mounted ranges,
measurement cache, and reader geometry are owned by ui.js. Backend persistence,
authentication, settings defaults, and live server lifecycle are unchanged.

## Reproducible verification

Run with the supported repository test interpreter and installed Playwright
Chromium and WebKit binaries. Browser trials use temporary HOME/HERMES_HOME and
WebUI state, a loopback test server, and fixture transport. They do not use a real
provider or production credentials.

```bash
./scripts/test.sh tests/test_transcript_scroll_owner.py \
  tests/test_issue500_message_list_virtualization.py \
  tests/test_issue6414_programmatic_scroll_user_intent.py -q

BROWSERS=chromium,webkit .venv/bin/python \
  tests/browser_transcript_scroll_owner.py --artifacts /tmp/scroll-text

SCROLL_FIXTURE=tools BROWSERS=chromium,webkit .venv/bin/python \
  tests/browser_transcript_scroll_owner.py --cases natural \
  --artifacts /tmp/scroll-tools
```

`WEBUI_TEST_ROOT` selects an immutable comparison worktree. `--baseline-ref`
freezes the two changed JS assets from a revision; a full comparison worktree is
preferable when other assets differ. Candidate assets are frozen per test run
and their hashes recorded. `SCROLL_SESSION_FILE` optionally loads a local private
fixture, blocks external requests, and suppresses screenshots. Never publish
private fixture contents or private screenshots.

The oracle observes actual trusted wheel events and frame-sampled content
landmarks, not only scrollTop or row index. It rejects intra-row teleports,
wrong-direction motion, blank viewports, lost input, and recreated retained rows.
Endpoint exemptions are directional. Pagination executes the production pure
window helpers rather than approximating visible-message limits with raw rows.

## Evidence and remaining blockers

The final development checkpoint passed the selected 104 neighboring/unit tests
and the 48/48 Chromium/WebKit text-content matrix. The reported private long
session passed a desktop natural-input traversal after turn alignment. Neither
of those results establishes final acceptance or mobile tool-history quality.
The timestamped tool-heavy mobile fixture failed in both Chromium and WebKit.

The public tool-heavy fixture still exposes blank mobile viewports. Earlier
upstream and failed-stack controls also failed browser geometry gates. These
failures are not permission to weaken the oracle or report this candidate fixed.

Before deployment:

- Make virtual geometry and the renderer agree on complete assistant/worklog
  groups, including expanded activity and zero-height source anchors.
- Prove content completeness and correct turn association, not just row bounds.
- Cover the reader when it is inside tool/worklog content rather than a visible
  data-msg-idx segment.
- Reconcile the owned-window early return with the ordinary renderer's live-turn,
  disclosure, hydration, and postprocessing lifecycle.
- Prove observer/listener lifecycle and input ownership on session replacement,
  resize, touch momentum, cancellation, and teardown.
- Require the same final revision to pass all browser matrices, real-session
  follow-up, completion/recovery regressions, and retained-node tests.

Do not integrate this branch merely because its unit tests pass. Do not disable
virtualization as a substitute for solving bounded rendering.
