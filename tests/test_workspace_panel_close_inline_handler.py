"""Regression: workspace panel close button must work even if boot.js dies mid-load.

The X (btnClearPreview) was the only button in the workspace panel header bound
exclusively via `$('btnClearPreview').onclick = handleWorkspaceClose;` late in
boot.js. Every sibling button carries an inline onclick attribute in the HTML.
In a long-lived tab that soft-reloads across a server update, mixed cached
assets can make boot.js throw before the binding line — every inline-bound
button keeps working while the X goes completely dead ("can't close workspace
panel"). An inline onclick fixes that: function declarations in boot.js hoist,
so handleWorkspaceClose exists even when execution stops early. The late
`element.onclick =` assignment still overrides the inline attribute in healthy
boots, so exactly one handler fires either way (same contract as
test_sprint44.py's test_btn_clear_preview_wired_to_handle_workspace_close).
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_btn_clear_preview_has_inline_onclick():
    assert 'id="btnClearPreview" onclick="handleWorkspaceClose()"' in HTML, (
        "btnClearPreview must carry an inline onclick so the close button works "
        "even when boot.js fails before its late JS binding runs"
    )


def test_inline_handler_matches_boot_js_target():
    """The inline handler must call the same function the JS binding does."""
    boot_js = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert "$('btnClearPreview').onclick=handleWorkspaceClose;" in boot_js
    assert "function handleWorkspaceClose()" in boot_js
