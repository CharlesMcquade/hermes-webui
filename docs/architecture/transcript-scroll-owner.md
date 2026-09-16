# Transcript window ownership (draft implementation)

## Problem and scope

Opted-in transcript virtualization can move the content under a reader while
mounting rows, prepending older history, or measuring newly laid-out content.
Position restoration must follow the painted content, not just `scrollTop` or
an estimated spacer height. Issue #7591 tracks the user-visible failure.

This implementation changes browser projection state in `static/ui.js` and the
prepend handoff in `static/sessions.js`. It does not change server persistence,
message pagination semantics, SSE events, activity-mode defaults, or the
`virtualize_transcript` default. It was extracted onto upstream `06d28c08`, not
submitted as a merge of the divergent development fork.

## Ownership

- `_messageWindowSnapshot` captures a painted source row or activity projection,
  including a within-content landmark and session-relative source index.
  Hidden or clipped activity cannot own the viewport.
- `_currentMessageVirtualWindow` retains that reader, distinguishes measured zero
  from an unknown height, and aligns assistant turns so a virtual gap does not
  change grouping semantics.
- `_loadOlderMessages` retains its existing request/session validation. It samples
  the reader after fetch completion and passes it into the owned prepend path.
- `_commitMessageWindow` stages rows off-DOM, inserts before removal, reuses
  unchanged keyed rows on window shifts, and performs one reader compensation.
  Reuse compares complete staged markup, not identity alone.
- Activity disclosure storage uses a session-relative source key on the legacy
  assistant-history path. `ensureActivityGroup` consumes the optional key and
  restore flag; callers not opting into this keep their existing behavior.
- `_reconcilePreservedLiveTurn` preserves upstream's parser-tail and structural
  superset decisions, but resolves the rebuilt turn inside the staged root.
- A page-lifetime ResizeObserver owns the reader snapshot. Session identity,
  message-array identity, render revision, input epoch, and position gate its
  use. New renders replace the snapshot; mismatched state cannot restore an old
  reader. The virtual scheduler coalesces one rAF and stops when its key is stable.
- Programmatic-scroll freshness suppresses follow interpretation, not mounting
  required by subsequent real input. Blank recovery requests an owned window
  update instead of recursively switching to a full transcript render.
- Markdown cache identity uses the complete input; the existing entry-count
  bound remains. Equal-length messages sharing a prefix and suffix must not
  paint one another's bodies.

Other settled anchor-scene disclosure identity policies are not redesigned here.
The optional disclosure handling supports the legacy-history owner used by this
change. Ordinary render/cache paths initialize the same reader ownership state.

## Verification on the upstream port

The independent browser gate serves actual app code with synthetic session/SSE
transport and isolated state. Pagination uses AST-extracted production message
window helpers. Candidate and baseline JS are frozen for each run. The oracle
checks painted content, offsets inside content, content identity, input travel,
blank frames, and mounted-row limits; failures remain failures.

Final source hashes and case results are in
[`results.json`](../images/transcript-scroll-owner/results.json). `head` in those
records is the pre-commit base; **source hashes identify the tested candidate**.

- Focused affected tests: **158 passed**.
- Broader 68-module neighbor selection: **703 passed, 1 skipped**. Selections
  overlap; these counts must not be added together.
- Text/lifecycle browser matrix: **60/60 passed**, Chromium and WebKit at
  desktop (1440x1000), narrow (820x900), and mobile (390x844).
- Synthetic tool-history activity prepend and trusted-wheel traversal:
  **12/12 passed**, both engines at all three widths.
- Same late-image oracle against upstream `06d28c08`: desktop failed with
  **884 px** landmark movement. Narrow/mobile controls failed earlier with a
  missing content landmark; they are not equivalent drift measurements.
- Cache-identity regression: **3/3 fail on base**, **3/3 pass on candidate**.
- Before the optional disclosure-key consumer was added to the extraction,
  desktop/mobile activity prepend failed. The unchanged composed tests passed
  after that dependency was restored.

Reproduce after preparing the repository test environment and installing
Playwright browsers according to `TESTING.md`:

```bash
BROWSERS=chromium,webkit VIEWPORTS=desktop,narrow,mobile \
  .venv/bin/python tests/browser_transcript_scroll_owner.py \
  --cases continuity,identity,prepend,input,switch,image,cold,stream,cache,disclosure

SCROLL_FIXTURE=tools BROWSERS=chromium,webkit VIEWPORTS=desktop,narrow,mobile \
  .venv/bin/python tests/browser_transcript_scroll_owner.py \
  --cases activity,natural --artifacts /tmp/hermes-scroll-tools

BROWSERS=chromium VIEWPORTS=desktop \
  .venv/bin/python tests/browser_transcript_scroll_owner.py \
  --baseline-ref 06d28c08 --cases image --artifacts /tmp/hermes-scroll-before
```

Neighbor selection: run `./scripts/test.sh` over `tests/test_*.py` whose filenames
contain `scroll`, `virtual`, `worklog`, `anchor`, or `disclosure`. The focused
selection additionally includes cache identity and midstream-flicker guards.

### Public synthetic screenshots

These are end-of-test screenshots, not proof of continuous motion. The desktop
pair shows different outcomes of the same late-image test. The narrow baseline
failed before finding its intended landmark, which explains the different row.
All screenshots contain generated fixture data only.

| Viewport | Before | After |
| --- | --- | --- |
| Desktop | ![Before desktop](../images/transcript-scroll-owner/before-desktop.png) | ![After desktop](../images/transcript-scroll-owner/after-desktop.png) |
| Narrow | ![Before narrow](../images/transcript-scroll-owner/before-narrow.png) | ![After narrow](../images/transcript-scroll-owner/after-narrow.png) |

## Remaining gates — not merge-ready

The reporter confirmed the development-fork replacement scrolls well in the
actual browser, with virtualization enabled. That is separate evidence from
these upstream-port tests.

A live tool-heavy **Transparent Stream** trial still exceeded its mounted-row
acceptance bound, reaching **555 observed rows**. Passing the default Compact
Worklog synthetic matrix does not resolve this. Whole-turn alignment can retain
large turns; the bounded-rendering contract still needs a solution and an
explicit all-display-mode gate before promoting this draft.

The full repository suite, physical-device touch momentum, real provider/network
reconnect, all-display-mode bounded traversal, browser auth-on behavior, and
composition with open PRs #7280/#7283 were not certified here. Live reconciliation
coverage uses synthetic transport and checks parser connectivity, not provider
reliability. Fork-only completion/reconnect scripts are not part of this port
and their historical passes are not counted above.

Related work: #6151/#6155 discuss default-on virtualization and performance;
#7280 addresses synchronous post-process drift; #7283 addresses height-estimator
calibration. This draft does not claim to supersede those proposals or authorize
a default change. Keep #7591 open pending upstream integration and remaining gates.
