# Stable live row placement

Classification: upstream-candidate
Base: fork combined release `02c390e6313d232539ac8e01c93bd11c8df7cb23`, including upstream `be5c07175049fc32e093231a8d7fc4b7127c0f0e`.
Maintenance owner: fork maintainers.
Upstream status: not-filed. Private details removed: yes.

Problem: a tool event after prose can move unchanged historical tool rows, clearing selection and forcing large layouts. A changing compact group can also detach its retained children to reconstruct the same wrapper.
Root cause: obsolete prose siblings remained present during reverse placement, so each preceding retained row appeared out of position; compact tool reconciliation flattened a nested group before regrouping it.
Change: remove obsolete siblings before placement in both live modes; reconcile changed compact tools inside the existing group and update its summary instead of replacing its wrapper. Legacy group markers use the same placement path; multiple sibling groups take the existing flatten/rebuild fallback. No cache, timer or new renderer.
Expected: unchanged historical rows and their selection/disclosure survive append and correction; real reorders, removal, shrinking groups and mode changes still reconcile.
Reproduction/verification: run `tests/browser_live_scene_energy.py` with the repository test interpreter. Chromium and WebKit cover real token/tool events, selection, disclosures, corrections, stale stream, reorder/removal, mode switches, legacy group markers, stray sibling groups and group shrink. The historical-row selection check fails on the base.
Compatibility: extends the fork's existing retained-tool renderer. For upstream reconstruction, first reconcile equivalent retained-tool work; do not import unrelated fork commits. Full transcript rendering is not changed. No physical iPad performance claim.
Rollback: revert this logical change and its browser regression; prior code remains functional but can clear selection on tool events. Retire when upstream provides equivalent placement semantics.
