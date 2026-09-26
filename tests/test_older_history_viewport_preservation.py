from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _body(src, start, end):
    return src[src.index(start):src.index(end, src.index(start))]


def test_loading_older_messages_expands_render_window_before_rendering():
    body = _body(SESSIONS_JS, "async function _loadOlderMessages()", "// Ensure the full message history")
    assert body.index("S.messages = nextMessages") < body.index("_messageRenderWindowSize=_currentMessageRenderWindowSize()") < body.index("renderMessages({preserveScroll:true, _prependAnchor:viewportAnchor, _ownedPrepend:true})")
    assert "if(typeof _messageIsRenderable==='function') return _messageIsRenderable(m);" in body
    assert "Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT)" in body


def test_loading_older_messages_preserves_viewport_without_bottom_snap():
    body = _body(SESSIONS_JS, "async function _loadOlderMessages()", "// Ensure the full message history")
    assert body.index("const viewportAnchor = container ? _messageWindowSnapshot() : null") < body.index("S.messages = nextMessages") < body.index("renderMessages({preserveScroll:true, _prependAnchor:viewportAnchor, _ownedPrepend:true})") < body.index("_scrollPinned = false")
    assert "const container = $('messages')" in body
    assert "container.scrollTop = newScrollH - prevScrollH" not in body
    render = UI_JS[UI_JS.index("function renderMessages(options)"):]
    assert "const windowAnchor=ownedWindow?((options&&options._prependAnchor)||_messageWindowSnapshot()):null" in render
    assert "_scrollAfterMessageRender(preserveScroll, scrollSnapshot)" in render


def test_loading_older_messages_owns_prepend_and_samples_after_fetch():
    body = _body(SESSIONS_JS, "async function _loadOlderMessages()", "// Ensure the full message history")
    assert body.index("const data = useBeforePaging") < body.index("const viewportAnchor = container ? _messageWindowSnapshot() : null")
    assert "_messagesGeneration !== startGeneration" in body
    assert "_ownedPrepend:true" in body
    assert "_messageWindowSnapshot()" in UI_JS
    assert "_messageWindowReader(entries)" in UI_JS


def test_owned_window_realigns_landmark_without_repinning():
    render = UI_JS[UI_JS.index("function renderMessages(options)"):]
    restore = _body(UI_JS, "function _restoreMessageWindowReader(target,anchor)", "let _messageWindowResizeObserver")
    assert render.index("const windowAnchor=ownedWindow?") < render.index("_commitMessageWindow(liveInner,inner,windowAnchor,windowOnly)")
    assert "_restoreMessageWindowReader(target,anchor)" in _body(UI_JS, "function _commitMessageWindow(", "function _initializeMessageWindowOwnership")
    assert restore.index("_programmaticScroll=true") < restore.index("container.scrollTop+=delta") < restore.index("_deferClearProgrammaticScroll()")
    assert "_scrollPinned=true" not in restore
