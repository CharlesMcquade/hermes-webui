#!/usr/bin/env python3
"""Isolated Chromium gate for opt-in Turn Worklog presentation and durability.

Uses the local Gateway fixture and temporary WebUI state from the public
conversation lifecycle gate; no provider or running service is involved.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from browser_conversation_lifecycle import (
    DeterministicGateway, FINAL_TEXT, PROMPT, REASONING_TEXT, TOOL_NAME,
    _capture_page_errors, _free_port, _get_json, _start_webui_server,
    _terminate_process, _wait_for_persisted_scene,
)

PROGRESS = "Lifecycle progress: checking fixture data."


def snapshot(page):
    return page.evaluate("""() => {
      const turn = document.querySelector('#liveAssistantTurn') ||
        Array.from(document.querySelectorAll('.assistant-turn')).pop();
      if (!turn) return {missing: true};
      const group = turn.querySelector('[data-anchor-scene-owner="1"]');
      const summary = group && group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
      const rows = Array.from(turn.querySelectorAll('[data-anchor-scene-row="1"]')).map(el => ({
        role: el.getAttribute('data-anchor-row-role'), text: el.innerText.trim(),
        source: el.getAttribute('data-anchor-source-event-type'),
        id: el.getAttribute('data-anchor-row-id'),
        tool: el.getAttribute('data-tool-name'),
        status: el.getAttribute('data-anchor-row-status'),
        classes: el.className,
        cardOpen: !!(el.querySelector('.thinking-card.open,.tool-card.open')),
        disclosure: Array.from(el.querySelectorAll('button,[aria-expanded],details')).map(node => ({
          tag: node.tagName, expanded: node.getAttribute('aria-expanded'),
          open: node.hasAttribute('open'), classes: node.className,
        })),
        hiddenDetails: Array.from(el.querySelectorAll('.thinking-body,.tool-card-details,.tool-card-body'))
          .map(node => ({hidden: node.hidden, display: getComputedStyle(node).display})),
      }));
      const final = Array.from(turn.querySelectorAll('.assistant-segment .msg-body'))
        .filter(el => !el.closest('[data-anchor-scene-owner="1"]') &&
          !el.closest('.assistant-segment').hidden &&
          getComputedStyle(el.closest('.assistant-segment')).display !== 'none' &&
          !el.closest('.assistant-segment').classList.contains('assistant-segment-worklog-source'))
        .map(el => el.innerText.trim());
      return {
        live: !!document.querySelector('#liveAssistantTurn'), mode: window._chatActivityDisplayMode,
        group: group ? {text: summary && summary.innerText.trim(),
          closed: group.classList.contains('tool-call-group-collapsed'),
          expanded: summary && summary.getAttribute('aria-expanded'),
          turnWorklog: group.getAttribute('data-turn-worklog-group'),
          index: Array.from(turn.querySelectorAll('*')).indexOf(group)} : null,
        rows, final,
        segments: Array.from(turn.querySelectorAll('.assistant-segment')).map(el => ({
          idx:el.getAttribute('data-msg-idx'), classes:el.className,
          hidden:el.hidden, text:el.innerText.trim(),
        })),
        sceneRows: (S.messages || []).filter(m => m && m._anchor_activity_scene).map(m => ({
          content: typeof m.content === 'string' ? m.content : '[non-text]',
          rows: m._anchor_activity_scene.activity_rows.map(r => ({role:r.role, source:r.source_event_type, text:r.text})),
        })),
      };
    }""")


def ordered_rows(data):
    return [row for row in data['rows'] if row['role'] in ('thinking', 'prose', 'tool', 'user')]


def assert_settled(page, *, expected_open=False):
    state = snapshot(page)
    assert not state['live'] and state['mode'] == 'turn_worklog', state
    assert state['group'] and state['group']['turnWorklog'] == '1', state
    assert state['group']['closed'] is (not expected_open), state
    assert state['group']['expanded'] == ('true' if expected_open else 'false'), state
    assert state['group']['text'].startswith('Worked for '), state
    assert state['final'] == [FINAL_TEXT], state
    if not expected_open:
        page.evaluate("""() => {
      const group = Array.from(document.querySelectorAll('[data-anchor-settled-scene-owner="1"]')).pop();
      const final = Array.from(group.closest('.assistant-turn').querySelectorAll('.assistant-segment'))
        .find(el => el.innerText.includes('Lifecycle gate final answer.'));
      if (!final || !(group.compareDocumentPosition(final) & Node.DOCUMENT_POSITION_FOLLOWING))
        throw new Error('closed worklog is not above visible final');
        group.querySelector('.tool-worklog-summary,.tool-call-group-summary').click();
        }""")
    page.wait_for_function("""() => !!document.querySelector('[data-anchor-settled-scene-owner="1"] [data-anchor-row-role="tool"]')""")
    opened = snapshot(page)
    assert opened['group']['expanded'] == 'true', opened
    assert any(row['tool'] == TOOL_NAME for row in opened['rows']), opened
    assert any(REASONING_TEXT in row['text'] for row in opened['rows']), opened
    assert any(PROGRESS in row['text'] for row in opened['rows']), opened
    assert all(not row['cardOpen'] for row in opened['rows'] if row['role'] in ('thinking', 'tool')), opened
    return opened


def capture_layout_evidence(page, label):
    evidence_dir = Path(tempfile.gettempdir()) / 'hermes-turn-worklog-evidence'
    evidence_dir.mkdir(exist_ok=True)
    for name, width, height in (('desktop', 1280, 900), ('narrow', 390, 844), ('mobile', 320, 640)):
        page.set_viewport_size({'width': width, 'height': height})
        if width <= 640:
            page.evaluate('closeMobileSidebar()')
            page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1")
        page.evaluate("() => document.querySelector('[data-anchor-scene-owner=\"1\"],.turn-worklog-live-row')?.scrollIntoView({block:'center'})")
        bounds = page.evaluate("""() => {
          const group = document.querySelector('[data-anchor-scene-owner="1"],.turn-worklog-live-row');
          const final = Array.from(document.querySelectorAll('.assistant-turn .assistant-segment:not([data-anchor-scene-row])')).pop();
          return [group, final].filter(Boolean).map(el => {
            const r = el.getBoundingClientRect();
            return {left: r.left, right: r.right, width: r.width};
          });
        }""")
        assert all(-1 <= item['left'] and item['right'] <= width + 1 for item in bounds), (name, bounds)
        assert page.evaluate("""() => {
          const el = document.querySelector('[data-anchor-scene-owner="1"],.turn-worklog-live-row');
          if (!el) return false;
          const r = el.getBoundingClientRect();
          const hit = document.elementFromPoint(Math.max(0, r.left + r.width / 2), Math.max(0, Math.min(innerHeight - 1, r.top + Math.min(r.height / 2, 30))));
          return el.contains(hit) || hit === el;
        }"""), f'{label}-{name}: worklog obstructed'
        path = evidence_dir / f'{label}-{name}.png'
        page.screenshot(path=str(path))
        print(f'EVIDENCE {path}')
    page.set_viewport_size({'width': 1280, 'height': 900})


def main(origin='turn_worklog', scenario='turn-worklog'):
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[1]
    gateway = DeterministicGateway(scenario)
    gateway.start()
    proc = log = browser = None
    with tempfile.TemporaryDirectory(prefix='hermes-turn-worklog-') as directory:
        state = Path(directory)
        agent = state / 'agent'
        agent.mkdir()
        (agent / 'run_agent.py').write_text('"""Gateway-only fixture."""\n')
        workspace = state / 'workspace'
        workspace.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.endswith('_API_KEY') and key not in (
                   'API_SERVER_KEY', 'HERMES_WEBUI_PASSWORD',
                   'HERMES_WEBUI_EXTENSION_DIR', 'HERMES_WEBUI_EXTENSION_MANIFEST')}
        env.update(HERMES_WEBUI_HOST='127.0.0.1',
                   HERMES_WEBUI_STATE_DIR=str(state / 'webui-state'),
                   HERMES_HOME=str(state / 'hermes-home'),
                   HERMES_BASE_HOME=str(state / 'hermes-home'),
                   HERMES_CONFIG_PATH=str(state / 'hermes-home' / 'config.yaml'),
                   HERMES_WEBUI_SKIP_ONBOARDING='1',
                   HERMES_WEBUI_AGENT_DIR=str(agent),
                   HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
                   HERMES_WEBUI_CHAT_BACKEND='gateway',
                   HERMES_WEBUI_GATEWAY_BASE_URL=gateway.base_url,
                   HERMES_WEBUI_GATEWAY_USE_RUNS_API='1',
                   PYTHONPATH=str(root), NO_PROXY='127.0.0.1,localhost',
                   no_proxy='127.0.0.1,localhost')
        try:
            proc, log, _, url = _start_webui_server(root, env, state)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(base_url=url)
                page = context.new_page()
                page.add_init_script("""(() => {
                  // The Gateway fixture owns its final answer; inject a real
                  // browser-side interim SSE event without pretending that a
                  // Gateway message.delta is a progress-only event.
                  const NativeEventSource = window.EventSource;
                  window.EventSource = class extends NativeEventSource {
                    constructor(...args) {
                      super(...args);
                      window.__turnWorklogTestSource = this;
                    }
                  };
                })();""")
                errors = _capture_page_errors(page)
                page.goto('/', wait_until='domcontentloaded')
                page.wait_for_selector('#msg', state='visible')
                page.evaluate("mode => _pickChatActivityDisplayMode(mode)", origin)
                page.wait_for_function("mode => window._chatActivityDisplayMode === mode", arg=origin)
                page.locator('#msg').fill(PROMPT)
                page.locator('#btnSend').click()
                assert gateway.activity_ready.wait(60), gateway.request_body
                page.wait_for_function("() => !!document.querySelector('#liveAssistantTurn [data-anchor-row-role=thinking]')")
                page.evaluate("""text => {
                  const source = window.__turnWorklogTestSource;
                  if (!source) throw new Error('browser SSE source not captured');
                  [text, text, text + ' — cross-checking'].forEach((update, index) => {
                    source.dispatchEvent(new MessageEvent('interim_assistant', {
                      data: JSON.stringify({text:update, event_id:'fixture-progress-' + index}),
                    }));
                  });
                }""", PROGRESS)
                page.wait_for_function("text => Array.from(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role=prose]')).some(el => el.innerText.includes(text))", arg=PROGRESS)
                page.evaluate("""() => {
                  window.__turnWorklogTestSource.dispatchEvent(new MessageEvent('steer_delivered', {
                    data: JSON.stringify({text:'Lifecycle steer delivered.', event_id:'fixture-steer-1'}),
                  }));
                }""")
                page.wait_for_function("() => !!document.querySelector('#liveAssistantTurn [data-anchor-row-role=user]')")
                gateway.release_tool.set()
                page.wait_for_function("() => !!document.querySelector('#liveAssistantTurn [data-anchor-row-role=tool]')")
                live = snapshot(page)
                assert live['mode'] == origin and live['live'], live
                if origin == 'turn_worklog':
                    assert not live['group'], live
                    assert any(REASONING_TEXT in row['text'] for row in live['rows']), live
                    assert sum(PROGRESS in row['text'] for row in live['rows'] if row['role'] == 'prose') == 3, live
                    assert any(row['tool'] == TOOL_NAME for row in live['rows']), live
                    assert any(PROGRESS in row['text'] for row in live['rows']), live
                    roles = [row['role'] for row in ordered_rows(live)]
                    if scenario == 'turn-worklog':
                        assert roles.index('thinking') < roles.index('prose') < roles.index('user') < roles.index('tool'), live
                    else:
                        # This shared error fixture signals activity_ready after
                        # its tool; the injected progress and Steer follow it.
                        assert roles.index('thinking') < roles.index('tool') < roles.index('user'), live
                    assert any(row['source'] == 'steer_delivered' and 'Lifecycle steer delivered.' in row['text'] for row in live['rows']), live
                    for row in live['rows']:
                        if row['role'] in ('thinking', 'tool'):
                            assert any(item['expanded'] == 'false' for item in row['disclosure']), live
                    assert all('running' not in (row['status'] or '') for row in live['rows'] if row['role'] == 'tool'), live
                    if scenario == 'turn-worklog':
                        capture_layout_evidence(page, 'turn-live')
                gateway.release_settle.set()
                assert gateway.final_prefix_ready.wait(10)
                gateway.release_terminal.set()
                page.wait_for_function("() => !S.busy && !S.activeStreamId && !document.querySelector('#liveAssistantTurn')")
                if scenario == 'terminal-error':
                    error_state = snapshot(page)
                    assert error_state['group'] and not error_state['group']['closed'], error_state
                    assert any(PROGRESS in row['text'] for row in error_state['rows']), error_state
                    assert any(row['source'] == 'steer_delivered' for row in error_state['rows']), error_state
                    assert error_state['final'] != [FINAL_TEXT], error_state
                    assert not errors, errors
                    page.reload(wait_until='domcontentloaded')
                    page.wait_for_function("() => !!document.querySelector('[data-turn-worklog-group=\"1\"]')")
                    restored = snapshot(page)
                    assert restored['group'] and not restored['group']['closed'], restored
                    assert any(PROGRESS in row['text'] for row in restored['rows']), restored
                    print('PASS isolated Turn Worklog error retains visible partial activity across reload')
                    context.close()
                    browser.close()
                    browser = None
                    return 0
                sid = page.evaluate('S.session && S.session.session_id')
                assert sid
                scene = _wait_for_persisted_scene(url, sid)
                assert scene['mode'] == origin, scene
                if origin == 'compact_worklog':
                    capture_layout_evidence(page, 'compact-before')
                    page.evaluate("_pickChatActivityDisplayMode('turn_worklog')")
                    page.wait_for_function("() => !!document.querySelector('[data-turn-worklog-group=\"1\"]')")
                    page.wait_for_function("() => !!document.querySelector('#settingsAppearanceAutosaveStatus.is-saved')")
                    assert page.evaluate("async () => (await (await fetch('/api/settings')).json()).chat_activity_display_mode") == 'turn_worklog'
                capture_layout_evidence(page, 'turn-after' if origin == 'compact_worklog' else 'turn-settled')
                settled = assert_settled(page)
                assert sum(PROGRESS in row['text'] for row in settled['rows'] if row['role'] == 'prose') == 3, settled
                if origin == 'turn_worklog':
                    assert [row['role'] for row in ordered_rows(settled)] == [
                        row['role'] for row in ordered_rows(live)
                    ], (live, settled)
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function("() => (document.querySelector('#msgInner') || {}).innerText.includes('Lifecycle gate final answer.')")
                reloaded = assert_settled(page, expected_open=True)
                assert [(row['role'], row['text']) for row in ordered_rows(settled)] == [
                    (row['role'], row['text']) for row in ordered_rows(reloaded)], (settled, reloaded)
                assert not errors, errors
                print(f'PASS isolated {origin} live → Turn Worklog settled → reload; closed disclosure expands')
                context.close()
                browser.close()
                browser = None
        finally:
            gateway.close()
            _terminate_process(proc)
            if log is not None:
                log.close()
    return 0


if __name__ == '__main__':
    for mode in ('turn_worklog', 'compact_worklog'):
        main(mode)
    main('turn_worklog', scenario='terminal-error')
    sys.exit(0)
