"""Fourth activity-mode settings contract (the older three-mode test predates it)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_fourth_mode_is_opt_in_and_server_validated():
    config = source("api/config.py")
    assert '"chat_activity_display_mode": "compact_worklog"' in config
    assert '"chat_activity_display_mode": {"compact_worklog", "turn_worklog", "transparent_stream", "hide_all_activity"}' in config


def test_picker_and_save_paths_keep_four_modes():
    html = source("static/index.html")
    boot = source("static/boot.js")
    panels = source("static/panels.js")
    for mode in ("compact_worklog", "turn_worklog", "transparent_stream", "hide_all_activity"):
        assert f'data-chat-activity-mode="{mode}"' in html
        assert f'<option value="{mode}"' in html
    assert html.count('class="chat-activity-mode-btn') == 4
    assert "s.chat_activity_display_mode==='turn_worklog'" in boot
    assert "window._transparentStream=window._chatActivityDisplayMode==='transparent_stream'" in boot
    assert "window._transparentStream=next==='transparent_stream'" in panels
    assert "chatActivityModeSel.value==='turn_worklog'" in panels
    assert "mode==='turn_worklog'" in panels
    assert "(($('settingsChatActivityDisplayMode')||{}).value==='turn_worklog')" in panels
    assert "_syncChatActivityDisplayModeControl(saved.chat_activity_display_mode)" in panels
    assert "_syncChatActivityDisplayModeControl(settings.chat_activity_display_mode)" in panels
    assert "_syncChatActivityDisplayModeControl(body.chat_activity_display_mode)" in panels


def test_four_options_fit_desktop_and_phone_and_locale_parity():
    css = source("static/style.css")
    i18n = source("static/i18n.js")
    assert "#mainSettings .chat-activity-mode-toggle{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert "#mainSettings .chat-activity-mode-toggle{grid-template-columns:1fr;}" in css
    assert i18n.count("settings_option_turn_worklog:") == 15
    assert i18n.count("worked_for_elapsed:") == 15
    assert ".turn-worklog-live-row" in css
    assert '.tool-worklog-group[data-turn-worklog-group="1"]' in css
