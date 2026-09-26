#!/usr/bin/env python3
"""Browser gate: real Gateway activity journaled by WebUI, then mid-run reload.

No synthetic EventSource events or client-side scene mutation. The Gateway fixture
pauses before the final answer; this gate checks the server's JSONL before reload.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import (
    DeterministicGateway, FINAL_TEXT, PROMPT, REASONING_TEXT, TOOL_NAME,
    _capture_page_errors, _start_webui_server, _terminate_process,
    _wait_for_persisted_scene,
)
from browser_turn_worklog_lifecycle import snapshot

PROGRESS = 'Lifecycle progress: checking fixture data.'


def main():
    root = Path(__file__).resolve().parents[1]
    gateway = DeterministicGateway('turn-worklog')
    gateway.start()
    proc = log = browser = None
    with tempfile.TemporaryDirectory(prefix='hermes-worklog-reconnect-') as directory:
        state = Path(directory)
        workspace = state / 'workspace'
        workspace.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.endswith('_API_KEY') and key not in (
                   'API_SERVER_KEY', 'HERMES_WEBUI_PASSWORD',
                   'HERMES_WEBUI_EXTENSION_DIR', 'HERMES_WEBUI_EXTENSION_MANIFEST')}
        env.update(HOME=str(state), HERMES_HOME=str(state / 'hermes-home'),
                   HERMES_BASE_HOME=str(state / 'hermes-home'),
                   HERMES_CONFIG_PATH=str(state / 'hermes-home' / 'config.yaml'),
                   HERMES_WEBUI_STATE_DIR=str(state / 'webui-state'),
                   HERMES_WEBUI_TEST_STATE_DIR=str(state / 'test-state'),
                   HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
                   HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
                   HERMES_WEBUI_CHAT_BACKEND='gateway',
                   HERMES_WEBUI_GATEWAY_BASE_URL=gateway.base_url,
                   HERMES_WEBUI_GATEWAY_USE_RUNS_API='1', PYTHONPATH=str(root),
                   NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
        try:
            proc, log, _, url = _start_webui_server(root, env, state)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(base_url=url)
                page = context.new_page()
                errors = _capture_page_errors(page)
                page.goto('/', wait_until='domcontentloaded')
                page.wait_for_selector('#msg', state='visible')
                page.wait_for_function("() => typeof window._chatActivityDisplayMode === 'string'")
                page.evaluate("_pickChatActivityDisplayMode('turn_worklog')")
                page.locator('#msg').fill(PROMPT)
                page.locator('#btnSend').click()
                assert gateway.activity_ready.wait(20), gateway.request_body
                page.wait_for_function("() => !!document.querySelector('#liveAssistantTurn [data-anchor-row-role=thinking]')")
                gateway.release_tool.set()
                page.wait_for_function("() => !!document.querySelector('#liveAssistantTurn [data-anchor-row-role=tool]')")
                page.wait_for_function("text => !!Array.from(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role=prose]')).find(el => el.innerText.includes(text))", arg=PROGRESS)
                before = snapshot(page)
                sid = page.evaluate('S.session && S.session.session_id')
                assert sid and before['live'] and before['mode'] == 'turn_worklog', before
                assert not any(FINAL_TEXT in text for text in before['final']), before
                journal_files = list((state / 'webui-state').rglob('_run_journal/' + sid + '/*.jsonl'))
                assert len(journal_files) == 1, journal_files
                rows = [json.loads(line) for line in journal_files[0].read_text().splitlines()]
                names = [r.get('event') or r.get('event_type') for r in rows]
                assert any('reasoning' in str(n) for n in names), names
                assert any('tool' in str(n) for n in names), names
                interim = [r for r in rows if (r.get('event') or r.get('event_type')) == 'interim_assistant']
                assert len(interim) == 1 and interim[0]['payload']['text'] == PROGRESS, interim
                assert all(n not in ('done', 'stream_end') for n in names), names
                interim_id = interim[0]['event_id']
                def prose(scene):
                    return [r for r in scene['rows'] if r['role'] == 'prose' and r['id'] == interim_id]
                assert len(prose(before)) == 1 and prose(before)[0]['id'] == interim_id and PROGRESS in prose(before)[0]['text'], before
                assert prose(before)[0]['source'] == 'interim_assistant', before
                first_reasoning = next(r for r in rows if (r.get('event') or r.get('event_type')) == 'reasoning')
                journal_reasoning_id = first_reasoning['event_id']
                assert journal_reasoning_id in [r['id'] for r in before['rows'] if r['role'] == 'thinking'], (
                    journal_reasoning_id, before)
                # Reload while the Gateway remains held: the browser must reconstruct
                # interim activity from persisted server events, not the old DOM.
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function("() => !!document.querySelector('[data-anchor-row-role=tool]')")
                resumed = snapshot(page)
                expected = [(r['role'], r['id'], r['text']) for r in before['rows']]
                actual = [(r['role'], r['id'], r['text']) for r in resumed['rows']]
                assert actual == expected, (expected, actual)
                assert len(prose(resumed)) == 1 and prose(resumed)[0]['id'] == interim_id and prose(resumed)[0]['text'] == prose(before)[0]['text'], resumed
                assert any(REASONING_TEXT in r['text'] for r in resumed['rows']), resumed
                assert any(r['tool'] == TOOL_NAME for r in resumed['rows']), resumed
                gateway.release_settle.set()
                assert gateway.final_prefix_ready.wait(10)
                gateway.release_terminal.set()
                page.wait_for_function("() => !S.busy && !S.activeStreamId && !document.querySelector('#liveAssistantTurn')")
                _wait_for_persisted_scene(url, sid)
                if snapshot(page)['group']['closed']:
                    page.locator('[data-turn-worklog-group="1"] .tool-worklog-summary, [data-turn-worklog-group="1"] .tool-call-group-summary').click()
                settled = snapshot(page)
                assert settled['final'] == [FINAL_TEXT], settled
                assert len(prose(settled)) == 1 and prose(settled)[0]['id'] == interim_id and prose(settled)[0]['text'] == prose(before)[0]['text'], settled
                assert all(FINAL_TEXT not in r['text'] for r in settled['rows']), settled
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function("() => (document.querySelector('#msgInner') || {}).innerText.includes('Lifecycle gate final answer.')")
                if snapshot(page)['group']['closed']:
                    page.locator('[data-turn-worklog-group="1"] .tool-worklog-summary, [data-turn-worklog-group="1"] .tool-call-group-summary').click()
                restored = snapshot(page)
                assert restored['final'] == [FINAL_TEXT], restored
                assert len(prose(restored)) == 1 and prose(restored)[0]['id'] == interim_id and prose(restored)[0]['text'] == prose(before)[0]['text'], restored
                assert all(FINAL_TEXT not in r['text'] for r in restored['rows']), restored
                assert [r['role'] for r in restored['rows']] == [r['role'] for r in settled['rows']], (settled, restored)
                assert not errors, errors
                print('PASS journal-backed mid-run reload → settlement → reload')
                context.close()
                browser.close()
                browser = None
        finally:
            gateway.release_tool.set()
            gateway.release_settle.set()
            gateway.release_terminal.set()
            gateway.close()
            _terminate_process(proc)
            if log:
                log.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
