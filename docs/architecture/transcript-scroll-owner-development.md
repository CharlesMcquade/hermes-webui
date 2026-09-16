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

The original tool-heavy mobile "blank viewport" reports counted only indexed
message segments. Same-frame DOM inspection found visible compact-worklog
reasoning outside those segments. The browser oracle now includes these
projections with source-message identities and ancestor-clipped geometry. This
correction does **not** make the candidate green: the `ca00f0c1` control still
fails natural mobile traversal in both engines with approximately 1,566px
content jumps.

The new `activity` case (`SCROLL_FIXTURE=tools`) places a paragraph in an
activity-only viewport, proves a visibility mutation is detected, and loads
older history. The `ca00f0c1` control fails in Chromium and WebKit mobile: the
landmark moves from approximately 100px to -53px. This proves activity-position
loss across the sequence, not that the prepend is its only position writer.
The follow-up activity-owner and disclosure-identity fixes pass this case at all
three viewport widths in both engines. Natural traversal remains a separate gate.
Earlier upstream and failed-stack controls also failed browser geometry gates.
None of these results is permission to report the candidate fixed.

## Follow-up review fixes

Cache-hit ownership initialization and post-commit nested disclosure offsets are
now fixed. The new `cache` and `disclosure` browser cases fail against `9a5add3b`
(stale `other` session stamp / nested scroll reset from 120 to 0) and pass on the
follow-up candidate in Chromium and WebKit at desktop, narrow, and mobile widths
(12 checks). The targeted 35 unit tests pass as well.

Activity and live-turn follow-up:

- Reader snapshots now resolve worklog reasoning/tool projections to their source
  and retain a within-content landmark. Activity disclosure state uses a
  session-relative source key and the existing disclosure restoration mechanism;
  raw loaded-slice indices no longer erase expansion intent on prepend.
- The `activity` case uses the real disclosure click handler and upward wheel
  input before placing its landmark. It fails on `ca00f0c1` in both mobile
  engines and passes on the candidate in desktop, narrow, and mobile (6 checks).
  Isolated activity helper layout tests cover reasoning and tool bodies.
- Owned commits now share the ordinary renderer's live-turn reconciliation,
  preserving the parser-owned segment without discarding richer staged content.
  All 8 real-browser reconciliation tests fail against `ca00f0c1` and pass on
  the candidate, covering both owned-window entry points in both engines.
- Follow-up verification: 119 selected unit/neighboring tests; 24 cache,
  disclosure, streaming and switch browser checks; 48 text browser checks.
  The combined text command timed out after completing Chromium, so WebKit was
  rerun separately to completion. These are not natural tool-history acceptance.
- The temporary natural diagnosis script catches assertions into evidence fields;
  its process-level PASS is diagnostic completion, **not** an acceptance pass.
  Natural tool-heavy traversal remains unresolved and blocks deployment.
- Historical scene hydration and disclosure open-state restoration already run
  before the owned early return. The disclosure defect was detached layout, not
  general omission of hydration. Do not report that broader claim as established.

Before deployment:

The mount-guard/clipped-owner follow-up moves range maintenance before fresh
programmatic-scroll suppression while keeping follow/unpin interpretation
behind that guard. Snapshot candidates are clipped against overflow ancestors;
positive layout height alone does not mean a collapsed worklog is painted.
Four behavioral cases fail on `289c31e5` and pass on this follow-up (two browser
engines for required mount scheduling/idle convergence; desktop and mobile for
clipped ownership). Verification: 44 targeted tests, 54 text/lifecycle browser
checks, and 6 expanded-activity browser checks passed. The unchanged natural
mobile tools gate still fails upward paging movement in both engines. This
follow-up is not a completed scrolling fix.

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
