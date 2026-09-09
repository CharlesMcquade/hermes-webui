#!/usr/bin/env python3
"""Real renderer regression: unchanged activity must not rebuild on SSE prose.

Synthetic sessions/transport; no provider, credentials or production state.
Run with the repository test interpreter; BROWSERS defaults to chromium,webkit.
"""
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import _start_webui_server, _terminate_process
from browser_reconnect_scene_redraw import INIT, fixture, session_route

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix='webui-scene-energy-') as temp:
        state = Path(temp)
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TMPDIR') if k in os.environ}
        env.update(HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
                   HERMES_WEBUI_STATE_DIR=str(state/'webui'),
                   HERMES_CONFIG_PATH=str(state/'config.yaml'),
                   HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
                   HERMES_WEBUI_AGENT_DIR=str(state/'no-agent'))
        proc, log, _, base = _start_webui_server(ROOT, env, state)
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get('BROWSERS', 'chromium,webkit').split(','):
                    browser = getattr(pw, engine).launch(headless=True)
                    try:
                        for mode in ['compact_worklog', 'transparent_stream']:
                            context = browser.new_context(viewport={'width': int(os.environ.get('WIDTH', '390')), 'height': 844}, bypass_csp=True)
                            context.add_init_script(INIT)
                            page = context.new_page()
                            errors = []
                            page.on('pageerror', lambda e, errors=errors: errors.append(str(e)))
                            snapshot: Any = fixture(100)
                            rows = []
                            for i, tool in enumerate(snapshot['anchor_activity_scene']['activity_rows']):
                                rows.append(dict(row_id=f'thinking:{i}', local_id=f'thinking:{i}',
                                                 role='thinking', source_event_type='reasoning', status='completed',
                                                 text=f'Completed reasoning {i}. **Evidence matters.**', order_index=i*2))
                                rows.append(dict(tool, order_index=i*2+1))
                            snapshot['anchor_activity_scene']['activity_rows'] = rows
                            session = dict(session_id='fixture', title='Energy regression', model='',
                                           workspace=temp, messages=[], message_count=0, tool_calls=[],
                                           active_stream_id='run-fixture', pending_user_message='Inspect fixture',
                                           pending_started_at=time.time(), runtime_journal_snapshot=snapshot)
                            page.route('**/api/session?*', session_route(session, 'fixture', temp))
                            page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
                            page.goto(base, wait_until='load')
                            deadline = time.monotonic()+30
                            while not page.evaluate("typeof loadSession==='function' && S._bootReady===true"):
                                assert time.monotonic() < deadline, errors
                                page.wait_for_timeout(50)
                            page.evaluate("mode=>{window._chatActivityDisplayMode=mode;window._showThinking=true;window._simplifiedToolCalling=true}", mode)
                            page.evaluate("async()=>await loadSession('fixture')")
                            page.wait_for_timeout(500)
                            result = page.evaluate("""()=>{
                              const select=()=>Array.from(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="thinking"]'));
                              const before=select();
                              if(before.length!==100)throw new Error('missing mixed fixture');
                              const card=before[0].querySelector('.thinking-card');
                              card.classList.add('open');
                              const body=card.querySelector('.thinking-card-body');
                              const range=document.createRange();range.selectNodeContents(body);
                              const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
                              const selected=selection.toString();
                              let builds=0;const build=window._thinkingActivityNode;
                              window._thinkingActivityNode=function(...args){builds++;return build(...args)};
                              const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                              source.emit('tool',{name:'terminal',tid:'extra',args:{command:'printf sample'}},'run-fixture:2001');
                              source.emit('tool_complete',{name:'terminal',tid:'extra',snippet:'RESULT'},'run-fixture:2002');
                              if(builds!==0)throw new Error('unchanged completed thinking rebuilt '+builds+' times');
                              if(!before.every((n,i)=>n===select()[i]&&n.isConnected))throw new Error('thinking identity lost');
                              if(!selected||selection.toString()!==selected||!card.classList.contains('open'))throw new Error('thinking interaction lost');
                              selection.removeAllRanges();
                              const scene=JSON.parse(JSON.stringify(_projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode())));
                              scene.activity_rows=_anchorSceneRowsForRendering(scene,{settled:false});
                              const row=scene.activity_rows.find(r=>r.role==='thinking');
                              row.text='Corrected **reasoning**';
                              const paint=()=>renderLiveAnchorActivityScene('run-fixture',scene,{sessionId:'fixture'});
                              paint();
                              if(select()[0].querySelector('.thinking-card-body pre').textContent!==row.text)throw new Error('correction stale');
                              window._showThinking=false;paint();
                              if(select().length)throw new Error('hidden thinking retained');
                              window._showThinking=true;paint();
                              if(select().length!==100)throw new Error('thinking not restored');
                              // HTML snapshots intentionally lose node-owned signatures:
                              // rehydrate once, then retain the resulting node.
                              select()[0].outerHTML=select()[0].outerHTML;
                              builds=0;paint();
                              if(builds!==1)throw new Error('snapshot did not rehydrate exactly one row: '+builds);
                              builds=0;paint();if(builds!==0)throw new Error('rehydrated row not retained');
                              // Active reasoning must continue through the normal renderer.
                              row.status='running';row.text='Still thinking';paint();
                              row.text='Still thinking, with more evidence';paint();
                              if(!select()[0].textContent.includes('with more evidence'))throw new Error('active update lost');
                              row.status='completed';row.text='Corrected **reasoning**';paint();
                              if(select()[0].querySelector('.thinking-card-body pre').textContent!==row.text)throw new Error('completion reused a stale pre-active signature');
                              scene.activity_rows=scene.activity_rows.filter(r=>r!==row);paint();
                              if(select().length!==99)throw new Error('removed reasoning retained');
                              window._chatActivityDisplayMode=chatActivityMode()==='compact_worklog'?'transparent_stream':'compact_worklog';paint();
                              if(select().length!==99)throw new Error('mode switch duplicated reasoning');
                              return {retained:true,correction:true,visibility:true,rehydration:true,active:true};
                            }""")
                            print(json.dumps(dict(engine=engine,mode=mode,**result)),flush=True)
                            assert not errors, errors
                            context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
