"""Regression coverage for PWA-backed browser notifications (#3196)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

DESKTOP_BACKGROUND_NOTIFICATION_NAMES = (
    "_desktopBackgroundedForNotifications",
    "__hermesSetBackgrounded",
    "_isBackgroundedForBrowserNotification",
)


def _source_between(start_marker: str, end_marker: str) -> str:
    start = MESSAGES_JS.index(start_marker)
    end = MESSAGES_JS.index(end_marker, start)
    return MESSAGES_JS[start:end]


def test_browser_notifications_use_service_worker_when_available():
    assert "function _showPwaNotification" in MESSAGES_JS
    assert "navigator.serviceWorker.ready" in MESSAGES_JS
    assert "reg.showNotification" in MESSAGES_JS
    assert "new Notification" in MESSAGES_JS
    assert "function sendBrowserNotification" in MESSAGES_JS


def test_notification_payload_uses_completion_session_when_provided():
    assert "function _notificationOptions" in MESSAGES_JS
    assert "const sid=(options&&options.sid)||(S&&S.session&&S.session.session_id);" in MESSAGES_JS
    assert "_sessionUrlForSid(sid)" in MESSAGES_JS
    assert "data:{url}" in MESSAGES_JS
    assert "tag:sid?`hermes-${sid}`" in MESSAGES_JS
    assert "function _completionNotificationPreviewText" in MESSAGES_JS
    assert "_completionNotificationPreviewText(lastAsst," in MESSAGES_JS
    assert "sendBrowserNotification('Response complete',_completionPreview||'Task finished',{forceHidden:_wasEverBackgrounded,sid:activeSid})" in MESSAGES_JS
    assert "assistantText?assistantText.slice(0,100)" not in MESSAGES_JS


def test_prompt_notifications_fire_from_card_renderer_chokepoint():
    """Approval/clarify notifications are owned by the card renderers, so EVERY
    surfacing path (live SSE, 1.5s fallback poll, post-respond 'next approval'
    refresh, reload-while-pending) notifies — not just the active-session SSE
    event. The SSE listeners no longer send notifications directly; the shared
    _notifyPromptCard gate dedupes per prompt id so repeated poll ticks and
    re-renders ping exactly once."""
    # The chokepoint helper exists and both card renderers call it BEFORE the
    # belongs-to-active-session guard (so a non-active session's prompt still
    # notifies).
    assert "function _notifyPromptCard(kind, sid, pending){" in MESSAGES_JS
    for fn_name, kind in (("showApprovalCard", "approval"), ("showClarifyCard", "clarify")):
        start = MESSAGES_JS.index(f"function {fn_name}(")
        body_start = MESSAGES_JS.index("{", start)
        depth = 0
        for i in range(body_start, len(MESSAGES_JS)):
            if MESSAGES_JS[i] == "{":
                depth += 1
            elif MESSAGES_JS[i] == "}":
                depth -= 1
                if depth == 0:
                    body = MESSAGES_JS[start : i + 1]
                    break
        assert "_notifyPromptCard(" in body, f"{fn_name} must route notifications through _notifyPromptCard"
    assert "_notifyPromptCard('approval', sid, pending);" in MESSAGES_JS
    assert "_notifyPromptCard('clarify', sid, pending);" in MESSAGES_JS
    # Dedupe gate: per prompt id, with TTL cleanup of stale keys.
    assert "const key = kind + ':' + id;" in MESSAGES_JS
    assert "if (_promptNotifySeen.has(key)) return;" in MESSAGES_JS
    assert "_PROMPT_NOTIFY_TTL_MS" in MESSAGES_JS
    # Visibility gate: suppress only while the tab is visible AND focused
    # (unlike the completion notice, an unfocused-but-visible window still
    # gets the ping because the run is blocked until the prompt is answered).
    assert "_isDocumentVisibleAndFocused()) return;" in MESSAGES_JS
    # SSE listeners delegate; they must not send directly (would bypass dedupe).
    approval_listener = _source_between(
        "source.addEventListener('approval',e=>{",
        "source.addEventListener('clarify',e=>{",
    )
    clarify_listener = _source_between(
        "source.addEventListener('clarify',e=>{",
        "source.addEventListener('state_saved',e=>{",
    )
    assert "sendBrowserNotification(" not in approval_listener
    assert "sendBrowserNotification(" not in clarify_listener
    assert "_notifyPromptCard()" in approval_listener  # ownership comment anchor
    assert "_notifyPromptCard()" in clarify_listener


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def _run_node(source: str) -> dict:
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is required for the executed notification-gate test")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run([node, str(script_path)], cwd=str(ROOT), capture_output=True, text=True, timeout=30)
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def test_notify_prompt_card_executed_gate_behavior():
    """Executed node-VM proof of _notifyPromptCard's runtime gates:
    (1) dedupes per prompt id — a second call for the same id is a no-op;
    (2) suppresses while the tab is visible+focused WITHOUT recording, so the
    same id still notifies once the tab is hidden (blocking prompt semantics);
    (3) notifies when hidden, with the right title/body and sid routing."""
    helper = _extract_fn(MESSAGES_JS, "_notifyPromptCard")
    vis = _extract_fn(MESSAGES_JS, "_isDocumentVisibleAndFocused")
    script = f"""
const document = {{ hidden: false, visibilityState: 'visible', hasFocus: () => true }};
const _promptNotifySeen = new Map();  // module-level state closed over by the helper
const _PROMPT_NOTIFY_TTL_MS = 600000; // same, for the stale-key cleanup pass
{vis}
{helper}
const sent = [];
function sendBrowserNotification(title, body, options) {{ sent.push({{ title, body, options }}); }}
const hiddenPending = {{ approval_id: 'a1', description: 'run the thing' }};
// Hidden tab: fires once...
document.hidden = true; document.visibilityState = 'hidden'; document.hasFocus = () => false;
_notifyPromptCard('approval', 'sid-1', hiddenPending);
_notifyPromptCard('approval', 'sid-1', hiddenPending); // dedupe: no second ping
// Same id after returning to a focused tab: dedupe still holds.
document.hidden = false; document.visibilityState = 'visible'; document.hasFocus = () => true;
_notifyPromptCard('approval', 'sid-1', hiddenPending);
// A DIFFERENT id while focused-visible: suppressed, NOT recorded...
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a2', description: 'second' }});
const afterFocused = sent.length; // checkpoint: must still be 1 (no ping while focused)
// ...so hiding the tab again pings it exactly once.
document.hidden = true; document.visibilityState = 'hidden'; document.hasFocus = () => false;
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a2', description: 'second' }});
const afterHidden = sent.length; // checkpoint: the suppressed id fires exactly once now
_notifyPromptCard('approval', 'sid-1', {{ approval_id: 'a2', description: 'second' }}); // dedupe
// Clarify shape routes its own title/body.
_notifyPromptCard('clarify', 'sid-1', {{ clarify_id: 'c1', question: 'Which one?' }});
process.stdout.write(JSON.stringify({{ sent, afterFocused, afterHidden }}));
"""
    result = _run_node(script)
    sent = result["sent"]
    # Mid-sequence checkpoints (a final count alone cannot distinguish
    # suppression from reordering — bite-check verified this exact gap).
    assert result["afterFocused"] == 1, (
        f"no ping may fire while the tab is visible+focused (got {result['afterFocused']} pings at checkpoint)"
    )
    assert result["afterHidden"] == 2, (
        f"the suppressed id must ping exactly once once the tab is hidden (got {result['afterHidden']} at checkpoint)"
    )
    assert len(sent) == 3, f"expected exactly 3 pings, got {len(sent)}: {sent}"
    assert sent[0]["title"] == "Approval required"
    assert sent[0]["body"] == "run the thing"
    assert sent[0]["options"] == {"sid": "sid-1"}
    assert sent[1]["body"] == "second"
    assert sent[2]["title"] == "Clarification needed"
    assert sent[2]["body"] == "Which one?"


def test_completion_notification_preview_uses_settled_message_not_live_prefix():
    """Background completion preview must not slice the live-stream accumulator."""
    assert "function _completionNotificationPreviewText" in MESSAGES_JS
    assert "String(msgContent(lastAssistantMessage)||'').trim()" in MESSAGES_JS
    assert "_assistantTurnAnchorSettledFinalAnswer" in MESSAGES_JS
    done_block = _source_between("source.addEventListener('done'", "source.addEventListener('stream_end'")
    assert "let lastAsst=null;" in done_block
    assert "d.session.messages" in done_block
    assert "liveDisplayText:typeof _streamDisplay==='function'?_streamDisplay():assistantText" in done_block


def test_completion_notification_fires_when_tab_was_hidden_during_stream():
    """#4416: a throttled background-tab SSE delivers `done` late (after the user
    returns, document.hidden=false), which silently dropped the completion
    notification. The done handler now passes forceHidden based on whether the
    tab was hidden at ANY point during the stream, and sendBrowserNotification
    bypasses ONLY the live visibility gate (not the user's enabled setting) on
    forceHidden — so a backgrounded stream notifies, a watched one stays silent."""
    # The per-stream hidden tracker exists and is wired at attach + done.
    assert "_STREAM_WAS_HIDDEN" in MESSAGES_JS
    assert "function _bindStreamHiddenTracker" in MESSAGES_JS
    # Entries are stream-owned ({streamId, wasHidden}) so a stale entry from a
    # non-`done` terminal path can't be mis-attributed to a later same-sid stream.
    assert "function _shouldForceCompletionNotification(sid, streamId){" in MESSAGES_JS
    assert "return wasHidden||wasBackgrounded;" in MESSAGES_JS
    assert "function _clearStreamHidden" in MESSAGES_JS
    assert "function _clearStreamNotificationBackground" in MESSAGES_JS
    # Done-path cleanup lives inside _shouldForceCompletionNotification(); the
    # activeSid call sites are the non-done terminal paths.
    assert "_clearStreamHidden(sid, streamId);" in MESSAGES_JS
    assert "_clearStreamNotificationBackground(sid, streamId);" in MESSAGES_JS
    assert MESSAGES_JS.count("_clearStreamHidden(activeSid, streamId)") >= 3
    assert MESSAGES_JS.count("_clearStreamNotificationBackground(activeSid, streamId)") >= 3
    # sendBrowserNotification honors forceHidden but still respects the
    # notifications-enabled setting (forceHidden is NOT the test-button force).
    assert "const forceHidden=!!(options&&options.forceHidden);" in MESSAGES_JS
    assert "if(!force&&!window._notificationsEnabled) return;" in MESSAGES_JS
    assert "function _isBackgroundedForBrowserNotification(){" in MESSAGES_JS
    assert "window.__hermesSetBackgrounded=(value)=>{" in MESSAGES_JS
    assert "if(!force&&!forceHidden&&!_isBackgroundedForBrowserNotification()) return;" in MESSAGES_JS


def test_desktop_background_notification_signal_stays_out_of_stream_visibility():
    stream_tracker = _source_between(
        "const LIVE_STREAMS={};",
        "function closeLiveStream(sessionId, streamId, source){",
    )
    deferred_recovery = _source_between(
        "function _reattachOrRestoreAfterDeferredStreamError(source){",
        "  // Bug A fix (#631):",
    )

    for name in DESKTOP_BACKGROUND_NOTIFICATION_NAMES:
        assert name not in stream_tracker
        assert name not in deferred_recovery


def test_service_worker_handles_notification_clicks_without_hijacking_other_sessions():
    assert "notificationclick" in SW_JS
    assert "event.notification.close()" in SW_JS
    assert "clients.matchAll" in SW_JS
    assert "clients.openWindow" in SW_JS
    # Match the open tab on pathname, not the full href (query/hash differ).
    assert "samePath(client.url)" in SW_JS
    assert "new URL(clientUrl).pathname === targetPath" in SW_JS
    assert "targetClient.focus()" in SW_JS
    exact_idx = SW_JS.index("targetClient.focus()")
    open_idx = SW_JS.index("self.clients.openWindow(targetUrl)")
    navigate_idx = SW_JS.index("focusableClient.navigate(targetUrl)")
    assert exact_idx < open_idx < navigate_idx


def test_settings_expose_permission_and_test_controls():
    assert "notificationPermissionStatus" in INDEX_HTML
    assert 'id="notificationPermissionButtonWrap"' in INDEX_HTML
    assert 'id="notificationPermissionButton"' in INDEX_HTML
    assert "requestNotificationPermission()" in INDEX_HTML
    assert "sendBrowserNotification('Hermes test'" in INDEX_HTML
    assert "{force:true}" in INDEX_HTML
    assert "function updateNotificationPermissionStatus" in PANELS_JS
    assert "const btn=$('notificationPermissionButton');" in PANELS_JS
    assert "const btnWrap=$('notificationPermissionButtonWrap');" in PANELS_JS
    assert "btn.disabled=granted;" in PANELS_JS
    assert "btn.title=granted?'':label;" in PANELS_JS
    assert "if(btnWrap) btnWrap.title=label;" in PANELS_JS
    assert "notifications_permission_status" in PANELS_JS
    assert "btn.setAttribute('aria-label', label);" in PANELS_JS
    assert "btn.setAttribute('aria-disabled', granted?'true':'false');" in PANELS_JS
    assert "btn.setAttribute('aria-disabled','true');" in PANELS_JS


def test_granted_permission_branch_is_not_silent():
    fn = MESSAGES_JS[
        MESSAGES_JS.index("function requestNotificationPermission(){") :
        MESSAGES_JS.index("function sendBrowserNotification(", MESSAGES_JS.index("function requestNotificationPermission(){"))
    ]
    assert "if(Notification.permission==='granted'){" in fn
    granted_branch = fn[
        fn.index("if(Notification.permission==='granted'){") :
        fn.index("if(Notification.permission==='denied'){")
    ]
    assert "updateNotificationPermissionStatus()" in granted_branch
    assert "showToast(t('notifications_enabled_toast'),3000)" in granted_branch
    assert "return Promise.resolve('granted');" in granted_branch


def test_notification_i18n_and_changelog_entries_exist():
    for key in [
        "notifications_enable_btn",
        "notifications_test_btn",
        "notifications_permission_status",
        "notifications_enabled_toast",
        "notifications_denied",
        "notifications_unsupported",
    ]:
        assert key in I18N_JS
    assert "PWA notifications now use the service worker" in CHANGELOG
    assert "#3196" in CHANGELOG
    entry = next(
        line for line in CHANGELOG.splitlines()
        if "Notification permission controls now reflect the real browser state" in line
    )
    assert entry.count("#4118") == 1
