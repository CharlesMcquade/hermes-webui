// Hermes WebUI — Live Voice (OpenAI Realtime over WebRTC)
// Thin voice front-end over the full Hermes agent. The realtime model has a
// single tool, ask_verity, which we execute against POST /api/chat (the
// synchronous agent endpoint) using the CURRENT session so voice exchanges
// land in the visible transcript.
(function(){
  'use strict';
  if(typeof window==='undefined') return;

  let _pc=null, _dc=null, _mic=null, _audioEl=null;
  let _state='off'; // off | connecting | live | error
  let _pendingCalls=0;

  function $(id){ return document.getElementById(id); }

  function _sid(){
    try{ return (window.S&&S.session&&(S.session.session_id||S.session.id))||null; }catch(_){ return null; }
  }

  function _setState(state, detail){
    _state=state;
    const btn=$('btnLiveVoice');
    if(btn){
      btn.classList.toggle('live-voice-active', state==='live'||state==='connecting');
      btn.classList.toggle('live-voice-connecting', state==='connecting');
      btn.classList.toggle('live-voice-thinking', state==='live'&&_pendingCalls>0);
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

  async function _askVerity(question){
    const sid=_sid();
    if(!sid) return 'No active session is open in the WebUI. Ask the user to open a chat session.';
    const headers={'Content-Type':'application/json'};
    try{
      const tok=window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.csrfToken;
      if(tok) headers['X-Hermes-CSRF-Token']=tok;
    }catch(_){ }
    try{
      const res=await fetch('api/chat',{
        method:'POST',
        headers,
        body:JSON.stringify({session_id:sid,message:question})
      });
      if(!res.ok){
        let msg='HTTP '+res.status;
        try{ const e=await res.json(); if(e&&e.error) msg=e.error; }catch(_){ }
        return 'The agent request failed: '+msg;
      }
      const data=await res.json();
      const answer=(data&&data.answer)||'';
      try{ if(typeof loadSession==='function') loadSession(sid,{preserveScroll:true}); }catch(_){ }
      return answer||'(the agent returned an empty answer)';
    }catch(e){
      return 'The agent request failed: '+(e&&e.message||'network error');
    }
  }

  function _sendEvent(obj){
    try{ if(_dc&&_dc.readyState==='open') _dc.send(JSON.stringify(obj)); }catch(_){ }
  }

  async function _onToolCall(name, callId, argsJson){
    let args={};
    try{ args=JSON.parse(argsJson||'{}'); }catch(_){ }
    let output='';
    if(name==='ask_verity'){
      _pendingCalls++; _setState(_state);
      try{ output=await _askVerity(String(args.question||'')); }
      finally{ _pendingCalls=Math.max(0,_pendingCalls-1); _setState(_state); }
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
      for(const item of msg.response.output){
        if(item&&item.type==='function_call'&&item.name&&item.call_id){
          _onToolCall(item.name,item.call_id,item.arguments);
        }
      }
    }else if(msg.type==='error'){
      console.warn('[live-voice] realtime error',msg);
    }
  }

  async function startLiveVoice(){
    if(_state==='connecting'||_state==='live') return;
    _setState('connecting');
    try{
      _mic=await navigator.mediaDevices.getUserMedia({audio:true});
    }catch(e){
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
      const headers={'Content-Type':'application/json'};
      try{
        const tok=window.__HERMES_CONFIG__&&window.__HERMES_CONFIG__.csrfToken;
        if(tok) headers['X-Hermes-CSRF-Token']=tok;
      }catch(_){ }
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
  window._liveVoiceState=function(){ return _state; };

  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',_initLiveVoiceButton);
  }else{
    _initLiveVoiceButton();
  }
})();
