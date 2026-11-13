#!/usr/bin/env python3
"""Benchmark real live-turn SSE rendering with deterministic fixture events.

Run from a checkout with Playwright and Google Chrome installed:
  .venv/bin/python scripts/bench/live_render_scaling.py --mode compact_worklog

Starts an agent-free WebUI and a separate headless Chrome on isolated ports,
with disposable HOME/state/browser profiles. No provider requests or production
state are used. Timings are JS work, not end-to-end provider latency or painted
frames: headless mode does not certify screen paint/compositor cost.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
INIT = r"""
if ('serviceWorker' in navigator) navigator.serviceWorker.register =
  () => Promise.reject(new Error('disabled by render benchmark'));
window.fixtureSources = [];
window.__longtasks = [];
window.__longtaskStart = performance.now();
new PerformanceObserver(list => {
  for (const e of list.getEntries()) window.__longtasks.push(
    {start: e.startTime, duration: e.duration});
}).observe({type: 'longtask', buffered: true});
class FixtureEventSource {
  static OPEN=1; static CONNECTING=0; static CLOSED=2;
  constructor(url) { this.url=String(url); this.readyState=1;
    this.listeners={}; window.fixtureSources.push(this); }
  addEventListener(name,fn) {(this.listeners[name]||=[]).push(fn);}
  removeEventListener() {}
  close() {this.readyState=2;}
  emit(name,data,id) {for (const fn of this.listeners[name]||[])
    fn({data:JSON.stringify(data),lastEventId:String(id||'')});}
}
window.EventSource=FixtureEventSource;
// The app exports a frozen API; use a shallow mutable view solely so the bench
// can instrument its projection method before streaming starts.
let benchAnchorApi;
Object.defineProperty(window,'HermesAssistantTurnAnchors',{configurable:true,
  get:()=>benchAnchorApi,set:value=>{benchAnchorApi={...value};}});
"""
ATTRIBUTION = r"""
(() => {
  const state=window.__benchAttribution={current:null, original:{}, names:{}};
  const names=['_anchorSceneNodeForRow','_anchorSceneTransparentNodeForRow',
    '_renderAnchorSceneRowsIntoWorklog','_anchorSceneRowsForRendering',
    '_anchorSceneToolRenderSignature','_captureMessageScrollSnapshot',
    '_restoreMessageScrollSnapshotSameFrame','_restoreLiveAnchorScrollSnapshotAfterRebuild',
    '_captureWorklogDetailDisclosureState','_restoreWorklogDetailDisclosureState',
    '_dedupeLiveProcessedWorklogAnchors','renderLiveAnchorActivityScene'];
  function wrap(owner,name,label) {
    const original=owner&&owner[name];
    if(typeof original!=='function') return;
    state.original[label]=[owner,original];
    state.names[label]=0;
    owner[name]=function(...args) {
      const start=performance.now();
      try { return original.apply(this,args); }
      finally {
        const elapsed=performance.now()-start, sample=state.current;
        state.names[label]++;
        if(sample) {
          sample.attribution[label]=(sample.attribution[label]||0)+elapsed;
          if(label==='_anchorSceneNodeForRow'||label==='_anchorSceneTransparentNodeForRow')
            sample.rows_rebuilt++;
        }
      }
    };
  }
  for(const name of names) wrap(window,name,name);
  wrap(window.HermesAssistantTurnAnchors,'projectAssistantTurnAnchorActivityScene',
    'projectAssistantTurnAnchorActivityScene');
})();
"""
RUN = r"""async ({events, checkpoints}) => {
  const sid='render-bench-fixture', stream='render-bench-stream';
  await loadSession(sid);
  const deadline=performance.now()+10000;
  while (!fixtureSources.some(s => s.url.includes('api/chat/stream?'))) {
    if (performance.now()>deadline) throw new Error('no live fixture stream');
    await new Promise(r=>setTimeout(r,10));
  }
  const source=fixtureSources.findLast(s =>
    s.url.includes('api/chat/stream?') && s.readyState===1);
  if (!source) throw new Error('no open fixture source');
  const samples=[], rows=[];
  let current=null, depth=0;
  const originalRaf=window.requestAnimationFrame;
  window.requestAnimationFrame=function(callback) {
    return originalRaf.call(this,timestamp=>{
      const start=performance.now();
      try { return callback(timestamp); }
      finally { if (current!==null) samples[current].work.push(performance.now()-start); }
    });
  };
  const original=window._renderLiveAnchorActivitySceneForStream;
  if (typeof original!=='function') throw new Error('live renderer hook absent');
  window._renderLiveAnchorActivitySceneForStream=function(...args) {
    if (current===null) return original.apply(this,args);
    const start=performance.now(); depth++;
    try { return original.apply(this,args); }
    finally { depth--; if (depth===0)
      samples[current].renders.push(performance.now()-start); }
  };
  const median=values => {
    if (!values.length) return null;
    const sorted=[...values].sort((a,b)=>a-b), middle=sorted.length>>1;
    return sorted.length%2 ? sorted[middle] : (sorted[middle-1]+sorted[middle])/2;
  };
  const summarize=sampleSlice => {
    const rendered=sampleSlice.filter(s=>s.renders.length);
    const attribution={};
    for(const name of Object.keys(window.__benchAttribution.names)) {
      attribution[name]=median(rendered.map(s=>
        (s.attribution[name]||0)/s.renders.length));
    }
    return {median_per_render_ms:median(sampleSlice.flatMap(s=>s.renders)),
      render_count:sampleSlice.flatMap(s=>s.renders).length,
      rows_rebuilt_per_render:median(rendered.map(s=>s.rows_rebuilt/s.renders.length)),
      attribution_ms_per_render_inclusive:attribution,
      attribution_witnessed:rendered.length>0};
  };
  // Every cycle emits exactly four SSE events. Tool results are 2048 chars.
  const result='r'.repeat(2048);
  for (let i=1; i<=events; i++) {
    const phase=(i-1)%4, toolIndex=Math.floor((i-1)/4);
    let type, data;
    if (phase===0) {type='reasoning';data={text:'Checking fixture step '+toolIndex+'. '};}
    else if (phase===1) {type='tool';data={name:'terminal',tid:'bench-'+toolIndex,
      args:{command:'printf result-'+toolIndex},preview:'running '+toolIndex};}
    else if (phase===2) {type='tool_complete';data={name:'terminal',tid:'bench-'+toolIndex,
      preview:result,snippet:result,duration:0.01,is_error:false};}
    else {type='token';data={text:'Fixture progress '+toolIndex+'. '};}
    current=samples.length;
    samples.push({events:i,type,renders:[],work:[],attribution:{},rows_rebuilt:0});
    window.__benchAttribution.current=samples[current];
    const start=performance.now();
    source.emit(type,data,stream+':'+i);
    samples[current].work.push(performance.now()-start);
    // The production renderer throttles token/reasoning work to ~15 fps. Let
    // its actual queued rAF run while attribution still points at this event.
    await new Promise(r=>setTimeout(r,120));
    const sample=samples[current];
    sample.elapsed_ms=performance.now()-start;
    sample.work=[Math.max(sample.work.reduce((a,b)=>a+b,0),
      sample.renders.reduce((a,b)=>a+b,0))];
    current=null; window.__benchAttribution.current=null;
    if (checkpoints.includes(i)) {
      const accumulated=samples.flatMap(s=>s.renders);
      const accumulatedWork=samples.flatMap(s=>s.work);
      const now=performance.now(), windowStart=Math.max(0,now-5000);
      let busyMs=0;
      for (const task of window.__longtasks) {
        const end=task.start+task.duration;
        busyMs+=Math.max(0,Math.min(end,now)-Math.max(task.start,windowStart));
      }
      const span=Math.max(1,Math.min(5000,now-window.__longtaskStart));
      const previous=checkpoints[checkpoints.indexOf(i)-1]||0;
      const interval=samples.slice(previous);
      const finalWindow=interval.slice(-200);
      rows.push({events_so_far:i,
        median_per_render_ms:median(accumulated), render_count:accumulated.length,
        median_event_work_ms:median(accumulatedWork),
        p95_per_render_ms:(()=>{const a=accumulated.sort((x,y)=>x-y);
          return a.length?a[Math.min(a.length-1,Math.floor(a.length*.95))]:null;})(),
        dom_nodes:document.querySelectorAll('*').length,
        main_thread_busy_pct_last_5s:Math.min(100,busyMs*100/span),
        cumulative_by_event_type_ms:Object.fromEntries(
          ['reasoning','tool','tool_complete','token'].map(type=>[type,
            median(samples.filter(s=>s.type===type).flatMap(s=>s.work))])),
        latest_interval:{events_so_far:i,...summarize(interval)},
        final_200_events:{events_so_far:i,...summarize(finalWindow)},
        mode:chatActivityMode()});
    }
  }
  window._renderLiveAnchorActivitySceneForStream=original;
  window.requestAnimationFrame=originalRaf;
  return {mode:chatActivityMode(),rows,samples};
}"""


def free_port(low: int, high: int) -> int:
    for port in range(low, high + 1):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError(f"no free port in {low}-{high}")


def wait_url(url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited {proc.returncode}: {url}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(url)


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def fixture(route, session, sid, workspace):
    requested = route.request.url.split("session_id=", 1)[-1].split("&", 1)[0]
    data = session if requested == sid else {
        "session_id": "idle", "messages": [], "message_count": 0,
        "tool_calls": [], "workspace": workspace,
    }
    route.fulfill(json={"session": data})


def fixed_session(session, sid, workspace):
    return lambda route: fixture(route, session, sid, workspace)


def wait_boot(page) -> None:
    deadline = time.monotonic() + 30
    while not page.evaluate("typeof loadSession==='function' && S._bootReady===true"):
        if time.monotonic() > deadline:
            raise TimeoutError("WebUI boot")
        page.wait_for_timeout(50)


def markdown(results: list[dict]) -> str:
    lines = ["| Run | Events | Scene ms/render | Rows rebuilt/render | Top attribution inclusive ms/render |",
             "|---:|---:|---:|---:|---|"]
    for result in results:
        for row in result["rows"]:
            attrs = row["final_200_events"]["attribution_ms_per_render_inclusive"]
            top = sorted(attrs.items(), key=lambda item: item[1] or 0, reverse=True)[:4]
            detail = ", ".join(f"{name}={value:.3f}" for name, value in top)
            lines.append(f"| {result['run']} | {row['events_so_far']} | "
                f"{row['median_per_render_ms']:.3f} | "
                f"{row['final_200_events']['rows_rebuilt_per_render']:.3f} | {detail} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--events", default="200,1000,2000",
        help="comma-separated checkpoint event counts")
    parser.add_argument("--mode", choices=("compact_worklog", "transparent_stream"),
        default="compact_worklog")
    parser.add_argument("--out", "--output", type=Path)
    args = parser.parse_args()
    checkpoints = [int(value) for value in args.events.split(",")]
    if args.runs < 1 or not checkpoints or any(value < 1 for value in checkpoints):
        parser.error("--runs and every --events checkpoint must be positive")
    if checkpoints != sorted(set(checkpoints)):
        parser.error("--events checkpoints must be unique and ascending")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error("install Playwright in the selected Python environment")
    results: list[dict] = []
    server: subprocess.Popen | None = None
    chrome: subprocess.Popen | None = None
    chrome_profile = tempfile.mkdtemp(prefix="p0a-chrome.", dir="/tmp")
    with tempfile.TemporaryDirectory(prefix="hermes-render-bench-") as temp:
        root = Path(temp)
        for subdir in ("home", "agent", "logs", "state"):
            (root / subdir).mkdir()
        (root / "state/settings.json").write_text(json.dumps(
            {"chat_activity_display_mode": args.mode}), encoding="utf-8")
        log = (root / "logs/server.log").open("w")
        env = {key: os.environ.get(key, "") for key in ("PATH", "SYSTEMROOT", "TMPDIR")}
        env.pop("PYTHONPATH", None); env.pop("PYTHONSAFEPATH", None)
        env.update(HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
            HERMES_WEBUI_STATE_DIR=str(root / "state"), HERMES_CONFIG_PATH=str(root / "config.yaml"),
            HERMES_WEBUI_HOST="127.0.0.1", HERMES_WEBUI_PORT=str(free_port(18800, 18899)),
            HERMES_WEBUI_SKIP_ONBOARDING="1", HERMES_WEBUI_AGENT_DIR=str(root / "agent"),
            HERMES_WEBUI_DEFAULT_WORKSPACE=temp, NO_PROXY="127.0.0.1,localhost")
        base = f"http://127.0.0.1:{env['HERMES_WEBUI_PORT']}"
        server = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_url(base + "/health", server)
            debug_port = free_port(19400, 19499)
            chrome = subprocess.Popen([CHROME, "--headless=new",
                f"--remote-debugging-port={debug_port}", f"--user-data-dir={chrome_profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
                "--disable-background-networking", "--window-size=1440,900", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            wait_url(f"http://127.0.0.1:{debug_port}/json/version", chrome)
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
                context = browser.contexts[0]
                for run in range(1, args.runs + 1):
                    page = context.new_page()
                    page.add_init_script(INIT)
                    cdp = context.new_cdp_session(page)
                    cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                    page.goto(base, wait_until="load")
                    wait_boot(page)
                    actual_mode = page.evaluate("chatActivityMode()")
                    if actual_mode != args.mode:
                        raise RuntimeError(f"activity mode {actual_mode!r}, expected {args.mode!r}")
                    snapshot = {"stream_id": "render-bench-stream", "last_seq": 0,
                        "last_event_id": "", "messages": [], "tool_calls": [],
                        "last_assistant_text": "", "last_reasoning_text": "",
                        "anchor_activity_scene": {"version": "activity_scene_v1",
                            "identity": {"session_id": "render-bench-fixture",
                                "stream_id": "render-bench-stream", "run_id": "render-bench-stream"},
                            "activity_rows": []}}
                    session = {"session_id": "render-bench-fixture", "title": "Render benchmark",
                        "workspace": temp, "messages": [], "message_count": 0, "tool_calls": [],
                        "active_stream_id": "render-bench-stream", "pending_user_message": "Fixture",
                        "pending_started_at": time.time(), "runtime_journal_snapshot": snapshot}
                    page.route("**/api/session?*", fixed_session(session,
                        "render-bench-fixture", temp))
                    page.route("**/api/chat/stream/status?*", lambda route:
                        route.fulfill(json={"active": True}))
                    cdp.send("Runtime.evaluate", {"expression": ATTRIBUTION})
                    details = page.evaluate(RUN, {"events": checkpoints[-1], "checkpoints": checkpoints})
                    result = {"run": run, "mode": details["mode"], "rows": details["rows"]}
                    results.append(result if not args.out else {**result, "samples": details["samples"]})
                    print(f"\nRun {run}: activity_mode={result['mode']}\n" + markdown([result]))
                    page.close()
                browser.close()
        finally:
            stop(server); stop(chrome)
            log.close()
            shutil.rmtree(chrome_profile, ignore_errors=True)
    if args.out:
        args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        args.out.with_suffix(".md").write_text(markdown(results) + "\n", encoding="utf-8")
        print(f"\nRaw samples and summaries: {args.out}; markdown: {args.out.with_suffix('.md')}")
    print("Attribution is self-inclusive and measured over each checkpoint's final 200 events.")
    print("Headless timings measure main-thread JS/layout work, not painted-frame latency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
