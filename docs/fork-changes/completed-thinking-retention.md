# Retain unchanged completed thinking rows

Classification: upstream-candidate. Upstream status: not-filed.
Upstream base: nesquena/hermes-webui master, be5c07175049fc32e093231a8d7fc4b7127c0f0e; fork integration base 02c390e6313d232539ac8e01c93bd11c8df7cb23.
Maintenance owner: fork maintainer. Private details removed: yes.

Problem / reproduction: `tests/browser_completed_thinking_retention.py` loads a mixed 100-tool/100-completed-thinking turn and emits a tool start/completion pair through the real SSE handlers. Previously this rebuilt all 100 completed thinking nodes twice, despite unchanged content.
Expected: unchanged completed content and reader interaction survive unrelated events without construction work.
Root cause: only tool rows participated in signature-before-construction reconciliation.
Change: extend the existing node-owned render signature to visible completed thinking, including full row, render options, language and disclosure default. Running reasoning and prose still take their existing render path; non-retained renders clear any prior signature. No global cache, persistence, throttle or new renderer.
Verification: run the browser regression in Chromium and WebKit, plus `tests/test_live_to_final_anchor_visible_order.py`, `tests/test_issue5367_transparent_live_row_reconcile.py`, `tests/test_issue5720_reasoning_owner.py`. Browser assertions cover zero redundant builds, selection/disclosure, canonical correction, hide/show, HTML snapshot rehydration, active-to-completed transitions, removal and mode switch.
Compatibility / risk: node identity is preserved only when the full signature matches; restored HTML rebuilds once. Synthetic desktop engines do not prove physical-device latency or energy consumption. This follows the fork's retained-row reconciliation and stable-placement patches; reconstruct those seams or use an equivalent upstream reconciler before porting.
Rollback: revert this logical change and its tests together; no data migration.
Retirement: replace with equivalent upstream completed-row retention after replaying the regression; no upstream issue or PR is implied.
