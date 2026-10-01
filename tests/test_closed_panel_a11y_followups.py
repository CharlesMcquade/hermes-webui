"""Browser regression coverage for the follow-up a11y gaps left by #7866.

#7866 took the closed compact workspace drawer out of the tab order. Its review
comment (Codex, 2026-09-30) left three small non-blocking follow-ups, all with the
same root cause as the drawer itself: an off-canvas panel that stays laid out so it
can animate keeps its whole subtree reachable by Tab.

1. The closed LEFT sidebar parks off-screen via translateX(-100%) and carried the
   same defect: every control in it (session list, new-chat, file actions) stayed
   tabbable, so a keyboard user tabbing forward walked off-screen into an invisible
   tree.
2. #workspaceFileInput, the programmatic target of triggerWorkspaceUpload(), was a
   tab stop even with the drawer OPEN — one Tab press moved focus onto a
   1px, opacity:0, left:-9999px input and focus appeared to vanish.
3. Closing a panel that had focus inside it left document.activeElement on the now
   invisible control (visibility:hidden does not clear it), so the next Tab
   restarted from that dead node and focus vanished for a press.

These probes drive the browser's REAL tab sequence from a control placed immediately
before the panel, because that is the walk the user takes; a computed-style or
el.tabIndex assertion cannot see a future rule that re-enables focus on a hidden
subtree. The production stylesheet and the real index.html markup are loaded
unchanged, so the same tests pin that the open panels still work afterwards.

The 641-900px band is deliberately NOT covered, for the reason given in #7866: that
band uses display:none (its off-canvas geometry is #6952's open scope) and already
keeps its subtree out of the tab order.
"""
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")

# The shipped compact band: the <=640px slide-in overlay, probed at its widest and
# narrowest points so the fix is pinned at both edges.
_BANDS = ({"width": 390, "height": 780}, {"width": 640, "height": 780})

# A representative slice of the sidebar's control set: the session-list search box
# plus the new-chat and reload buttons in the titlebar. If the closed sidebar is
# inert, none of them can be reached; if the fix regresses, the walk hits all three.
_SIDEBAR_CONTROLS = (
    "sidebarSearch",
    "btnNewChat",
    "btnReload",
)

_TAB_STEPS = 14


def _sidebar_html() -> str:
    controls = "\n".join(
        f'    <input id="{cid}">' if cid == "sidebarSearch" else f'    <button id="{cid}">{cid}</button>'
        for cid in _SIDEBAR_CONTROLS
    )
    return f"""<div class="layout" id="layout">
  <button id="beforeSidebar">Before the sidebar</button>
  <aside class="sidebar" id="sidebar">
{controls}
  </aside>
  <main class="chat-shell"></main>
</div>"""


def _page_script() -> str:
    """Helpers that report where the real tab sequence actually lands."""
    return """
    window.__focusBeforeSidebar = () => document.getElementById('beforeSidebar').focus();
    window.__activeDescriptor = () => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      return {
        id: el.id || null,
        inSidebar: !!el.closest('.sidebar'),
      };
    };
    window.__openSidebar = () => {
      document.querySelector('.sidebar').classList.add('mobile-open');
    };
    window.__sidebarPointerEvents = () =>
      getComputedStyle(document.querySelector('.sidebar')).pointerEvents;
    window.__sidebarVisibility = () =>
      getComputedStyle(document.querySelector('.sidebar')).visibility;
    """


def _tab_hits_in_sidebar(page) -> list:
    """Walk the browser's real tab sequence and report sidebar hits, by control id."""
    page.evaluate("window.__focusBeforeSidebar()")
    hits: list[str] = []
    for _ in range(_TAB_STEPS):
        page.keyboard.press("Tab")
        state = page.evaluate("window.__activeDescriptor()")
        if state is None:
            break
        if state["inSidebar"]:
            hits.append(state["id"] or "<unnamed>")
            if len(hits) >= len(_SIDEBAR_CONTROLS):
                break
    return hits


def _chromium():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the sidebar a11y browser test")
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    return playwright, browser


@pytest.mark.parametrize("viewport", _BANDS, ids=["390px", "640px"])
def test_closed_compact_sidebar_is_out_of_the_tab_order(viewport):
    """Follow-up 1: the closed left sidebar must expose none of its controls to the
    tab sequence, and the open sidebar must expose all of them."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size(viewport)
        page.set_content(
            f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>{_sidebar_html()}<script>{_page_script()}</script></body></html>"""
        )
        closed_hits = _tab_hits_in_sidebar(page)
        closed_visibility = page.evaluate("window.__sidebarVisibility()")
        closed_pointer_events = page.evaluate("window.__sidebarPointerEvents()")
        page.evaluate("window.__openSidebar()")
        # The sidebar animates its transform, so let the open transition settle
        # before walking the open tab order.
        page.wait_for_timeout(400)
        open_visibility = page.evaluate("window.__sidebarVisibility()")
        open_hits = _tab_hits_in_sidebar(page)
    finally:
        browser.close()
        playwright.stop()

    assert closed_hits == [], (
        f"the closed compact sidebar at {viewport['width']}px still puts "
        f"{closed_hits} in the keyboard tab order"
    )
    assert sorted(open_hits) == sorted(_SIDEBAR_CONTROLS), (
        f"the open compact sidebar must expose every control at "
        f"{viewport['width']}px, got {open_hits}"
    )
    assert closed_visibility == "hidden", (
        "the parked sidebar must be visibility:hidden, which is what takes its "
        f"subtree out of the tab order (got {closed_visibility!r})"
    )
    assert open_visibility == "visible", (
        f"the open sidebar must be visible at {viewport['width']}px, "
        f"got {open_visibility!r}"
    )
    assert closed_pointer_events == "none", (
        "the parked sidebar must not swallow clicks aimed at the chat underneath it"
    )


def test_closed_sidebar_slide_animation_is_not_cut_short():
    """The inert rules must not kill the close animation: visibility flips only
    after the 250ms slide-out, so the sidebar is still visible while it slides."""
    # Anchor on the compact-band rule itself rather than splitting the stylesheet
    # into media-query segments, which breaks as soon as another query follows.
    anchor = ".sidebar{position:fixed;left:0;top:0;bottom:0;width:100vw"
    start = STYLE_CSS.find(anchor)
    assert start != -1, "the compact sidebar rule is missing from style.css"
    sidebar_rule = STYLE_CSS[start : STYLE_CSS.index("}", start)]
    open_anchor = ".sidebar.mobile-open{"
    open_start = STYLE_CSS.index(open_anchor, start)
    open_rule = STYLE_CSS[open_start : STYLE_CSS.index("}", open_start)]

    # Closed: the delayed visibility step must outlast the transform, otherwise the
    # panel blanks out before it has finished sliding away.
    assert "visibility 0s linear .25s" in sidebar_rule, (
        "the closed sidebar must delay its visibility flip past the 250ms "
        f"transform transition, got {sidebar_rule!r}"
    )
    assert "transform .25s ease" in sidebar_rule, (
        f"the closed sidebar must keep its transform transition, got {sidebar_rule!r}"
    )
    assert "visibility:hidden" in sidebar_rule, (
        f"the closed sidebar must be visibility:hidden, got {sidebar_rule!r}"
    )
    # Open: the flip must be immediate so the panel is focusable the moment it
    # arrives, not one transition late.
    assert "visibility 0s linear 0s" in open_rule, (
        f"the open sidebar must apply visibility immediately, got {open_rule!r}"
    )
    assert "visibility:visible" in open_rule, (
        f"the open sidebar must be visible, got {open_rule!r}"
    )


def test_workspace_file_input_is_not_a_tab_stop():
    """Follow-up 2: the hidden upload target must be reachable only
    programmatically. The visible #btnUploadWorkspace button is the tab stop."""
    assert 'id="workspaceFileInput"' in INDEX_HTML, (
        "the workspace file input is missing from index.html"
    )
    input_tag = next(
        line
        for line in INDEX_HTML.splitlines()
        if 'id="workspaceFileInput"' in line
    )
    assert 'tabindex="-1"' in input_tag, (
        "#workspaceFileInput must carry tabindex=\"-1\" so it stops swallowing a Tab "
        f"press while focus is visibly nowhere; got {input_tag.strip()!r}"
    )
    # The user-facing affordance must remain a normal, focusable button.
    assert 'id="btnUploadWorkspace"' in INDEX_HTML, (
        "the visible upload button must stay in the markup"
    )
    upload_tag = next(
        line for line in INDEX_HTML.splitlines() if 'id="btnUploadWorkspace"' in line
    )
    assert "tabindex" not in upload_tag, (
        "the visible upload button must stay in the tab order; a tabindex here "
        f"would swap one lost focus for another: {upload_tag.strip()!r}"
    )


def test_upload_still_opens_the_picker_from_the_visible_button():
    """tabindex=\"-1\" must not break the upload path: triggerWorkspaceUpload()
    drives the input with a programmatic .click(), which does not need focus."""
    start = WORKSPACE_JS.find("function triggerWorkspaceUpload(")
    assert start != -1, "triggerWorkspaceUpload() is missing from workspace.js"
    body = WORKSPACE_JS[start : WORKSPACE_JS.find("\n}", start) + 2]
    assert "input.click()" in body, (
        "triggerWorkspaceUpload() must still open the picker via a programmatic "
        f"click (tabindex=\"-1\" only removes the tab stop); got {body!r}"
    )
    assert "focus()" not in body, (
        "triggerWorkspaceUpload() must not depend on focusing the input, or "
        "tabindex=\"-1\" would have to come back out"
    )


def test_closing_a_panel_releases_focus_from_inside_it():
    """Follow-up 3: closing a panel that held the focus must park focus on <body>,
    and must NOT steal it when the focus was somewhere else entirely (that would
    yank a user out of the composer mid-sentence)."""
    assert "function _releaseFocusFromClosedPanel(" in BOOT_JS, (
        "the focus-release helper is missing from boot.js"
    )
    start = BOOT_JS.find("function _releaseFocusFromClosedPanel(")
    helper = BOOT_JS[start : BOOT_JS.find("\n}", start) + 2]
    assert "panel.contains(active)" in helper, (
        "the helper must only rescue focus that is actually inside the panel being "
        f"closed; got {helper!r}"
    )
    assert "blur()" in helper, (
        f"the helper must release the focus; got {helper!r}"
    )

    # Both close paths must call it: the workspace drawer (via _setWorkspacePanelMode)
    # and the left sidebar (via closeMobileSidebar).
    mode_start = BOOT_JS.find("function _setWorkspacePanelMode(")
    mode_body = BOOT_JS[mode_start : BOOT_JS.find("\n}", mode_start) + 2]
    assert "_releaseFocusFromClosedPanel(panel)" in mode_body, (
        "_setWorkspacePanelMode() must release focus when it closes the drawer; "
        f"got {mode_body!r}"
    )
    # Only on the close leg: opening must never steal focus.
    assert "if(!open)_releaseFocusFromClosedPanel(panel);" in mode_body, (
        "the focus release must be guarded on the close leg so opening the panel "
        f"leaves the user's focus alone; got {mode_body!r}"
    )

    close_start = BOOT_JS.find("function closeMobileSidebar(")
    close_body = BOOT_JS[close_start : BOOT_JS.find("\n}", close_start) + 2]
    assert "_releaseFocusFromClosedPanel(sidebar)" in close_body, (
        "closeMobileSidebar() must release focus from the parked sidebar; "
        f"got {close_body!r}"
    )


def test_release_helper_lands_focus_on_a_node_with_no_tab_stop():
    """Blur parks focus on <body>; assert the helper never targets anything that
    would re-introduce a focus stop of its own."""
    start = BOOT_JS.find("function _releaseFocusFromClosedPanel(")
    helper = BOOT_JS[start : BOOT_JS.find("\n}", start) + 2]
    assert ".focus(" not in helper, (
        "the helper must blur rather than move focus to a specific control — "
        f"inventing a landing spot re-creates the problem; got {helper!r}"
    )


# A desktop-width viewport: the band where closeMobileSidebar() runs but the
# sidebar stays laid out and visible.
_DESKTOP_BAND = {"width": 1280, "height": 800}


def _boot_functions_source() -> str:
    """The real production helpers under test: the phone-width predicate, the focus
    rescue and closeMobileSidebar(), lifted verbatim from boot.js. Sourcing them
    (rather than restating them) keeps the probe honest — it fails if boot.js
    changes its mind, instead of testing a copy that drifted away from it."""
    source: list[str] = []
    for name in (
        "function _isPhoneWidthViewport(",
        "function _releaseFocusFromClosedPanel(",
        "function closeMobileSidebar(",
    ):
        start = BOOT_JS.find(name)
        assert start != -1, f"{name} is missing from boot.js"
        source.append(BOOT_JS[start : BOOT_JS.find("\n}", start) + 2])
    return "\n".join(source)


@pytest.mark.parametrize(
    ("band", "expects_rescue"),
    [(_DESKTOP_BAND, False), ({"width": 390, "height": 780}, True)],
    ids=["1280px", "390px"],
)
def test_closed_sidebar_focus_rescue_is_guarded_to_the_phone_band(band, expects_rescue):
    """maintainer regression: closeMobileSidebar() runs on desktop too — opening a
    session calls it unconditionally — and at desktop width the sidebar is still
    laid out and visible, so releasing focus there would dump keyboard users on
    <body> on every session open. The rescue must fire only in the ≤640px band
    where the sidebar actually hides; a focused control elsewhere on the page
    (the session button) keeps its focus at desktop width."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size(band)
        page.set_content(
            f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>
  <button id="openSessionBtn">Session</button>
  <div class="mobile-overlay" id="mobileOverlay"></div>
  <aside class="sidebar" id="sidebar"><button id="btnNewChat">New</button></aside>
  <script>$ = (id) => document.getElementById(id);</script>
  <script>{_boot_functions_source()}</script>
</body></html>"""
        )
        # Park focus directly on the sidebar's own control — the real scenario:
        # the button that was in focus when the panel got closed.
        page.evaluate("document.getElementById('btnNewChat').focus()")
        page.evaluate("closeMobileSidebar()")
        focused = page.evaluate("() => document.activeElement && document.activeElement.id")
    finally:
        browser.close()
        playwright.stop()

    if expects_rescue:
        assert focused != "btnNewChat", (
            f"at {band['width']}px the closed sidebar is visibility:hidden; the "
            f"rescue must park focus off its now-invisible control (got {focused!r})"
        )
    else:
        assert focused == "btnNewChat", (
            f"at {band['width']}px the sidebar stays visible, so closeMobileSidebar() "
            f"must not steal focus from a focused control (got {focused!r})"
        )
