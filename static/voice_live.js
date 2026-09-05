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
  let _digest='';
  let _generation=0;
  let _replyGate=null;      // normalized release phrase, or null
  let _responseOpen=false;  // response.create sent until matching response.done
  const _playbackResponses=new Map(); // response ID -> whether playback has started
  const _drainedResponses=new Set();  // late events cannot resurrect old playback
  const _pendingContext=new Map();    // latest progress / final result per run
  let _speakingItem=null;
  let _activeResponseId=null;
  let _unbindPromise=Promise.resolve();
  let _connectPromise=Promise.resolve();
  let _settlingResponses=0;
  const _responseReasons=new Set();
  const _seenTranscriptItems=new Set();
  const _inputItems=[];
  const _transcripts=new Map();
  const _completedResponses=new Set();
  const _toolCalls=new Map(); // call_id -> Promise<{continueResponse:boolean}>
  let _pendingMirrorUsers=[];
  let _requestedMirrorUsers=[];
  let _deferredMirrorUsers=[];
  const _responseMirrorUsers=new Map(); // response_id -> triggering transcripts
  const _voiceRequests=new Set();

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
      btn.classList.toggle('live-voice-thinking', state==='live'&&(_pendingCalls>0||!!_activeAsk));
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

  async function _mirrorTurn(userText, assistantText, sid){
    const targetSid=sid||_boundSid;
    if(!targetSid||(!userText&&!assistantText)) return;
    try{
      const res=await fetch('api/voice/live/turn',{
        method:'POST',
        headers:Object.assign({'Content-Type':'application/json'},_csrf()),
        body:JSON.stringify({session_id:targetSid,user_text:userText||'',assistant_text:assistantText||''})
      });
      if(!res.ok) return;
      try{ if(_sid()===targetSid&&typeof loadSession==='function') loadSession(targetSid,{preserveScroll:true}); }catch(_){ }
    }catch(_){ }
  }

  // ── deep lane: non-blocking ask_verity via /api/voice/live/ask ───────


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

  function _narrate(text, streamId){
    if(!text) return;
    // Progress is context for the next reply, not a new turn every six seconds.
    // Coalesce it locally: never rewrite the conversation beneath active speech.
    _pendingContext.set('progress:'+streamId,'[voice progress] '+String(text).slice(0,2000));
  }

  async function _pollAsk(ask){
    const {stream_id:streamId, started:startedAt, sid, generation}=ask;
    // The exact run's immutable done journal owns its answer, never the
    // mutable last message of a session which may already have another run.
    const maxMs=15*60*1000; // generous ceiling; user can hit Stop
    let narrAt=0;
    let consecutiveErrors=0;
    let lastNarration='';
    while(generation===_generation&&Date.now()-startedAt<maxMs){
      await new Promise(r=>setTimeout(r,1500));
      if(generation!==_generation||ask.cancelled) return null;
      let st=null;
      try{
        const result=await _voicePost('api/voice/live/status',{session_id:sid,stream_id:streamId});
        if(result.status<400&&result.data.ok) st=result.data;
      }catch(_){ }
      if(generation!==_generation||ask.cancelled) return null;
      if(!st){
        consecutiveErrors++;
        if(consecutiveErrors>=5){
          return {completed:false,error:'I lost the live status connection. The Hermes run may still be working in chat.'};
        }
        continue;
      }
      consecutiveErrors=0;
      if(st.terminal){
        return st.result_available&&typeof st.answer==='string'
          ? {completed:true,answer:(st.terminal_state==='completed'?'':'Run ended with '+String(st.terminal_state)+'; this result may be partial.\n')+st.answer}
          : {completed:true,error:'The Hermes run ended ('+String(st.terminal_state||'unknown')+') without a verified final answer. Check the chat transcript.'};
      }
      // Voice the agent's steps while it works (every ~6s at most).
      if(Date.now()-narrAt>6000){
        narrAt=Date.now();
        const progress=st.latest_step||((st.recent_tools||[]).length?_describeTool(st.recent_tools[st.recent_tools.length-1]):'');
        if(progress&&progress!==lastNarration){
          lastNarration=progress;
          _narrate(progress, streamId);
        }
      }
    }
    if(generation!==_generation) return null;
    return {completed:false,error:'The Hermes run is still active after the voice wait limit. Check the chat transcript for progress.'};
  }

  // ── deep lane: non-blocking agent_ask via /api/voice/live/ask ────────

  async function _voicePost(path, body){
    const controller=new AbortController();
    _voiceRequests.add(controller);
    const timer=setTimeout(()=>controller.abort(),30000);
    try{
      const res=await fetch(path,{
        method:'POST',signal:controller.signal,
        headers:Object.assign({'Content-Type':'application/json'},_csrf()),
        body:JSON.stringify(Object.assign({session_id:_boundSid||_sid()},body||{}))
      });
      const data=await res.json().catch(()=>({}));
      return {status:res.status,data:data||{}};
    }finally{
      clearTimeout(timer);
      _voiceRequests.delete(controller);
    }
  }

  async function _agentStatus(){
    const {data}=await _voicePost('api/voice/live/status',{});
    if(!data.ok) return 'Status unavailable.';
    if(!data.active) return 'No run is active right now.';
    const parts=[];
    if(data.latest_step) parts.push('Current step: '+data.latest_step);
    if(Array.isArray(data.recent_tools)&&data.recent_tools.length) parts.push('Recent tools: '+data.recent_tools.join(', '));
    return parts.length?parts.join('. '):'The run is active and working.';
  }

  async function _agentSteer(text){
    const {data}=await _voicePost('api/voice/live/steer',{text:String(text||'')});
    if(data.ok) return 'Delivered to the active run. Application is not yet confirmed.';
    return 'Could not steer: '+(data.message||data.reason||'no active run');
  }

  async function _agentStop(){
    const generation=_generation;
    const ask=_activeAsk;
    const {data}=await _voicePost('api/voice/live/stop',{});
    if((data.persistence_failed||(data.ok&&data.cancelled))&&
       generation===_generation&&ask===_activeAsk&&ask){
      ask.cancelled=true;
      _pendingContext.delete('progress:'+ask.stream_id);
    }
    if(data.persistence_failed){
      return 'The stop encountered a persistence failure. Do not claim the final state was saved; check the chat for the recovery warning.';
    }
    if(data.ok&&data.cancelled){
      return 'Stopped. The run is cancelled.';
    }
    if(!data.ok) return 'Could not stop the run: '+(data.error||'stop failed');
    return 'Nothing to stop — no run is active.';
  }

  async function _watchAsk(ask){
    try{
      const result=await _pollAsk(ask);
      if(ask.generation!==_generation||ask.cancelled||!result) return;
      const rawText=String(result.answer||result.error||'');
      const text=rawText.length>16000
        ? rawText.slice(0,16000)+'\n…(truncated for voice; full answer is in the chat transcript)'
        : rawText;
      _pendingContext.delete('progress:'+ask.stream_id);
      _pendingContext.set('result:'+ask.stream_id,'[agent result]\n'+text);
      _queueResponse('agent-result');
    }finally{
      if(_activeAsk===ask&&ask.generation===_generation){
        _activeAsk=null;
        _setState(_state);
      }
    }
  }

  async function _askVerity(question){
    const sid=_boundSid||_sid();
    const generation=_generation;
    if(!sid) return 'No active session is open in the WebUI. Ask the user to open a chat session.';
    if(!String(question||'').trim()) return 'The agent request failed: question required.';
    try{
      const {status,data}=await _voicePost('api/voice/live/ask',{session_id:sid,question:question});
      if(generation!==_generation) return 'Voice disconnected. Any accepted run remains in chat.';
      if(data&&data.busy){
        return JSON.stringify({busy:true,message:data.message||'The agent is still working on the previous request.'});
      }
      if(status>=400||!data||!data.stream_id){
        return 'The agent request failed: '+((data&&data.error)||('HTTP '+status));
      }
      const ask={
        stream_id:data.stream_id,
        started:Date.now(),
        sid,
        generation,
        cancelled:false
      };
      _activeAsk=ask;
      _setState(_state);
      // Attach the app's own live renderer to this stream so tokens, tool
      // cards, and the worklog appear in the transcript in real time — the
      // same path a typed composer message takes.
      try{
        if(typeof attachLiveStream==='function'&&generation===_generation&&_sid()===sid){
          attachLiveStream(sid,data.stream_id,[]);
          try{
            const sess=(typeof S!=='undefined'&&S&&S.session&&S.session.session_id===sid)?S.session:null;
            if(sess) sess.active_stream_id=data.stream_id;
            if(typeof S!=='undefined') S.activeStreamId=data.stream_id;
            if(typeof setBusy==='function') setBusy(true);
          }catch(_){ }
        }
      }catch(_){ }
      void _watchAsk(ask);
      return JSON.stringify({
        ok:true,
        stream_id:data.stream_id,
        message:'The Hermes run started. Continue the conversation while it works.'
      });
    }catch(e){
      return 'The request outcome is unconfirmed: '+(e&&e.message||'network error')+'. Check agent_status and chat before repeating the action.';
    }
  }

  // ── realtime event handling ──────────────────────────────────────────

  function _sendEvent(obj){
    try{
      if(_dc&&_dc.readyState==='open'){
        _dc.send(JSON.stringify(obj));
        return true;
      }
    }catch(_){ }
    return false;
  }

  function _normalizeSpeech(text){
    return String(text||'')
      .normalize('NFKC')
      .toLocaleLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu,' ')
      .trim()
      .replace(/\s+/g,' ');
  }

  function _extractReplyGate(text){
    const raw=String(text||'').replace(/[’‘]/g,"'").trim();
    const patterns=[
      /\b(?:do not|don't|dont|please do not|please don't)\s+(?:reply|respond|answer|speak|talk|say anything)(?:\s+to me)?\s+(?:again\s+)?(?:until|unless)\s+(?:i\s+)?(?:say|tell you)\s+(.+)$/iu,
      /\b(?:stay|keep|be)\s+(?:silent|quiet)\s+(?:until|unless)\s+(?:i\s+)?(?:say|tell you)\s+(.+)$/iu,
      /\bwait\s+(?:to\s+)?(?:reply|respond|answer|speak|talk)\s+until\s+(?:i\s+)?(?:say|tell you)\s+(.+)$/iu
    ];
    for(const pattern of patterns){
      const match=raw.match(pattern);
      if(!match) continue;
      const quoted=match[1].match(/^["“]([^"”]+)["”]/u);
      const phrase=(quoted?quoted[1]:match[1].split(/[.!?]/u)[0])
        .replace(/^[\s"'“”‘’]+|[\s"'“”‘’.,!?;:]+$/gu,'');
      const normalized=_normalizeSpeech(phrase);
      if(normalized&&normalized.length<=80&&normalized.split(' ').length<=8) return normalized;
    }
    return null;
  }

  function _containsReplyGate(text){
    if(!_replyGate) return false;
    const normalized=_normalizeSpeech(text);
    if((' '+normalized+' ').includes(' '+_replyGate+' ')) return true;
    // Transcription may render a compound release phrase with or without a
    // hyphen/space ("Spider-Man" vs "Spiderman"). Accept that narrow variant
    // without making short one-word gates fuzzy.
    if(_replyGate.includes(' ')){
      const compactGate=_replyGate.replace(/\s+/g,'');
      if(compactGate.length>=5) return normalized.split(' ').includes(compactGate);
    }
    return false;
  }

  let _responseEventSeq=0;
  let _responseEventId=null;
  let _responseRetryCount=0;

  function _reservePlayback(responseId, started=false){
    if(responseId&&!_drainedResponses.has(responseId)&&
       (responseId===_activeResponseId||_playbackResponses.has(responseId))){
      _playbackResponses.set(responseId,started||_playbackResponses.get(responseId)===true);
    }
  }

  function _isAudioPart(part){
    return part&&(part.type==='audio'||part.type==='output_audio');
  }

  function _flushResponseQueue(){
    if(_replyGate||_responseOpen||_playbackResponses.size||_settlingResponses||_pendingCalls||
       _speakingItem!==null||_inputItems.length||!_responseReasons.size) return;
    // Context and its reply share ONE boundary. A new result is not permission
    // to cut the previous audio, or talk over a user whose transcript is pending.
    for(const [key,text] of _pendingContext){
      if(!_sendEvent({type:'conversation.item.create',item:{
        type:'message',role:'system',content:[{type:'input_text',text}]
      }})){
        _setState('error','Voice connection lost while delivering an update. The result remains in chat.');
        stopLiveVoice(true);
        return;
      }
      _pendingContext.delete(key);
    }
    const eventId='hermes-voice-response-'+_generation+'-'+(++_responseEventSeq);
    if(_sendEvent({event_id:eventId,type:'response.create'})){
      _requestedMirrorUsers=_deferredMirrorUsers.concat(_pendingMirrorUsers);
      _deferredMirrorUsers=[];
      _pendingMirrorUsers=[];
      _responseReasons.clear();
      _responseOpen=true;
      _responseEventId=eventId;
    }else{
      _setState('error','Voice connection lost before replying. Reconnect voice to continue.');
      stopLiveVoice(true);
    }
  }

  function _queueResponse(reason){
    if(reason!=='retry') _responseRetryCount=0;
    _responseReasons.add(reason||'turn');
    _flushResponseQueue();
  }

  async function _executeToolCall(name, args){
    if(name==='wait_for_user'){
      return {output:JSON.stringify({ok:true,waiting:true}),continueResponse:false};
    }
    let output='';
    if(name==='agent_ask'){
      if(!String(args.question||'').trim()) output='agent_ask failed: question required';
      else try{ output=await _askVerity(String(args.question)); }
      catch(e){ output='agent_ask failed: '+(e&&e.message||e); }
    }else if(name==='agent_status'){
      try{ output=await _agentStatus(); }
      catch(e){ output='agent_status failed: '+(e&&e.message||e); }
    }else if(name==='agent_steer'){
      if(!String(args.text||'').trim()) output='agent_steer failed: text required';
      else try{ output=await _agentSteer(String(args.text)); }
      catch(e){ output='agent_steer failed: '+(e&&e.message||e); }
    }else if(name==='agent_stop'){
      try{ output=await _agentStop(); }
      catch(e){ output='agent_stop failed: '+(e&&e.message||e); }
    }else{
      output='Unknown tool: '+name;
    }
    return {output:String(output==null?'':output),continueResponse:true};
  }

  function _dispatchToolCall(item){
    const callId=String(item&&item.call_id||'');
    if(!callId) return Promise.resolve({continueResponse:false});
    if(_toolCalls.has(callId)) return _toolCalls.get(callId);
    const generation=_generation;
    const promise=(async()=>{
      _pendingCalls++; _setState(_state);
      let result;
      let args={};
      try{
        try{ args=JSON.parse(item.arguments||'{}'); }
        catch(_){ result={output:'Invalid JSON arguments for '+String(item.name||'unknown tool'),continueResponse:true}; }
        if(!result&&(!args||typeof args!=='object'||Array.isArray(args))){
          result={output:'Tool arguments must be a JSON object',continueResponse:true};
        }
        if(!result&&_replyGate){
          result={output:'Reply hold is active. Wait for the release phrase.',continueResponse:false};
        }
        if(!result) result=await _executeToolCall(String(item.name||''),args);
        if(generation!==_generation) return {continueResponse:false};
        let output=String(result.output||'');
        // Full text remains in the WebUI transcript; bound the voice context.
        if(output.length>16000) output=output.slice(0,16000)+'\n…(truncated for voice; full answer is in the chat transcript)';
        if(!_sendEvent({type:'conversation.item.create',item:{type:'function_call_output',call_id:callId,output}})){
          _setState('error','Voice connection lost while delivering a tool result. Check the chat before retrying the action.');
          stopLiveVoice(true);
          return {continueResponse:false};
        }
        return {continueResponse:result.continueResponse!==false};
      }finally{
        if(generation===_generation){
          _pendingCalls=Math.max(0,_pendingCalls-1); _setState(_state);
        }
      }
    })();
    _toolCalls.set(callId,promise);
    return promise;
  }

  function _functionCalls(output){
    return (Array.isArray(output)?output:[]).filter(item=>
      item&&item.type==='function_call'&&item.name&&item.call_id&&(!item.status||item.status==='completed')
    );
  }

  async function _handleResponseDone(response){
    const generation=_generation;
    const responseId=String(response&&response.id||'');
    if(responseId&&_completedResponses.has(responseId)) return;
    if(responseId) _completedResponses.add(responseId);
    const ownsOpenResponse=!!responseId&&_activeResponseId===responseId;
    if(response&&response.status==='failed'){
      if(ownsOpenResponse){
        _setState('error','Voice response failed. Reconnect voice to continue; accepted Hermes tasks remain in chat.');
        stopLiveVoice(true);
      }
      return;
    }

    // Generation can finish before WebRTC's playback-start notification. The
    // audio content itself reserves the floor until this response drains.
    if((response.output||[]).some(item=>(item.content||[]).some(_isAudioPart))){
      _reservePlayback(responseId);
    }else if(ownsOpenResponse&&_playbackResponses.get(responseId)===false){
      // An announced part can be abandoned before any audio is produced. Its
      // terminal snapshot has no audio, so there is no buffer to wait on. Never
      // apply this shortcut after playback has actually started.
      _playbackResponses.delete(responseId);
      _drainedResponses.add(responseId);
    }

    if(ownsOpenResponse){
      _responseOpen=false;
      _activeResponseId=null;
      _responseEventId=null;
      _responseRetryCount=0;
    }

    let users=[];
    if(responseId&&_responseMirrorUsers.has(responseId)){
      users=_responseMirrorUsers.get(responseId)||[];
      _responseMirrorUsers.delete(responseId);
    }else if(ownsOpenResponse&&_requestedMirrorUsers.length){
      users=_requestedMirrorUsers;
      _requestedMirrorUsers=[];
    }

    let spoken='';
    try{
      for(const item of (response&&response.output||[])){
        if(item&&item.type==='message'&&item.role==='assistant'){
          for(const part of (item.content||[])){
            if(part&&(part.transcript||part.text)) spoken+=((part.transcript||part.text)+' ');
          }
        }
      }
    }catch(_){ }

    const calls=_functionCalls(response&&response.output);
    const toolNames=new Set(calls.map(call=>String(call.name||'')));
    if(toolNames.has('agent_ask')||toolNames.has('wait_for_user')){
      // agent_ask persists the prompt through the normal chat-start path;
      // wait_for_user intentionally discards ambient/non-addressed chatter.
      users=[];
    }else if(calls.length){
      // Keep the triggering transcript for the post-tool spoken answer rather
      // than persisting an unmatched user row or a throwaway preamble.
      _deferredMirrorUsers=users.concat(_deferredMirrorUsers);
      users=[];
    }else if(response.status==='completed'&&spoken.trim()&&users.length){
      void _mirrorTurn(users.join('\n'),spoken.trim());
    }

    if(calls.length){
      _settlingResponses++;
      let results;
      try{ results=await Promise.all(calls.map(_dispatchToolCall)); }
      finally{ if(generation===_generation) _settlingResponses--; }
      if(generation!==_generation) return;
      if(ownsOpenResponse&&results.some(result=>result&&result.continueResponse)){
        _queueResponse('tool');
      }else if(ownsOpenResponse){
        _flushResponseQueue();
      }
    }else if(ownsOpenResponse){
      _flushResponseQueue();
    }
  }

  function _onCompletedTranscript(msg){
    const txt=String(msg.transcript||'').trim();
    if(!txt) return;
    const itemId=String(msg.item_id||(msg.item&&msg.item.id)||msg.event_id||'');
    if(itemId&&_seenTranscriptItems.has(itemId)) return;
    if(itemId) _seenTranscriptItems.add(itemId);

    const newGate=_extractReplyGate(txt);
    if(newGate){
      _replyGate=newGate;
      _responseReasons.delete('user');
      _pendingMirrorUsers=[];
      _deferredMirrorUsers=[];
      // Only a recognized explicit silence command may interrupt playback;
      // arbitrary speech/VAD activity never does.
      if(_responseOpen) _sendEvent({type:'response.cancel'});
      if(_playbackResponses.size) _sendEvent({type:'output_audio_buffer.clear'});
      return;
    }
    if(_replyGate){
      if(!_containsReplyGate(txt)) return;
      _replyGate=null;
    }
    _pendingMirrorUsers.push(txt);
    _queueResponse('user');
  }

  function _onDataChannelMessage(ev){
    let msg=null;
    try{ msg=JSON.parse(ev.data); }catch(_){ return; }
    if(!msg||!msg.type) return;
    if(msg.type==='input_audio_buffer.speech_started'){
      _speakingItem=String(msg.item_id||'');
    }else if(msg.type==='input_audio_buffer.speech_stopped'){
      _speakingItem=null;
      // Keep the floor reserved through commit and transcription, not just VAD.
      if(msg.item_id&&!_seenTranscriptItems.has(msg.item_id)&&!_inputItems.includes(msg.item_id)) _inputItems.push(msg.item_id);
    }else if(msg.type==='input_audio_buffer.committed'&&msg.item_id){
      // Input transcription completion can arrive out of order. Apply silence
      // directives in audio-commit order, not transcription network order.
      if(!_seenTranscriptItems.has(msg.item_id)&&!_inputItems.includes(msg.item_id)) _inputItems.push(msg.item_id);
    }else if(msg.type==='response.content_part.added'&&_isAudioPart(msg.part)){
      _reservePlayback(msg.response_id);
    }else if(msg.type==='output_audio_buffer.started'){
      _reservePlayback(msg.response_id,true);
      if(_replyGate&&_playbackResponses.has(msg.response_id)) _sendEvent({type:'output_audio_buffer.clear'});
    }else if(msg.type==='output_audio_buffer.stopped'||msg.type==='output_audio_buffer.cleared'){
      if(!msg.response_id) return;
      _drainedResponses.add(msg.response_id);
      _playbackResponses.delete(msg.response_id);
      _flushResponseQueue();
    }else if(msg.type==='response.created'&&msg.response){
      _responseOpen=true;
      _activeResponseId=String(msg.response.id||'')||null;
      if(_activeResponseId){
        _responseMirrorUsers.set(_activeResponseId,_requestedMirrorUsers);
        _requestedMirrorUsers=[];
      }
    }else if(msg.type==='response.output_item.done'&&msg.item&&msg.item.type==='function_call'&&(!msg.item.status||msg.item.status==='completed')){
      void _dispatchToolCall(msg.item);
    }else if(msg.type==='response.function_call_arguments.done'&&msg.call_id&&msg.name){
      void _dispatchToolCall({type:'function_call',name:msg.name,call_id:msg.call_id,arguments:msg.arguments});
    }else if(msg.type==='response.done'&&msg.response){
      void _handleResponseDone(msg.response);
    }else if(msg.type==='conversation.item.input_audio_transcription.completed'){
      if(_speakingItem===String(msg.item_id||'')) _speakingItem=null;
      if(_inputItems.includes(msg.item_id)){
        _transcripts.set(msg.item_id,msg);
        while(_inputItems.length&&_transcripts.has(_inputItems[0])){
          const id=_inputItems.shift();
          const transcript=_transcripts.get(id);
          _transcripts.delete(id);
          _onCompletedTranscript(transcript);
        }
      }else _onCompletedTranscript(msg);
      _flushResponseQueue();
    }else if(msg.type==='conversation.item.input_audio_transcription.failed'){
      _setState('error','Voice transcription failed. Reconnect before continuing so a missed instruction is not ignored.');
      stopLiveVoice(true);
    }else if(msg.type==='error'){
      const failedEventId=String((msg.error&&msg.error.event_id)||msg.event_id||'');
      if(failedEventId&&failedEventId===_responseEventId){
        _responseOpen=false;
        _activeResponseId=null;
        _responseEventId=null;
        if(_responseRetryCount<1){
          _responseRetryCount++;
          _pendingMirrorUsers=_requestedMirrorUsers.concat(_pendingMirrorUsers);
          _requestedMirrorUsers=[];
          _responseReasons.add('retry');
          const generation=_generation;
          setTimeout(()=>{ if(generation===_generation) _flushResponseQueue(); },100);
        }else{
          _setState('error','Voice reply was rejected after retrying. Reconnect voice to continue; accepted Hermes tasks remain in chat.');
          stopLiveVoice(true);
        }
      }
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
    _digest=data.digest||'';
    return data; // {digest, ...}
  }

  async function _unbindSession(sid){
    const targetSid=sid||_boundSid;
    if(!targetSid) return;
    try{
      await fetch('api/voice/live/disconnect',{
        method:'POST',
        headers:Object.assign({'Content-Type':'application/json'},_csrf()),
        body:JSON.stringify({session_id:targetSid,yolo_was_enabled:_yoloWasEnabled})
      });
    }catch(_){ }
    if(_boundSid===targetSid) _boundSid=null;
  }

  function startLiveVoice(){
    if(_state==='connecting'||_state==='live') return;
    const generation=++_generation;
    _setState('connecting');
    // A stopped getUserMedia/connect request can still resolve. Serialize
    // setup so its late result cannot overwrite the next call's resources.
    _connectPromise=_connectPromise.catch(()=>{}).then(async()=>{
      await _unbindPromise;
      if(generation===_generation) await _startLiveVoice(generation);
    });
  }

  async function _startLiveVoice(generation){
    _replyGate=null;
    _responseOpen=false;
    _playbackResponses.clear();
    _drainedResponses.clear();
    _pendingContext.clear();
    _speakingItem=null;
    _activeResponseId=null;
    _responseEventId=null;
    _responseRetryCount=0;
    _responseReasons.clear();
    _seenTranscriptItems.clear();
    _inputItems.length=0;
    _transcripts.clear();
    _completedResponses.clear();
    _toolCalls.clear();
    _pendingMirrorUsers=[];
    _requestedMirrorUsers=[];
    _deferredMirrorUsers=[];
    _responseMirrorUsers.clear();

    _settlingResponses=0;
    _setState('connecting');
    await _unbindPromise;
    if(generation!==_generation) return;
    try{
      const bind=await _bindSession();
      if(generation!==_generation){
        const staleSid=_boundSid;
        _unbindPromise=_unbindSession(staleSid);
        return;
      }
      try{
        if(typeof showToast==='function') showToast(bind.yolo_enabled?'Live voice: full access enabled for this call':'Live voice connected',2500,'info');
      }catch(_){ }
    }catch(e){
      if(generation!==_generation) return;
      _setState('error','Live voice: '+(e&&e.message||e));
      _setState('off');
      return;
    }
    try{
      _mic=await navigator.mediaDevices.getUserMedia({audio:{
        echoCancellation:{ideal:true},
        noiseSuppression:{ideal:true},
        autoGainControl:{ideal:true},
        channelCount:{ideal:1}
      }});
      if(generation!==_generation){
        _mic.getTracks().forEach(track=>{ try{ track.stop(); }catch(_){ } });
        _mic=null;
        return;
      }
    }catch(e){
      const sid=_boundSid;
      await _unbindSession(sid);
      if(generation!==_generation) return;
      _setState('error','Microphone access denied');
      _setState('off');
      return;
    }
    try{
      _pc=new RTCPeerConnection();
      _audioEl=document.createElement('audio');
      _audioEl.autoplay=true;
      _audioEl.playsInline=true;
      _audioEl.setAttribute('playsinline','');
      _pc.ontrack=(e)=>{ _audioEl.srcObject=e.streams[0]; };
      _pc.addTrack(_mic.getTracks()[0]);
      _dc=_pc.createDataChannel('oai-events');
      _dc.onmessage=ev=>{ if(generation===_generation) _onDataChannelMessage(ev); };
      _dc.onopen=_flushResponseQueue;
      _dc.onclose=()=>{
        if(generation!==_generation) return;
        _setState('error','Voice connection closed. Accepted Hermes tasks remain in chat.');
        stopLiveVoice(true);
      };
      _pc.onconnectionstatechange=()=>{
        const st=_pc&&_pc.connectionState;
        if(st==='connected') _setState('live');
        else if(st==='failed'||st==='disconnected'||st==='closed'){
          if(_state!=='off') stopLiveVoice(true);
        }
      };
      const offer=await _pc.createOffer();
      await _pc.setLocalDescription(offer);
      if(generation!==_generation) return;
      const headers=Object.assign({'Content-Type':'application/json'},_csrf());
      const res=await fetch('api/voice/live/sdp',{
        method:'POST',headers,
        body:JSON.stringify(Object.assign({sdp:offer.sdp},_digest?{digest:_digest}:{}))
      });
      if(!res.ok){
        let msg='HTTP '+res.status;
        try{ const e=await res.json(); if(e&&e.error) msg=e.error; }catch(_){ }
        throw new Error(msg);
      }
      const data=await res.json();
      if(!data||!data.sdp) throw new Error('no SDP answer');
      if(generation!==_generation) return;
      await _pc.setRemoteDescription({type:'answer',sdp:data.sdp});
      // state flips to 'live' via onconnectionstatechange
    }catch(e){
      if(generation!==_generation) return;
      _setState('error','Live voice failed: '+(e&&e.message||e));
      stopLiveVoice(true);
    }
  }

  function stopLiveVoice(silent){
    _generation++;
    _voiceRequests.forEach(controller=>controller.abort());
    _voiceRequests.clear();
    try{ if(_dc){ _dc.onmessage=null; _dc.onopen=null; _dc.onclose=null; _dc.close(); } }catch(_){ }
    try{ if(_pc){ _pc.onconnectionstatechange=null; _pc.close(); } }catch(_){ }
    try{ if(_mic){ _mic.getTracks().forEach(tr=>{ try{tr.stop();}catch(_){}}); } }catch(_){ }
    try{ if(_audioEl){ _audioEl.srcObject=null; _audioEl.remove(); } }catch(_){ }
    _dc=null; _pc=null; _mic=null; _audioEl=null; _pendingCalls=0;
    _settlingResponses=0;
    _activeAsk=null;
    _replyGate=null;
    _responseOpen=false;
    _playbackResponses.clear();
    _drainedResponses.clear();
    _pendingContext.clear();
    _speakingItem=null;
    _activeResponseId=null;
    _responseEventId=null;
    _responseRetryCount=0;
    _responseReasons.clear();
    _seenTranscriptItems.clear();
    _inputItems.length=0;
    _transcripts.clear();
    _completedResponses.clear();
    _toolCalls.clear();
    _pendingMirrorUsers=[];
    _requestedMirrorUsers=[];
    _deferredMirrorUsers=[];
    _responseMirrorUsers.clear();
    const sid=_boundSid;
    // Fire-and-forget cleanup; the next call waits for this restore before bind.
    if(sid) _unbindPromise=_unbindSession(sid);
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
