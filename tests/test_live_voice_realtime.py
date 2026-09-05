"""Live voice (OpenAI Realtime WebRTC) — backend + frontend wiring tests."""

import io
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
VOICE_JS = (ROOT / "static" / "voice_live.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")

from api import voice_live


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)


class _FakeHandler:
    def __init__(self, body=None, command="POST", client=("1.2.3.4", 1)):
        self.command = command
        self.client_address = client
        self._body = json.dumps(body or {}).encode()
        self.headers = {"Content-Length": str(len(self._body)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(self._body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


def _last_json(handler):
    raw = handler.wfile.getvalue()
    return json.loads(raw.decode()) if raw else None


# ── backend validation ──────────────────────────────────────────────


def test_sdp_endpoint_rejects_get():
    h = _FakeHandler(command="GET")
    voice_live.handle_voice_live_sdp(h)
    assert h.status == 405


def test_sdp_endpoint_requires_offer(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "sk-test")
    h = _FakeHandler(body={"sdp": "not-an-sdp"})
    voice_live.handle_voice_live_sdp(h)
    assert h.status == 400


def test_sdp_endpoint_503_without_key(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "")
    monkeypatch.setattr(voice_live, "_rate_limited", lambda c: False)
    h = _FakeHandler(body={"sdp": "v=0\r\no=- 0 0 IN IP4 127.0.0.1"})
    voice_live.handle_voice_live_sdp(h)
    assert h.status == 503


def test_sdp_endpoint_rate_limited(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "sk-test")
    voice_live._RATE_LAST.clear()
    h1 = _FakeHandler(body={"sdp": "not-valid"})  # fails at sdp check before rate
    voice_live.handle_voice_live_sdp(h1)
    # two rapid valid-shaped requests: second must hit 429 (first fails later at network,
    # but consumes the rate slot first)
    def _fake_urlopen(*a, **k):
        raise OSError("no network in test")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    ha = _FakeHandler(body={"sdp": "v=0\r\ntest"})
    voice_live.handle_voice_live_sdp(ha)
    assert ha.status == 502
    hb = _FakeHandler(body={"sdp": "v=0\r\ntest"})
    voice_live.handle_voice_live_sdp(hb)
    assert hb.status == 429


def test_voice_allowlist_clamps():
    assert "marin" in voice_live.ALLOWED_VOICES
    assert voice_live.DEFAULT_VOICE in voice_live.ALLOWED_VOICES


def test_session_config_is_server_side():
    # The realtime session gets the orchestration tools, defined server-side.
    assert voice_live.ASK_VERITY_TOOL["name"] == "agent_ask"
    assert voice_live.AGENT_STATUS_TOOL["name"] == "agent_status"
    assert voice_live.AGENT_STEER_TOOL["name"] == "agent_steer"
    assert voice_live.AGENT_STOP_TOOL["name"] == "agent_stop"
    assert voice_live.WAIT_FOR_USER_TOOL["name"] == "wait_for_user"
    assert "agent_ask" in voice_live.VOICE_INSTRUCTIONS
    assert "agent_steer" in voice_live.VOICE_INSTRUCTIONS


def test_session_config_owns_turns_and_hardens_far_field_audio():
    config = voice_live._build_realtime_session_config("marin", "Earlier context")

    assert config["model"] == "gpt-realtime-2.1"
    assert config["reasoning"] == {"effort": "low"}
    assert config["parallel_tool_calls"] is True
    assert config["audio"]["input"]["noise_reduction"] == {"type": "far_field"}
    assert config["audio"]["input"]["transcription"]["model"] == "gpt-live-transcribe"
    assert config["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": False,
        "interrupt_response": False,
    }
    assert "Earlier context" in config["instructions"]
    assert [tool["name"] for tool in config["tools"]] == [
        "agent_ask",
        "agent_status",
        "agent_steer",
        "agent_stop",
        "wait_for_user",
    ]


def test_capability_endpoint(monkeypatch):
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "sk-test")
    h = _FakeHandler(command="GET")
    voice_live.handle_voice_live_capability(h)
    assert _last_json(h) == {"available": True}
    monkeypatch.setattr(voice_live, "_resolve_openai_key", lambda: "")
    h2 = _FakeHandler(command="GET")
    voice_live.handle_voice_live_capability(h2)
    assert _last_json(h2) == {"available": False}


# ── route wiring ─────────────────────────────────────────────────────


def test_routes_wired():
    assert '"/api/voice/live/sdp"' in ROUTES_PY
    assert '"/api/voice/live/capability"' in ROUTES_PY


# ── frontend wiring ─────────────────────────────────────────────────


def test_index_has_button_and_script():
    assert 'id="btnLiveVoice"' in INDEX_HTML
    assert "static/voice_live.js?v=__WEBUI_VERSION__" in INDEX_HTML


def test_voice_js_uses_backend_and_agent_bridge():
    assert "api/voice/live/sdp" in VOICE_JS
    assert "api/voice/live/capability" in VOICE_JS
    assert "agent_ask" in VOICE_JS
    assert "api/voice/live/ask" in VOICE_JS
    assert "api/voice/live/turn" in VOICE_JS
    assert "api/voice/live/connect" in VOICE_JS
    assert "api/voice/live/disconnect" in VOICE_JS
    assert "X-Hermes-CSRF-Token" in VOICE_JS
    # never touches OpenAI directly or embeds a key
    assert "api.openai.com" not in VOICE_JS
    assert "sk-" not in VOICE_JS


def test_voice_js_async_bridge_contract():
    # busy-guard: the bridge surfaces busy instead of spawning concurrent turns
    assert "data.busy" in VOICE_JS
    assert "api/voice/live/status" in VOICE_JS
    # mirrors spoken turns into the transcript
    assert "input_audio_transcription.completed" in VOICE_JS
    assert "_mirrorTurn" in VOICE_JS
    # orchestration surface: steer/status/stop handled client-side
    assert "api/voice/live/steer" in VOICE_JS
    assert "api/voice/live/status" in VOICE_JS
    assert "api/voice/live/stop" in VOICE_JS
    assert "agent_steer" in VOICE_JS
    assert "agent_stop" in VOICE_JS
    # YOLO lifecycle: connect enables, disconnect restores prior state
    assert "yolo_was_enabled" in VOICE_JS
    assert "_unbindSession" in VOICE_JS
    # digest flows to SDP mint for instruction enrichment
    assert "digest:_digest" in VOICE_JS


def test_routes_wired_v2():
    assert '"/api/voice/live/ask"' in ROUTES_PY
    assert '"/api/voice/live/turn"' in ROUTES_PY
    assert '"/api/voice/live/connect"' in ROUTES_PY
    assert '"/api/voice/live/disconnect"' in ROUTES_PY
    assert '"/api/voice/live/steer"' in ROUTES_PY
    assert '"/api/voice/live/status"' in ROUTES_PY
    assert '"/api/voice/live/stop"' in ROUTES_PY


# ── v2 backend unit tests ────────────────────────────────────────────


def _mk_session(tmp_path, monkeypatch, sid="voicetest01", msgs=None):
    """Register a real Session and patch routes' lookup to return it."""
    from api.models import Session
    s = Session(session_id=sid, messages=msgs or [])
    monkeypatch.setattr(
        "api.voice_live._require_webui_session",
        lambda handler, sid_arg: (s, None) if sid_arg == sid else (None, None),
        raising=True,
    )
    return s


def test_connect_enables_yolo_and_returns_digest(monkeypatch):
    from api import voice_live
    from api.route_approvals import disable_session_yolo, is_session_yolo_enabled

    sid = "voicetest02"
    disable_session_yolo(sid)
    from api.models import Session as _S
    sess = _S(session_id=sid, messages=[
        {"role": "user", "content": "hello there", "id": 1},
        {"role": "assistant", "content": "Hi! What can I do for you?", "id": 2},
    ])
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    h = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(h)
    out = _last_json(h)
    assert out["ok"] is True
    assert out["yolo_enabled"] is True
    assert out["yolo_was_enabled"] is False
    assert "hello there" in out["digest"]
    assert is_session_yolo_enabled(sid) is True
    # restore
    disable_session_yolo(sid)


def test_disconnect_restores_prior_yolo_state(monkeypatch):
    from api import voice_live
    from api.route_approvals import enable_session_yolo, disable_session_yolo, is_session_yolo_enabled

    sid = "voicetest03"
    disable_session_yolo(sid)
    from api.models import Session as _S
    sess = _S(session_id=sid)
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    # Simulate connect (server-side binding records prior state)
    h0 = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(h0)
    assert is_session_yolo_enabled(sid) is True
    # disconnect restores the pre-call state WITHOUT trusting client input
    h = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_disconnect(h)
    assert _last_json(h)["ok"] is True
    assert is_session_yolo_enabled(sid) is False
    # if YOLO was already on before the call, disconnect must NOT disable it
    enable_session_yolo(sid)
    h1 = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(h1)
    assert _last_json(h1)["yolo_was_enabled"] is True
    h2 = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_disconnect(h2)
    assert is_session_yolo_enabled(sid) is True
    # reconnect mid-call (page refresh): rebind must KEEP the recorded
    # pre-call state (False here), not re-read the flag we enabled ourselves
    disable_session_yolo(sid)
    voice_live.handle_voice_live_connect(_FakeHandler(body={"session_id": sid}))
    hb = _FakeHandler(body={"session_id": sid})
    voice_live.handle_voice_live_connect(hb)
    assert _last_json(hb)["yolo_was_enabled"] is False  # survived the rebind
    voice_live.handle_voice_live_disconnect(_FakeHandler(body={"session_id": sid}))
    assert is_session_yolo_enabled(sid) is False
    disable_session_yolo(sid)


def test_turn_mirror_appends_and_persists(monkeypatch, tmp_path):
    from api import voice_live
    from api.models import Session as _S

    sid = "voicetest04"
    sess = _S(session_id=sid, messages=[{"role": "user", "content": "earlier", "id": 1}])
    saved = []
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    monkeypatch.setattr(type(sess), "save", lambda self, **kw: saved.append(True))
    h = _FakeHandler(body={"session_id": sid, "user_text": "what time is it", "assistant_text": "Noon-ish"})
    voice_live.handle_voice_live_turn(h)
    out = _last_json(h)
    assert out["ok"] is True and out["appended"] == 2
    roles = [m["role"] for m in sess.messages]
    assert roles == ["user", "user", "assistant"]
    assert "[voice] what time is it" in sess.messages[1]["content"]
    assert saved == [True]


def test_turn_mirror_rejects_empty(monkeypatch):
    from api import voice_live
    from api.models import Session as _S

    sess = _S(session_id="voicetest05")
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))
    h = _FakeHandler(body={"session_id": "voicetest05"})
    voice_live.handle_voice_live_turn(h)
    assert h.status == 400


def test_digest_truncates_and_orders(monkeypatch):
    from api import voice_live
    from api.models import Session as _S

    msgs = [{"role": "user", "content": f"msg {i} " + "x" * 500, "id": i} for i in range(12)]
    sess = _S(session_id="voicetest06", messages=msgs, title="Test Chat")
    digest = voice_live._build_session_digest(sess)
    assert "Test Chat" in digest
    assert "msg 11" in digest       # recent turns included
    assert "msg 0" not in digest    # old turns dropped
    # each turn clipped to ~400 chars
    assert len([l for l in digest.splitlines() if l.startswith("- ")]) == 8


def test_agent_ask_returns_run_handle(monkeypatch):
    """The browser watches the exact returned run, not the session tail."""
    from api.models import Session as _S

    sid = "voicetest07"
    sess = _S(
        session_id=sid,
        messages=[
            {"role": "user", "content": "old request", "id": 1},
            {"role": "assistant", "content": "old answer", "id": 2},
        ],
    )
    monkeypatch.setattr(voice_live, "_require_webui_session", lambda h, x: (sess, None))

    def _fake_chat_start(proxy, body):
        proxy.send_response(200)
        proxy.wfile.write(json.dumps({"stream_id": "voice-stream-1"}).encode())

    monkeypatch.setattr("api.routes._handle_chat_start", _fake_chat_start)
    h = _FakeHandler(body={"session_id": sid, "question": "new request"})
    voice_live.handle_voice_live_ask(h)

    assert _last_json(h) == {
        "ok": True,
        "stream_id": "voice-stream-1",
        "session_id": sid,
    }


def _run_voice_js_scenario(scenario=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    script = textwrap.dedent(
        r"""
        const fs=require('fs');
        const vm=require('vm');
        const sent=[];
        const fetches=[];
        let audioConstraints=null;
        let dc=null;
        const delay=(ms)=>new Promise(resolve=>setTimeout(resolve,ms));
        const response=(status,data)=>({
          ok:status>=200&&status<300,
          status,
          json:async()=>data
        });

        global.window=global;
        global.window.addEventListener=()=>{};
        global.S={session:{session_id:'voicejs01'}};
        global.loadSession=()=>{};
        global.setBusy=()=>{};
        global.attachLiveStream=()=>{};
        global.document={
          readyState:'loading',
          getElementById:()=>null,
          addEventListener:()=>{},
          createElement:()=>({autoplay:false,playsInline:false,setAttribute(){},remove(){}})
        };
        Object.defineProperty(global,'navigator',{configurable:true,value:{
          mediaDevices:{getUserMedia:async(c)=>{
            audioConstraints=c;
            return {getTracks:()=>[{stop(){}}]};
          }},
          sendBeacon:()=>true
        }});
        global.fetch=async(url,opts={})=>{
          fetches.push({url:String(url),body:opts.body||null});
          if(String(url).includes('/connect')) return response(200,{ok:true,session_id:'voicejs01',yolo_enabled:true,yolo_was_enabled:false,digest:''});
          if(String(url).includes('/sdp')) return response(200,{ok:true,sdp:'v=0\r\nanswer'});
          if(String(url).includes('/ask')) return response(200,{ok:true,stream_id:'stream-1'});
          if(String(url).includes('/status')) return response(200,{ok:true,active:false});
          if(String(url).includes('/steer')) return response(200,{ok:true});
          if(String(url).includes('/stop')) return response(200,{ok:true,cancelled:true});
          if(String(url).includes('/turn')) return response(200,{ok:true});
          if(String(url).includes('/disconnect')) return response(200,{ok:true,yolo_enabled:false});
          return response(200,{ok:true});
        };
        class FakePC{
          constructor(){ this.connectionState='new'; this.ontrack=null; this.onconnectionstatechange=null; }
          createDataChannel(){
            dc={readyState:'open',onmessage:null,send:(raw)=>sent.push(JSON.parse(raw)),close(){this.readyState='closed';}};
            return dc;
          }
          addTrack(){}
          async createOffer(){ return {sdp:'v=0\r\noffer'}; }
          async setLocalDescription(){}
          async setRemoteDescription(){
            this.connectionState='connected';
            if(this.onconnectionstatechange) this.onconnectionstatechange();
          }
          close(){ this.connectionState='closed'; }
        }
        global.RTCPeerConnection=FakePC;

        vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'),{filename:'voice_live.js'});
        const emit=(msg)=>dc.onmessage({data:JSON.stringify(msg)});
        const count=(type)=>sent.filter(e=>e.type===type).length;

        (async()=>{
          window.toggleLiveVoice();
          await delay(25);

          emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u1',transcript:"Don't reply until I say Spider-Man."});
          emit({type:'input_audio_buffer.speech_started',item_id:'u2'});
          emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u2',transcript:'Someone else is still talking over there.'});
          await delay(10);
          const whileGated=count('response.create');
          emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u3',transcript:'Okay, Spiderman.'});
          await delay(10);
          const afterRelease=count('response.create');
          const firstCreate=sent.find(e=>e.type==='response.create');
          emit({type:'error',error:{event_id:firstCreate.event_id,message:'transient create failure'}});
          await delay(130);
          const afterCreateRetry=count('response.create');

          // A queued turn must wait until audible playback—not just model
          // generation—has finished.
          emit({type:'response.created',response:{id:'r0'}});
          emit({type:'output_audio_buffer.started',response_id:'r0'});
          emit({type:'response.done',response:{id:'r0',status:'completed',output:[{
            type:'message',role:'assistant',content:[{type:'audio',transcript:'Released.'}]
          }]}});
          emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u4',transcript:'One more thing.'});
          await delay(10);
          const whileAudioPlaying=count('response.create');
          emit({type:'output_audio_buffer.stopped',response_id:'r0'});
          await delay(10);
          const afterAudioStopped=count('response.create');

          emit({type:'response.created',response:{id:'r1'}});
          const callItem={type:'function_call',name:'agent_status',call_id:'c1',arguments:'{}'};
          emit({type:'response.output_item.done',response_id:'r1',item:callItem});
          emit({type:'response.done',response:{id:'r1',status:'completed',output:[callItem]}});
          await delay(20);
          const outputsAfterFirst=sent.filter(e=>e.type==='conversation.item.create'&&e.item&&e.item.call_id==='c1').length;
          const responsesAfterFirst=count('response.create');
          emit({type:'response.done',response:{id:'r1',status:'completed',output:[callItem]}});
          await delay(10);

          emit({type:'response.created',response:{id:'r2'}});
          const waitItem={type:'function_call',name:'wait_for_user',call_id:'c2',arguments:'{"reason":"side conversation"}'};
          emit({type:'response.done',response:{id:'r2',status:'completed',output:[waitItem]}});
          await delay(15);
          const responsesAfterWait=count('response.create');

          emit({type:'response.created',response:{id:'r3'}});
          const askItem={type:'function_call',name:'agent_ask',call_id:'c3',arguments:'{"question":"check something"}'};
          emit({type:'response.done',response:{id:'r3',status:'completed',output:[askItem]}});
          await delay(25);
          const askOutput=sent.find(e=>e.type==='conversation.item.create'&&e.item&&e.item.call_id==='c3');

          emit({type:'response.created',response:{id:'r4'}});
          const steerItem={type:'function_call',name:'agent_steer',call_id:'c4',arguments:'{"text":"only direct flights"}'};
          const stopItem={type:'function_call',name:'agent_stop',call_id:'c5',arguments:'{}'};
          emit({type:'response.done',response:{id:'r4',status:'completed',output:[steerItem,stopItem]}});
          await delay(25);
          const steerOutputs=sent.filter(e=>e.type==='conversation.item.create'&&e.item&&e.item.call_id==='c4').length;
          const stopOutputs=sent.filter(e=>e.type==='conversation.item.create'&&e.item&&e.item.call_id==='c5').length;
          window.stopLiveVoice(true);

          console.log(JSON.stringify({
            audioConstraints,
            whileGated,
            afterRelease,
            afterCreateRetry,
            whileAudioPlaying,
            afterAudioStopped,
            outputsAfterFirst,
            outputsAfterDuplicate:sent.filter(e=>e.type==='conversation.item.create'&&e.item&&e.item.call_id==='c1').length,
            responsesAfterFirst,
            responsesAfterWait,
            waitOutputs:sent.filter(e=>e.type==='conversation.item.create'&&e.item&&e.item.call_id==='c2').length,
            askResult:askOutput&&JSON.parse(askOutput.item.output),
            steerOutputs,
            stopOutputs,
            mirroredTurns:fetches
              .filter(f=>f.url.includes('/turn')&&f.body)
              .map(f=>JSON.parse(f.body)),
            cancelEvents:count('response.cancel'),
            clearEvents:count('output_audio_buffer.clear')
          }));
        })().catch(e=>{ console.error(e&&e.stack||e); process.exit(1); });
        """
    )
    if scenario is not None:
        script = script[:script.index("(async()=>{")] + "(async()=>{\n" + scenario + "\n})().catch(e=>{ console.error(e.stack||e); process.exit(1); });"
    proc = subprocess.run(
        [node, "-e", script, str(ROOT / "static" / "voice_live.js")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_voice_js_gates_replies_and_settles_each_tool_once():
    result = _run_voice_js_scenario()

    assert result["audioConstraints"]["audio"]["echoCancellation"] == {"ideal": True}
    assert result["audioConstraints"]["audio"]["noiseSuppression"] == {"ideal": True}
    assert result["audioConstraints"]["audio"]["autoGainControl"] == {"ideal": True}
    assert result["whileGated"] == 0
    assert result["afterRelease"] == 1
    assert result["afterCreateRetry"] == 2
    assert result["whileAudioPlaying"] == 2
    assert result["afterAudioStopped"] == 3
    assert result["outputsAfterFirst"] == 1
    assert result["outputsAfterDuplicate"] == 1
    assert result["responsesAfterFirst"] == 4
    assert result["responsesAfterWait"] == 4
    assert result["waitOutputs"] == 1
    assert result["askResult"]["ok"] is True
    assert result["askResult"]["stream_id"] == "stream-1"
    assert result["steerOutputs"] == 1
    assert result["stopOutputs"] == 1
    assert result["mirroredTurns"] == [
        {
            "session_id": "voicejs01",
            "user_text": "Okay, Spiderman.",
            "assistant_text": "Released.",
        }
    ]
    assert result["cancelEvents"] == 0
    assert result["clearEvents"] == 0


def test_voice_gate_orders_transcripts_and_stops_only_explicit_hold():
    result = _run_voice_js_scenario(r'''
      window.toggleLiveVoice(); await delay(25);
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u0',transcript:'Hello'});
      emit({type:'response.created',response:{id:'r0'}});
      emit({type:'output_audio_buffer.started',response_id:'r0'});
      emit({type:'input_audio_buffer.speech_started'});
      const ambientCancels=count('response.cancel');
      emit({type:'input_audio_buffer.committed',item_id:'u1'});
      emit({type:'input_audio_buffer.committed',item_id:'u2'});
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u2',transcript:'Still talking'});
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u1',transcript:'Do not reply until I say "Spider-Man". I need to think.'});
      const holdCancels=count('response.cancel');
      const holdClears=count('output_audio_buffer.clear');
      emit({type:'response.done',response:{id:'r0',status:'cancelled',output:[]}});
      emit({type:'output_audio_buffer.cleared',response_id:'r0'});
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u3',transcript:'Spidermania is not the release phrase'});
      const held=count('response.create');
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u4',transcript:'Spider Man'});
      const released=count('response.create');
      window.stopLiveVoice(true);
      console.log(JSON.stringify({ambientCancels,holdCancels,holdClears,held,released}));
    ''')
    assert result == {"ambientCancels": 0, "holdCancels": 1, "holdClears": 1, "held": 1, "released": 2}


def test_voice_tools_settle_before_new_reply_and_ignore_late_completion():
    result = _run_voice_js_scenario(r'''
      const baseFetch=fetch; let resolveStatus;
      global.fetch=(url,opts)=>String(url).includes('/status')
        ? new Promise(r=>{resolveStatus=r;}) : baseFetch(url,opts);
      window.toggleLiveVoice(); await delay(25);
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u0',transcript:'Status?'});
      emit({type:'response.created',response:{id:'r0'}});
      const item={type:'function_call',name:'agent_status',call_id:'c0',arguments:'{}'};
      emit({...item,type:'response.function_call_arguments.done',response_id:'r0'});
      emit({type:'response.output_item.done',response_id:'r0',item});
      emit({type:'response.done',response:{id:'r0',status:'completed',output:[item]}});
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u1',transcript:'Also this'});
      const whilePending=count('response.create');
      resolveStatus(response(200,{ok:true,active:false})); await delay(10);
      const settled=count('response.create');
      emit({type:'response.done',response:{id:'unrelated-old',status:'cancelled',output:[]}});
      emit({type:'response.done',response:{id:'unrelated-failed',status:'failed',output:[]}});
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u2',transcript:'And this'});
      const afterStale=count('response.create');
      const outputs=sent.filter(x=>x.item&&x.item.call_id==='c0').length;
      window.stopLiveVoice(true);
      console.log(JSON.stringify({whilePending,settled,afterStale,outputs}));
    ''')
    assert result == {"whilePending": 1, "settled": 2, "afterStale": 2, "outputs": 1}


def test_voice_reconnect_discards_pending_tool_result():
    result = _run_voice_js_scenario(r'''
      const baseFetch=fetch; let resolveAsk;
      global.fetch=(url,opts)=>String(url).includes('/ask')
        ? new Promise(r=>{resolveAsk=r;}) : baseFetch(url,opts);
      window.toggleLiveVoice(); await delay(25);
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u0',transcript:'Check a file'});
      emit({type:'response.created',response:{id:'r0'}});
      const item={type:'function_call',name:'agent_ask',call_id:'c0',arguments:'{"question":"check a file"}'};
      emit({type:'response.done',response:{id:'r0',status:'completed',output:[item]}});
      window.stopLiveVoice(true); window.toggleLiveVoice(); await delay(25);
      const before=sent.length;
      resolveAsk(response(200,{ok:true,stream_id:'old-run'})); await delay(15);
      const after=sent.length;
      const state=window._liveVoiceState();
      window.stopLiveVoice(true);
      console.log(JSON.stringify({before,after,state}));
    ''')
    assert result["before"] == result["after"]
    assert result["state"] == {"state": "live", "bound": "voicejs01", "busy": False}


def test_voice_reconnect_waits_for_late_microphone_and_stops_old_track():
    result = _run_voice_js_scenario(r'''
      let resolveMic; let captures=0; let stops=0;
      navigator.mediaDevices.getUserMedia=()=>{
        captures++;
        if(captures===1) return new Promise(r=>{resolveMic=r;});
        return Promise.resolve({getTracks:()=>[{stop(){stops++;}}]});
      };
      window.toggleLiveVoice(); await delay(10);
      window.stopLiveVoice(true); window.toggleLiveVoice(); await delay(10);
      const before=captures;
      resolveMic({getTracks:()=>[{stop(){stops++;}}]}); await delay(25);
      const state=window._liveVoiceState();
      const after=captures; const oldStops=stops;
      window.stopLiveVoice(true);
      console.log(JSON.stringify({before,after,oldStops,state}));
    ''')
    assert result["before"] == 1
    assert result["after"] == 2
    assert result["oldStops"] == 1
    assert result["state"]["state"] == "live"


@pytest.mark.parametrize("arguments", ["null", "[]", "{broken"])
def test_voice_bad_arguments_return_error_without_running_tools(arguments):
    result = _run_voice_js_scenario('''
      window.toggleLiveVoice(); await delay(25);
      emit({type:'response.created',response:{id:'r0'}});
      const item={type:'function_call',name:'agent_ask',call_id:'c0',arguments:%s};
      emit({type:'response.done',response:{id:'r0',status:'completed',output:[item]}});
      await delay(10);
      const calls=fetches.filter(x=>x.url.includes('/ask')).length;
      const outputs=sent.filter(x=>x.item&&x.item.call_id==='c0');
      window.stopLiveVoice(true);
      console.log(JSON.stringify({calls,outputs}));
    ''' % json.dumps(arguments))
    assert result["calls"] == 0
    assert len(result["outputs"]) == 1
    assert "JSON" in result["outputs"][0]["item"]["output"]


def test_voice_failed_response_exits_visibly_instead_of_staying_silent():
    result = _run_voice_js_scenario("""
      const notices=[];
      global.showToast=msg=>notices.push(msg);
      window.toggleLiveVoice(); await delay(20);
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u',transcript:'Hello'});
      emit({type:'response.created',response:{id:'r'}});
      emit({type:'response.done',response:{id:'r',status:'failed',status_details:{error:{code:'server_error'}},output:[]}});
      await delay(5);
      console.log(JSON.stringify({state:window._liveVoiceState().state,notices}));
    """)
    assert result["state"] == "off"
    assert any("failed" in msg.lower() for msg in result["notices"])


@pytest.mark.parametrize("trigger", ["user", "tool"])
def test_voice_rejected_response_retry_exits_visibly_without_repeating_tools(trigger):
    result = _run_voice_js_scenario("""
      const trigger=%s;
      const notices=[];
      global.showToast=msg=>notices.push(msg);
      window.toggleLiveVoice(); await delay(25);
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u',transcript:'Status?'});
      if(trigger==='tool'){
        emit({type:'response.created',response:{id:'r'}});
        const item={type:'function_call',name:'agent_status',call_id:'c',arguments:'{}'};
        emit({type:'response.done',response:{id:'r',status:'completed',output:[item]}});
        await delay(10);
      }
      const creates=()=>sent.filter(e=>e.type==='response.create');
      const beforeFailure=creates().length;
      emit({type:'error',error:{event_id:creates().at(-1).event_id,message:'create rejected'}});
      await delay(130);
      const afterRetry=creates().length;
      emit({type:'error',event_id:creates().at(-1).event_id,error:{message:'retry rejected'}});
      await delay(130);
      const state=window._liveVoiceState();
      const disconnects=fetches.filter(f=>f.url.includes('/disconnect')).length;
      const statusCalls=fetches.filter(f=>f.url.includes('/status')).length;
      const toolOutputs=sent.filter(e=>e.item&&e.item.call_id==='c').length;
      const cancels=fetches.filter(f=>f.url.includes('/stop')||f.url.includes('/cancel')).length;
      window.stopLiveVoice(true);
      console.log(JSON.stringify({beforeFailure,afterRetry,afterFailure:creates().length,
        state,notices,disconnects,statusCalls,toolOutputs,cancels}));
    """ % json.dumps(trigger))
    assert result["afterRetry"] == result["beforeFailure"] + 1
    assert result["afterFailure"] == result["afterRetry"]
    assert result["state"] == {"state": "off", "bound": None, "busy": False}
    assert result["disconnects"] == 1
    assert any("rejected" in msg.lower() and "reconnect" in msg.lower() for msg in result["notices"])
    assert result["statusCalls"] == (1 if trigger == "tool" else 0)
    assert result["toolOutputs"] == (1 if trigger == "tool" else 0)
    assert result["cancels"] == 0


def test_voice_refused_response_send_exits_visibly():
    result = _run_voice_js_scenario("""
      window.toggleLiveVoice(); await delay(20);
      dc.readyState='closed';
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'u',transcript:'Hello'});
      await delay(5);
      console.log(JSON.stringify({state:window._liveVoiceState().state}));
    """)
    assert result["state"] == "off"


def test_voice_background_run_result_is_run_scoped_and_respects_gate():
    result = _run_voice_js_scenario("""
      const originalFetch=global.fetch;
      let statusBody;
      global.fetch=async(url,opts={})=>{
        if(String(url).includes('/status')){
          statusBody=JSON.parse(opts.body);
          return response(200,{ok:true,terminal:true,terminal_state:'completed',result_available:true,answer:'Verified answer for this run'});
        }
        if(String(url).includes('api/session?')) throw Error('must not read session tail');
        return originalFetch(url,opts);
      };
      window.toggleLiveVoice(); await delay(20);
      emit({type:'response.created',response:{id:'r'}});
      emit({type:'response.done',response:{id:'r',status:'completed',output:[{type:'function_call',name:'agent_ask',call_id:'ask',arguments:'{"question":"Check status"}'}]}});
      await delay(10);
      emit({type:'response.created',response:{id:'ack'}});
      emit({type:'response.done',response:{id:'ack',status:'completed',output:[]}});
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'gate',transcript:"Don't reply until I say Spider-Man"});
      const before=count('response.create');
      await delay(1600);
      const whileHeld=count('response.create');
      emit({type:'conversation.item.input_audio_transcription.completed',item_id:'release',transcript:'Spiderman'});
      await delay(10);
      const notes=sent.filter(x=>x.type==='conversation.item.create'&&x.item.type==='message').map(x=>x.item.content[0].text);
      console.log(JSON.stringify({statusBody,before,whileHeld,after:count('response.create'),notes}));
    """)
    assert result["statusBody"] == {"session_id": "voicejs01", "stream_id": "stream-1"}
    assert result["whileHeld"] == result["before"]
    assert result["after"] == result["before"] + 1
    assert result["notes"] == ["[agent result]\nVerified answer for this run"]


def test_voice_stop_reuses_authoritative_chat_cancel(monkeypatch):
    from types import SimpleNamespace
    from api import voice_live, routes

    handler = _FakeHandler(body={"session_id": "stopvoice"})
    monkeypatch.setitem(routes.STREAMS, "the-stream", {})
    monkeypatch.setattr(voice_live, "_voice_binding_sid", lambda *args: (
        SimpleNamespace(session_id="stopvoice", active_stream_id="the-stream"), None
    ))
    calls = []
    def cancel(h, parsed):
        calls.append((h, parsed.path, parsed.query))
        return routes.j(h, {"cancelled": False, "persistence_failed": True})
    monkeypatch.setattr(routes, "handle_get", cancel)
    voice_live.handle_voice_live_stop(handler)
    assert calls == [(handler, "/api/chat/cancel", "stream_id=the-stream")]
    assert _last_json(handler) == {"cancelled": False, "persistence_failed": True}


def test_voice_stop_preserves_persistence_warning():
    result = _run_voice_js_scenario("""
      const originalFetch=global.fetch;
      global.fetch=async(url,opts)=>String(url).includes('/stop')
        ? response(200,{ok:true,cancelled:false,persistence_failed:true}) : originalFetch(url,opts);
      window.toggleLiveVoice(); await delay(20);
      emit({type:'response.created',response:{id:'stop'}});
      emit({type:'response.done',response:{id:'stop',status:'completed',output:[{type:'function_call',name:'agent_stop',call_id:'cstop',arguments:'{}'}]}});
      await delay(20);
      console.log(JSON.stringify({output:sent.find(x=>x.item&&x.item.call_id==='cstop').item.output}));
      window.stopLiveVoice(true);
    """)
    assert "persistence" in result["output"].lower()
    assert "Nothing to stop" not in result["output"]


def test_css_live_voice_rules():
    assert ".live-voice-btn.live-voice-active" in STYLE_CSS


def test_i18n_keys_all_locales():
    assert len(re.findall(r"live_voice_start:", I18N_JS)) == 15
    assert len(re.findall(r"live_voice_stop:", I18N_JS)) == 15
    assert len(re.findall(r"live_voice_connecting:", I18N_JS)) == 15
