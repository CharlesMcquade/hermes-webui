// Hermes WebUI — Live Voice v2 (OpenAI Realtime over WebRTC)
// Session-bound voice front-end. The realtime model gets a session digest,
// mirrors its spoken turns into the visible transcript, and drives the real
// agent through the non-blocking /api/voice/live/ask bridge (busy-guarded).
// Session YOLO is enabled for the call by /api/voice/live/connect and
// restored on disconnect.
(function(){
  'use strict';
  if(typeof window==='undefined') return;

  let _pc=null, _dc=null, _mic=null, _audioEl=null;
  let _state='off'; // off | connecting | live | error
  let _pendingCalls=0;
  let _boundSid=null;
  let _yoloWasEnabled=true; // conservative default: leave YOLO alone on cleanup
  let _activeAsk=null;      // {stream_id, started}
  let _lastUserTranscript='';

  function $(id){ return document.getElementById(id); }

  function _sid(){
    try{
      if(typeof S!=='undefined'&&S&&S.session) return S.session.session_id||S.session.id||null;
    }catch(_){ }
    return null;
  }

  function _csrf(){
    try{
      const tok=window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.csrfToken;
      return tok?{'X-Hermes-CSRF-Token':tok}:{};
    }catch(_){ return {}; }
  }

  function _setState(state, detail){
    _state=state;
    const btn=$('btnLiveVoice');
    if(btn){
      btn.classList.toggle('live-voice-active', state==='live'||state==='connecting');
      btn.classList.toggle('live-voice-connecting', state==='connecting');
      btn.classList.toggle('live-voice-thinking', state==='live'&&_pendingCalls>0);
      btn.classList.toggle('live-voice-yolo', state==='live'&&_yoloWasEnabled===false);
      const key = state==='live'?'live_voice_stop':(state==='connecting'?'live_voice_connecting':'live_voice_start');
      const label=(typeof t==='function'?t(key):null)||
        (state==='live'?'Stop live voice':(state==='connecting'?'Connecting…':'Live voice'));
      btn.setAttribute('data-tooltip',label);
      btn.setAttribute('aria-label',label);
      btn.setAttribute('aria-pressed', state==='live'||state==='connecting' ? 'true':'false');
    }
    if(state==='error'&&detail){
      try{ if(typeof showToast==='function') showToast(detail,4000,'error'); else console.warn('[live-voice]',detail); }catch(_){ console.warn('[live-voice]',detail); }
    }
  }

  // ── transcript mirroring ─────────────────────────────────────────────

  async function _mirrorTurn(userText, assistantText){
    if(!_boundSid||(!userText&&!assistantText)) return;
    try{
      await fetch('api/voice/live/turn',{
        method:'POST',
        headers:Object.assign({'Content-Type':'application/json'},_csrf()),
        body:JSON.stringify({session_id:_boundSid,user_text:userText||'',assistant_text:assistantText||''})
      });
      try{ if(typeof loadSession==='function') loadSession(_boundSid,{preserveScroll:true}); }catch(_){ }
    }catch(_){ }
  }

  // ── deep lane: non-blocking ask_verity via /api/voice/live/ask ───────

  async function _fetchFinalAnswer(streamId){
    // The stream just finished; the last assistant message in the session is
    // the answer. GET /api/session returns the full message array.
    try{
      const res=await fetch('api/session?session_id='+encodeURIComponent(_boundSid)+'&messages=1',{cache:'no-store'});
      if(!res.ok) return null;
      const data=await res.json();
      const msgs=(data&&(data.session?data.session.messages:data.messages))||[];
      for(let i=msgs.length-1;i>=0;i--){
        const m=msgs[i];
        if(m&&m.role==='assistant'&&(m.content||'').trim()) return String(m.content).trim();
      }
    }catch(_){ }
    return null;
  }

  // ── progress narration from the run journal ──────────────────────────

  function _describeTool(name){
    const n=String(name||'').toLowerCase();
    if(n.includes('terminal')) return 'running a terminal command';
    if(n.includes('web_search')||n==='search') return 'searching the web';
    if(n.includes('web_extract')) return 'reading a web page';
    if(n.includes('read_file')) return 'reading a file';
    if(n.includes('write_file')||n.includes('patch')) return 'editing files';
    if(n.includes('session_search')) return 'searching past conversations';
    if(n.includes('memory')) return 'checking memory';
    if(n.includes('execute_code')) return 'running some code';
    if(n) return 'using '+n.replace(/_/g,' ');
    return 'working';
  }

  function _narrate(text){
    if(!text) return;
    // Inject a short status as a system-ish context item so the realtime
    // model can mention it naturally without derailing its turn.
    _sendEvent({type:'conversation.item.create',item:{
      type:'message',role:'system',content:[{type:'input_text',text:'[voice progress] '+text}]
    }});
  }

  let _lastNarrSeq=0;
  async function _narrateProgress(streamId){
    // Poll the run journal for step events the agent already emits
    // (interim_assistant / tool.started / approval) and voice them once.
    try{
      const res=await fetch('api/session?session_id='+encodeURIComponent(_boundSid)+'&messages=0',{cache:'no-store'});
      if(!res.ok) return;
      const data=await res.json();
      const sess=data&&(data.session||data);
      const snap=sess&&sess.runtime_journal;
      if(!snap||snap.last_seq===undefined) return;
      // The live snapshot payload is embedded on the session GET; fall back to
      // the journal status endpoint for the coarse state.
      if(snap.last_seq>_lastNarrSeq){
        const le=String(snap.last_event||'');
        _lastNarrSeq=snap.last_seq;
        if(le==='tool') _narrate('using tools…');
        else if(le==='interim_assistant') _narrate('working on it…');
        else if(le==='approval') _narrate('an approval is waiting on screen');
      }
    }catch(_){ }
  }

  async function _pollAsk(streamId, startedAt){
    // Poll stream status until the run ends, then fetch the final answer.
    const maxMs=15*60*1000; // generous ceiling; user can hit Stop
    let narrAt=0;
    while(Date.now()-startedAt<maxMs){
      await new Promise(r=>setTimeout(r,1500));
      let st=null;
      try{
        const res=await fetch('api/chat/stream/status?stream_id='+encodeURIComponent(streamId),{cache:'no-store'});
        st=await res.json();
      }catch(_){ }
      if(!st){ continue; }
      if(st.active===false){
        return await _fetchFinalAnswer(streamId);
      }
      // Voice the agent's steps while it works (every ~6s at most).
      if(Date.now()-narrAt>6000){
        narrAt=Date.now();
        void _narrateProgress(streamId);
      }
    }
    return null; // timed out
  }

  async function _askVerity(question){
    const sid=_boundSid||_sid();
    if(!sid) return 'No active session is open in the WebUI. Ask the user to open a chat session.';
    try{
      const res=await fetch('api/voice/live/ask',{
        method:'POST',
        headers:Object.assign({'Content-Type':'application/json'},_csrf()),
        body:JSON.stringify({session_id:sid,question:question})
      });
      const data=await res.json().catch(()=>({}));
      if(data&&data.busy){
        return JSON.stringify({busy:true,message:data.message||'The agent is still working on the previous request.'});
      }
      if(!res.ok||!data||!data.stream_id){
        return 'The agent request failed: '+((data&&data.error)||('HTTP '+res.status));
      }
      _activeAsk={stream_id:data.stream_id,started:Date.now()};
      _pendingCalls++; _setState(_state);
      // Attach the app's own live renderer to this stream so tokens, tool
      // cards, and the worklog appear in the transcript in real time — the
      // same path a typed composer message takes.
      try{
        if(typeof attachLiveStream==='function'){
          attachLiveStream(_boundSid,data.stream_id,[]);
          try{
            const sess=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id===_boundSid)?S.session:null;
            if(sess) sess.active_stream_id=data.stream_id;
            if(typeof S!=='undefined') S.activeStreamId=data.stream_id;
            if(typeof setBusy==='function') setBusy(true);
          }catch(_){ }
        }
      }catch(_){ }
      try{
        const answer=await _pollAsk(data.stream_id,_activeAsk.started);
        return answer||'(the agent finished but returned no visible answer — it is in the chat transcript)';
      }finally{
        _activeAsk=null;
        _pendingCalls=Math.max(0,_pendingCalls-1); _setState(_state);
      }
    }catch(e){
      return 'The agent request failed: '+(e&&e.message||'network error');
    }
  }

  async function _cancelAsk(){
    const ask=_activeAsk;
    if(!ask) return false;
    try{
      await fetch('api/chat/cancel?stream_id='+encodeURIComponent(ask.stream_id),{method:'POST',headers:_csrf()});
    }catch(_){ }
    return true;
  }

  // ── realtime event handling ──────────────────────────────────────────

  function _sendEvent(obj){
    try{ if(_dc&&_dc.readyState==='open') _dc.send(JSON.stringify(obj)); }catch(_){ }
  }

  async function _onToolCall(name, callId, argsJson){
    let args={};
    try{ args=JSON.parse(argsJson||'{}'); }catch(_){ }
    let output='';
    if(name==='ask_verity'){
      try{ output=await _askVerity(String(args.question||'')); }
      catch(e){ output='ask_verity failed: '+(e&&e.message||e); }
    }else{
      output='Unknown tool: '+name;
    }
    // Realtime output item size safety: clip enormous answers for the voice
    // channel (full text is already in the transcript).
    if(output.length>16000) output=output.slice(0,16000)+'\n…(truncated for voice; full answer is in the chat transcript)';
    _sendEvent({type:'conversation.item.create',item:{type:'function_call_output',call_id:callId,output:output}});
    _sendEvent({type:'response.create'});
  }

  function _onDataChannelMessage(ev){
    let msg=null;
    try{ msg=JSON.parse(ev.data); }catch(_){ return; }
    if(!msg||!msg.type) return;
    if(msg.type==='response.done'&&msg.response&&Array.isArray(msg.response.output)){
      // Mirror the spoken assistant reply (collected from audio deltas) —
      // handled via response.output_text? gpt-realtime emits audio; the
      // transcript of what it SAID arrives via response.done output content
      // or the input_transcription events. We mirror what we can:
      let spoken='';
      try{
        for(const item of (msg.response.output||[])){
          if(item&&item.type==='message'&&item.role==='assistant'){
            const parts=(item.content||[]);
            for(const p of parts){
              if(p&&(p.transcript||p.text)) spoken+=((p.transcript||p.text)+' ');
            }
          }
          if(item&&item.type==='function_call'&&item.name&&item.call_id){
            _onToolCall(item.name,item.call_id,item.arguments);
          }
        }
      }catch(_){ }
      if(spoken.trim()){
        _mirrorTurn('', spoken.trim());
      }
    }else if(msg.type==='conversation.item.input_audio_transcription.completed'){
      const txt=(msg.transcript||'').trim();
      if(txt&&txt!==_lastUserTranscript){
        _lastUserTranscript=txt;
        _mirrorTurn(txt,'');
      }
    }else if(msg.type==='error'){
      console.warn('[live-voice] realtime error',msg);
    }
  }

  // ── connect / disconnect with session binding + YOLO ─────────────────

  async function _bindSession(){
    const sid=_sid();
    if(!sid) throw new Error('No active session — open a chat first');
    const res=await fetch('api/voice/live/connect',{
      method:'POST',
      headers:Object.assign({'Content-Type':'application/json'},_csrf()),
      body:JSON.stringify({session_id:sid})
    });
    const data=await res.json().catch(()=>({}));
    if(!res.ok||!data||!data.ok) throw new Error((data&&data.error)||('connect failed HTTP '+res.status));
    _boundSid=data.session_id;
    _yoloWasEnabled=(data.yolo_was_enabled!==false); // server says prior state
    return data; // {digest, ...}
  }

  async function _unbindSession(){
    if(!_boundSid) return;
    try{
      await fetch('api/voice/live/disconnect',{
        method:'POST',
        headers:Object.assign({'Content-Type':'application/json'},_csrf()),
        body:JSON.stringify({session_id:_boundSid,yolo_was_enabled:_yoloWasEnabled})
      });
    }catch(_){ }
    _boundSid=null;
  }

  async function startLiveVoice(){
    if(_state==='connecting'||_state==='live') return;
    _setState('connecting');
    try{
      const bind=await _bindSession();
      try{
        if(typeof showToast==='function') showToast(bind.yolo_enabled?'Live voice: full access enabled for this call':'Live voice connected',2500,'info');
      }catch(_){ }
    }catch(e){
      _setState('error','Live voice: '+(e&&e.message||e));
      _setState('off');
      return;
    }
    try{
      _mic=await navigator.mediaDevices.getUserMedia({audio:true});
    }catch(e){
      await _unbindSession();
      _setState('error','Microphone access denied');
      _setState('off');
      return;
    }
    try{
      _pc=new RTCPeerConnection();
      _audioEl=document.createElement('audio');
      _audioEl.autoplay=true;
      _pc.ontrack=(e)=>{ _audioEl.srcObject=e.streams[0]; };
      _pc.addTrack(_mic.getTracks()[0]);
      _dc=_pc.createDataChannel('oai-events');
      _dc.onmessage=_onDataChannelMessage;
      _pc.onconnectionstatechange=()=>{
        const st=_pc&&_pc.connectionState;
        if(st==='connected') _setState('live');
        else if(st==='failed'||st==='disconnected'||st==='closed'){
          if(_state!=='off') stopLiveVoice(true);
        }
      };
      const offer=await _pc.createOffer();
      await _pc.setLocalDescription(offer);
      const headers=Object.assign({'Content-Type':'application/json'},_csrf());
      const res=await fetch('api/voice/live/sdp',{
        method:'POST',headers,
        body:JSON.stringify({sdp:offer.sdp})
      });
      if(!res.ok){
        let msg='HTTP '+res.status;
        try{ const e=await res.json(); if(e&&e.error) msg=e.error; }catch(_){ }
        throw new Error(msg);
      }
      const data=await res.json();
      if(!data||!data.sdp) throw new Error('no SDP answer');
      await _pc.setRemoteDescription({type:'answer',sdp:data.sdp});
      // state flips to 'live' via onconnectionstatechange
    }catch(e){
      _setState('error','Live voice failed: '+(e&&e.message||e));
      stopLiveVoice(true);
    }
  }

  function stopLiveVoice(silent){
    try{ if(_dc){ _dc.onmessage=null; _dc.close(); } }catch(_){ }
    try{ if(_pc){ _pc.onconnectionstatechange=null; _pc.close(); } }catch(_){ }
    try{ if(_mic){ _mic.getTracks().forEach(tr=>{ try{tr.stop();}catch(_){}}); } }catch(_){ }
    try{ if(_audioEl){ _audioEl.srcObject=null; _audioEl.remove(); } }catch(_){ }
    _dc=null; _pc=null; _mic=null; _audioEl=null; _pendingCalls=0;
    _activeAsk=null;
    const sid=_boundSid;
    // Fire-and-forget cleanup; YOLO restore is idempotent server-side.
    if(sid){ _unbindSession(); }
    _lastUserTranscript='';
    _setState('off');
    if(!silent){
      try{ if(typeof showToast==='function') showToast('Live voice ended',2500,'info'); }catch(_){ }
    }
  }

  function toggleLiveVoice(){
    if(_state==='live'||_state==='connecting') stopLiveVoice();
    else startLiveVoice();
  }

  async function _initLiveVoiceButton(){
    const btn=$('btnLiveVoice');
    if(!btn) return;
    // Requires WebRTC + secure context (or localhost) for getUserMedia.
    if(!window.RTCPeerConnection||!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia) return;
    try{
      const res=await fetch('api/voice/live/capability',{cache:'no-store'});
      const cap=await res.json();
      if(!cap||!cap.available) return;
    }catch(_){ return; }
    btn.style.display='';
    btn.addEventListener('click',toggleLiveVoice);
  }

  window.toggleLiveVoice=toggleLiveVoice;
  window.stopLiveVoice=stopLiveVoice;
  window._liveVoiceState=function(){ return {state:_state,bound:_boundSid,busy:!!_activeAsk}; };

  // Orphan-proofing: if the page unloads mid-call (refresh, close, navigate),
  // sendBeacon still delivers the YOLO restore — it survives page teardown
  // where fetch() does not. The endpoint is CSRF-exempt and fail-safe (it can
  // only restrict privilege, never grant it). Server-side binding state is the
  // authoritative backup if even the beacon is lost.
  window.addEventListener('pagehide',function(){
    if(!_boundSid) return;
    try{
      const payload=JSON.stringify({session_id:_boundSid});
      if(navigator.sendBeacon){
        navigator.sendBeacon('api/voice/live/disconnect',new Blob([payload],{type:'application/json'}));
      }else{
        fetch('api/voice/live/disconnect',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json'},body:payload});
      }
    }catch(_){ }
  });

  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',_initLiveVoiceButton);
  }else{
    _initLiveVoiceButton();
  }
})();
