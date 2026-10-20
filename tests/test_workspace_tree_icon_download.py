"""Regression tests — workspace tree file icon click must download the file.

Bug: the icon rendered to the left of each file name (fileIcon()) had no click
handler of its own, so icon clicks bubbled to the row's onclick → openFile()
→ the preview pane. PDF rows render a *download* glyph
(fileIcon maps .pdf to li('download', 14)) and DOWNLOAD_EXTS rows download on
any click anyway, but preview-able types (PDF foremost) opened the preview —
the icon promised "download" and delivered "preview".

Fix contract (#user report: "Clicking the actual item (the name of it) opens
preview. Clicking the download icon to the left of the name actually downloads
that item."), as repaired from PR #7810 (fb6296e8):

  * ONLY download glyphs (DOWNLOAD_EXTS + .pdf, minus image/markdown families)
    bind the download action — every other file-type icon keeps bubbling to
    the row's preview handler, so filename and icon hit regions stay distinct;
  * download-glyph icons bind iconEl.onclick → downloadFile(item.path) with
    e.stopPropagation() so the row's preview handler never fires;
  * the file NAME click keeps the #1707 debounce → el.onclick delegation
    (single click previews, double click renames) — untouched;
  * directory-like rows keep the expand/collapse toggle (no download handler);
  * read-only escape rows (authorized external symlinks) keep their gated
    navigation flow — no direct download bypass;
  * keyboard: role="button", tabindex=0, aria-label; Enter or Space downloads
    exactly once — held-key auto-repeat (e.repeat) is suppressed, Space never
    page-scrolls (preventDefault on the keydown), and activation never
    bubbles to the row preview;
  * double-click on the icon downloads once, not twice (e.detail guard);
  * the download-glyph CSS class must not inflate PDF row geometry
    (no min-height/min-width:44px on .file-icon-download — compact tree rows
    and narrow containers keep their height).

LIMITED renderer-level evidence: these tests drive the icon block through a
Node VM and static-analyze ui.js/style.css. Native browser download behavior
(content-disposition attachment, actual file save) is a parent/functional
gate, not covered here.

These tests static-analyze static/ui.js and drive the icon block through a
Node VM.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
I18N_JS_PATH = REPO_ROOT / "static" / "i18n.js"
ICONS_JS_PATH = REPO_ROOT / "static" / "icons.js"
STYLE_CSS_PATH = REPO_ROOT / "static" / "style.css"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _icon_block() -> str:
    """Return the icon construction block of _renderTreeItems."""
    src = _read(UI_JS_PATH)
    start_marker = "const iconEl=document.createElement('span');"
    start = src.index(start_marker)
    end_marker = "el.appendChild(iconEl);"
    end = src.index(end_marker, start)
    return src[start:end + len(end_marker)]


def _name_handler_block() -> str:
    """Return the name element handler block (from the rename tooltip to the
    appendChild(nameEl) line) — the #1707 contract region."""
    src = _read(UI_JS_PATH)
    start_marker = "if(isLk && item.target)"
    start = src.find(start_marker)
    if start < 0:
        start_marker = "nameEl.title=t('double_click_rename');"
        start = src.find(start_marker)
    assert start >= 0, "nameEl rename tooltip not found in static/ui.js"
    end_marker = "el.appendChild(nameEl);"
    end = src.find(end_marker, start)
    assert end >= 0, "el.appendChild(nameEl) not found after rename tooltip"
    return src[start:end + len(end_marker)]


# ── Source-level regression locks ─────────────────────────────────────────────


class TestIconDownloadHandlerShape:
    def test_icon_binds_download_click_handler(self):
        block = _icon_block()
        assert "iconEl.onclick=" in block, (
            "the file icon must bind its own onclick handler — without it the "
            "click bubbles to the row's openFile (preview) handler"
        )

    def test_icon_handler_calls_downloadfile_with_item_path(self):
        block = _icon_block()
        assert "downloadFile(item.path)" in block, (
            "icon click must call downloadFile(item.path), not openFile"
        )

    def test_icon_handler_stops_propagation(self):
        block = _icon_block()
        assert "e.stopPropagation();" in block, (
            "icon click must stopPropagation so the row's preview handler "
            "does not also fire"
        )

    def test_icon_handler_gated_to_writable_file_rows_with_download_glyph(self):
        block = _icon_block()
        assert "if(isFileLike && !isReadOnlyEscape && hasDownloadGlyph){" in block, (
            "download handler must be gated to file-like rows that are not "
            "read-only escape rows AND render a download glyph — other file "
            "icons keep the row's preview behavior"
        )

    def test_icon_has_download_tooltip(self):
        block = _icon_block()
        assert "t('media_download')" in block, (
            "icon must carry a Download tooltip (existing media_download key)"
        )

    def test_downloadfile_undefined_guard(self):
        block = _icon_block()
        assert "typeof downloadFile==='function'" in block, (
            "icon handler must guard downloadFile with typeof — downloadFile "
            "lives in static/workspace.js, a sibling global script"
        )

    def test_icon_still_appended_after_handler(self):
        """Ordering lock (#2554 family): handler wiring must not move the
        appendChild — icon construction still ends at el.appendChild(iconEl)."""
        src = _read(UI_JS_PATH)
        start = src.index("const iconEl=document.createElement('span');")
        end = src.index("el.appendChild(iconEl);", start)
        assert end > start


class TestKeyboardAccessibility:
    """Keyboard/ARIA correctness for the download glyph (#7810 repair)."""

    def test_icon_is_a_keyboard_reachable_button(self):
        block = _icon_block()
        assert "setAttribute('role','button')" in block, (
            "download glyph must expose role=button"
        )
        assert "setAttribute('tabindex','0')" in block, (
            "download glyph must be focusable via keyboard"
        )
        assert "aria-label" in block, (
            "download glyph must carry an aria-label naming the file"
        )

    def test_keydown_handles_enter_and_space(self):
        block = _icon_block()
        assert "e.key==='Enter'||e.key===' '" in block, (
            "Enter and Space must both activate the download glyph"
        )

    def test_keydown_prevents_default(self):
        """preventDefault must run before any early return so held Space
        never page-scrolls the document."""
        block = _icon_block()
        assert "e.preventDefault();" in block

    def test_keydown_suppresses_auto_repeat(self):
        block = _icon_block()
        assert "e.repeat" in block, (
            "held-key auto-repeat must be suppressed — one download per "
            "intentional Enter/Space press"
        )

    def test_keydown_stops_propagation(self):
        block = _icon_block()
        # Both the click and keydown paths must stop propagation.
        assert block.count("e.stopPropagation();") >= 2, (
            "click and keydown activation must not bubble to the row preview"
        )

    def test_double_click_downloads_once(self):
        block = _icon_block()
        assert "e.detail>1" in block, (
            "the second click of a double-click must not start another download"
        )


class TestDownloadGlyphHelper:
    """The glyph predicate lives in static/icons.js beside the icon mapping."""

    def test_helper_exists_in_icons_js(self):
        src = _read(ICONS_JS_PATH)
        assert "function isDownloadGlyph(" in src, (
            "static/icons.js must define isDownloadGlyph(name, type)"
        )

    def test_helper_gates_to_download_glyphs(self):
        src = _read(ICONS_JS_PATH)
        start = src.index("function isDownloadGlyph(")
        end = src.index("\n}", start)
        body = src[start:end]
        assert "DOWNLOAD_EXTS" in body, "predicate must consult DOWNLOAD_EXTS"
        assert "'.pdf'" in body or '".pdf"' in body, (
            "predicate must treat .pdf as a download glyph (fileIcon renders "
            "the download glyph for .pdf)"
        )

    def test_css_class_has_geometry_safe_rule(self):
        css = _read(STYLE_CSS_PATH)
        assert ".file-icon-download{" in css, (
            "style.css must style .file-icon-download"
        )
        start = css.index(".file-icon-download{")
        end = css.index("}", start)
        rule = css[start:end]
        assert "min-height" not in rule and "min-width" not in rule, (
            ".file-icon-download must not use min-width/min-height hit-area "
            "inflation (44px) — it inflates PDF row height and breaks narrow "
            "containers; keep compact row geometry"
        )
        assert "focus-visible" in css[start:end + 200], (
            ".file-icon-download needs a :focus-visible ring for keyboard use"
        )


class TestNameClickContractUntouched:
    """The #1707 contract (single click on name → preview after debounce,
    double click → rename) must remain exactly as it was."""

    def test_name_click_still_delegates_to_row_handler(self):
        block = _name_handler_block()
        assert "el.onclick(" in block, (
            "nameEl.onclick must still delegate to el.onclick after the "
            "300ms debounce — the icon change must not touch the name path"
        )

    def test_name_click_still_uses_debounce(self):
        block = _name_handler_block()
        assert "setTimeout" in block and "clearTimeout" in block, (
            "nameEl debounce machinery must remain"
        )


class TestI18nKey:
    def test_media_download_key_exists_in_locales(self):
        i18n = _read(I18N_JS_PATH)
        count = i18n.count("media_download:")
        assert count >= 10, (
            f"media_download key must exist across locale blocks; found {count}"
        )


# ── Behavioral tests via Node VM ──────────────────────────────────────────────

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node_vm(item_type: str, read_only_escape: bool,
                 name: str = "foo.pdf") -> dict:
    """Execute the icon block with mocked DOM/globals and drive interactions."""
    block = _icon_block()
    payload = {
        "block": block,
        "itemType": item_type,
        "readOnlyEscape": read_only_escape,
        "name": name,
    }
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
const block = params.block;
const itemType = params.itemType;
const readOnlyEscape = params.readOnlyEscape;

let downloadCalls = [];
let stopPropagationCalls = 0;
let openCalls = 0;
let preventDefaultCalls = 0;
let appendedTo = null;

const document = {
  createElement: (tag) => ({
    tagName: tag.toUpperCase(),
    className: '', classList: {add() {}}, textContent: '', title: '',
    innerHTML: '', onclick: null, onkeydown: null, attributes: {},
    setAttribute(key, val) { this.attributes[key] = val; },
    style: {},
    appendChild(child) {},
  }),
};
const li = () => '';
const fileExt = (p) => { const i = p.lastIndexOf('.'); return i >= 0 ? p.slice(i).toLowerCase() : ''; };
const IMAGE_EXTS = new Set(['.png', '.jpg']);
const MD_EXTS = new Set(['.md']);
const DOWNLOAD_EXTS = new Set(['.zip', '.tar.gz', '.whl']);
const isDownloadGlyph = (n, ty) => {
  if (ty === 'dir' || ty === 'symlink') return false;
  const e = fileExt(n);
  if (IMAGE_EXTS.has(e) || MD_EXTS.has(e)) return false;
  return DOWNLOAD_EXTS.has(e) || e === '.pdf';
};
const fileIcon = (n) => isDownloadGlyph(n) ? 'download' : 'file-text';
const t = (key) => ({media_download: 'Download'}[key] || key);
const downloadFile = (path) => { downloadCalls.push(path); };
const el = { appendChild(child) { appendedTo = child; }, onclick() { openCalls++; } };
const item = { type: itemType, name: params.name, path: 'dir/' + params.name };

const isLk = itemType === 'symlink';
const isExternalLink = false;
const isDirLike = itemType === 'dir' || (isLk && itemType === 'dir');
const isFileLike = !isDirLike;
const isReadOnlyEscape = readOnlyEscape;

const runner = new Function(
  'document', 'li', 'fileIcon', 'fileExt', 'IMAGE_EXTS', 'MD_EXTS',
  'DOWNLOAD_EXTS', 'isDownloadGlyph', 't', 'downloadFile', 'el', 'item',
  'isLk', 'isExternalLink', 'isDirLike', 'isFileLike', 'isReadOnlyEscape',
  '(()=>{' + block + '})();'
);
runner(document, li, fileIcon, fileExt, IMAGE_EXTS, MD_EXTS, DOWNLOAD_EXTS,
       isDownloadGlyph, t, downloadFile, el, item,
       isLk, isExternalLink, isDirLike, isFileLike, isReadOnlyEscape);

const iconEl = appendedTo;
let stoppedClick = false;
function click(detail) {
  stoppedClick = false;
  const event = {detail, stopPropagation() { stoppedClick = true; stopPropagationCalls++; }};
  if (typeof iconEl.onclick === 'function') iconEl.onclick(event);
  if (!stoppedClick) el.onclick(event);
}
click(1);
if (params.name === 'double.pdf') click(2);
function key(k, repeat) {
  const event = {key: k, repeat: !!repeat,
                 preventDefault() { preventDefaultCalls++; },
                 stopPropagation() { stopPropagationCalls++; }};
  if (typeof iconEl.onkeydown === 'function') iconEl.onkeydown(event);
  // Unstopped keydown would scroll/bubble to the row: model the preview leak.
  else el.onclick(event);
}
if (params.name === 'keyboard.pdf') {
  key('Enter', false);
  key(' ', false);
  // Held Space: OS auto-repeat delivers repeated keydown events.
  key(' ', true); key(' ', true); key(' ', true);
}

console.log(JSON.stringify({
  iconHasHandler: !!(iconEl && typeof iconEl.onclick === 'function'),
  downloadCalls,
  openCalls,
  stopPropagationCalls,
  preventDefaultCalls,
  role: iconEl ? iconEl.attributes.role : null,
  tabindex: iconEl ? iconEl.attributes.tabindex : null,
  label: iconEl ? iconEl.attributes['aria-label'] : null,
  title: iconEl ? iconEl.title : null,
}));
"""
    )
    r = subprocess.run(
        [str(NODE), "-e", js],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestIconDownloadBehavior:
    def test_file_icon_click_downloads(self):
        """Clicking a PDF (download glyph) calls downloadFile with item path."""
        out = _run_node_vm(item_type="file", read_only_escape=False)
        assert out["iconHasHandler"] is True, (
            f"PDF row icon must have a click handler; got {out}"
        )
        assert out["downloadCalls"] == ["dir/foo.pdf"], (
            f"icon click must call downloadFile('dir/foo.pdf'); got {out}"
        )
        assert out["stopPropagationCalls"] == 1, (
            f"icon click must stopPropagation exactly once; got {out}"
        )
        assert out["openCalls"] == 0, (
            f"icon click must not bubble to the row preview; got {out}"
        )
        assert out["title"] == "Download", (
            f"icon must carry the Download tooltip; got {out}"
        )

    def test_archive_glyph_downloads(self):
        out = _run_node_vm(item_type="file", read_only_escape=False,
                           name="archive.zip")
        assert out["iconHasHandler"] is True, out
        assert out["downloadCalls"] == ["dir/archive.zip"], out
        assert out["openCalls"] == 0, out

    def test_non_download_glyph_keeps_row_preview(self):
        """Non-download file-type icons must NOT bind a download handler —
        their click bubbles to the row's preview handler (distinct hit
        regions: download glyph downloads, everything else previews)."""
        for name in ("analysis.md", "main.py", "config.json", "data.csv",
                     "photo.png", "notes.txt"):
            out = _run_node_vm(item_type="file", read_only_escape=False,
                               name=name)
            assert out["iconHasHandler"] is False, (name, out)
            assert out["downloadCalls"] == [], (name, out)
            assert out["openCalls"] == 1, (name, out)

    def test_unicode_and_spaced_pdf_names_download(self):
        for name in ("résumé final.pdf", "报告 报告.pdf", "a b c d e f g.pdf"):
            out = _run_node_vm(item_type="file", read_only_escape=False,
                               name=name)
            assert out["downloadCalls"] == ["dir/" + name], (name, out)
            assert out["openCalls"] == 0, (name, out)

    def test_dir_icon_has_no_download_handler(self):
        """Directory rows keep the expand/collapse toggle — no download."""
        out = _run_node_vm(item_type="dir", read_only_escape=False)
        assert out["iconHasHandler"] is False, (
            f"directory icon must not bind a download handler; got {out}"
        )
        assert out["downloadCalls"] == [], (
            f"directory icon click must not download; got {out}"
        )

    def test_read_only_escape_icon_has_no_download_handler(self):
        """Read-only escape rows keep their gated navigation flow."""
        out = _run_node_vm(item_type="file", read_only_escape=True)
        assert out["iconHasHandler"] is False, (
            f"read-only escape icon must not bind a download handler; got {out}"
        )
        assert out["downloadCalls"] == [], (
            f"read-only escape icon click must not download (bypasses the "
            f"escape gate); got {out}"
        )


class TestDoubleClickAndKeyboardBehavior:
    def test_double_click_downloads_once(self):
        out = _run_node_vm(item_type="file", read_only_escape=False,
                           name="double.pdf")
        assert out["downloadCalls"] == ["dir/double.pdf"], out
        assert out["openCalls"] == 0, out

    def test_enter_and_space_download_once_despite_auto_repeat(self):
        out = _run_node_vm(item_type="file", read_only_escape=False,
                           name="keyboard.pdf")
        assert out["role"] == "button", out
        assert out["tabindex"] == "0", out
        assert out["label"] == "Download keyboard.pdf", out
        # Icon click + one Enter press + one intentional Space press = 3
        # downloads. The 3 held-Space repeat events must not add more.
        assert out["downloadCalls"] == ["dir/keyboard.pdf"] * 3, out
        assert out["openCalls"] == 0, out
        # preventDefault ran for every Enter/Space keydown (incl. repeats)
        # so held Space never page-scrolls.
        assert out["preventDefaultCalls"] == 5, out

    def test_symlink_icon_keeps_original_behavior(self):
        out = _run_node_vm(item_type="symlink", read_only_escape=False,
                           name="foo.pdf")
        assert out["iconHasHandler"] is False, out
        assert out["openCalls"] == 1, out
        assert out["downloadCalls"] == [], out
