/* embed-host.js — WebUI embedded-page host adapter (Phase 1 spike, frame side).
 *
 * Loaded FIRST in embed mode (see static/index.html) — before the inline
 * theme/localStorage blocks and before pwa-startup.js — so the storage
 * namespacing wrapper covers every script on the page.
 *
 * Implements the frame side of HOST-CONTRACT.md (candidate, frozen for spike):
 *   §1 handshake   hello {t:'hello',v:1,gen,nonce}   → ready {t:'ready',v:1,gen,nonce,server:{build,caps}}
 *   §3 requests    {t:'req',op,method,path,query,body,deadlineMs} → {t:'res',op,status,body,headers} | {t:'res',op,status:0,error}
 *   §4 streams     {t:'stream-sub',sub,kind,url,lastEventId} ← {t:'stream-ev',sub,event,data} / {t:'stream-end',sub,reason}
 *   §7 storage     namespaced under hermes-embed-<gen>-, legacy-migration + settings-mirror writes suppressed
 *   §11            origin-pinned handshake, single-use nonce, no auth headers from the frame
 *
 * Scope: chat text path only. Media/upload brokerage is deferred post-G1.
 */
(function(){
  'use strict';

  /* ── Embed detection ──────────────────────────────────────────────────── */

  function _embedQueryFlag(){
    try{ return new URLSearchParams(location.search).get('hermes_embed')==='1'; }
    catch(_){ return false; }
  }

  var EMBED = (typeof window!=='undefined' && window.__HERMES_EMBED__===true) || _embedQueryFlag();

  /* Expose for boot.js / workspace.js gates. Window property is not writable
   * afterwards so page scripts cannot flip the policy either way. */
  if(typeof window!=='undefined'){
    try{
      Object.defineProperty(window,'__HERMES_EMBED__',{value:EMBED,configurable:false,writable:false});
    }catch(_){ window.__HERMES_EMBED__=EMBED; }
  }
  if(!EMBED || typeof window==='undefined') return; // ordinary WebUI tab: no-op

  /* View generation: supplied by the shell (?gen=) or a stable fallback so
   * storage never collides across generations (contract §2). */
  var GEN = (function(){
    try{
      var g=(new URLSearchParams(location.search).get('gen')||'').replace(/[^A-Za-z0-9_-]/g,'');
      if(g) return g;
    }catch(_){}
    return '0';
  })();

  var STORAGE_PREFIX='hermes-embed-'+GEN+'-';

  /* ── §7 Storage namespacing wrapper ─────────────────────────────────────
   * Installed before the inline index.html blocks that read localStorage
   * directly. All keys are transparently prefixed; the six legacy-migration
   * writes plus settings-mirror / session-restore / boot-flag writes are
   * suppressed (WEBUI-INVENTORY §1). Reads of suppressed keys return null so
   * the page boots with shipped defaults (contract §7 New-by-default). */

  var SUPPRESSED_WRITE_KEYS = [
    'hermes-theme',
    'hermes-skin',
    'hermes-font-size',
    'hermes-content-width',
    'hermes-webui-workspace-panel',
    'hermes-webui-workspace-panel-pref',
    'hermes-webui-sidebar-collapsed',
    'hermes-webui-tab-order',
    'hermes-webui-hidden-tabs',
    'hermes-webui-session',
    'hermes-webui-server-stopped',
    'hermes-webui-model'
  ];
  /* R4 (PHASE2-CONTRACT-DELTAS): settings mirrors are WRITE-suppressed in
   * embed mode even when GET /api/settings succeeds — boot applies settings
   * read-only, never persisting them back to the embed namespace. Reads still
   * resolve (namespaced, normally absent → shipped defaults). */
  var SETTINGS_MIRROR_WRITE_KEYS = [
    'hermes-lang',
    'hermes-default-message-mode',
    'hermes-busy-input-mode',
    'hermes-auto-scroll-follow',
    'hermes-tts-engine',
    'hermes-tts-voice',
    'hermes-tts-rate',
    'hermes-tts-pitch',
    'hermes-voice-silence-ms',
    'hermes-raw-audio-mode',
    'hermes-tts-enabled',
    'hermes-dictation-append',
    'hermes-mic-continuous',
    'hermes-voice-mode-button',
    'hermes-voice-continuous',
    'mic_force_mediarecorder'
  ];
  var SUPPRESSED_WRITE_SET = {};
  for(var _i=0;_i<SUPPRESSED_WRITE_KEYS.length;_i++) SUPPRESSED_WRITE_SET[SUPPRESSED_WRITE_KEYS[_i]]=true;
  var SETTINGS_MIRROR_WRITE_SET = {};
  for(var _j=0;_j<SETTINGS_MIRROR_WRITE_KEYS.length;_j++) SETTINGS_MIRROR_WRITE_SET[SETTINGS_MIRROR_WRITE_KEYS[_j]]=true;

  function _nsKey(key){ return STORAGE_PREFIX+String(key); }

  function installEmbedStorage(nativeStorage){
    var store = {
      getItem: function(key){
        if(SUPPRESSED_WRITE_SET[String(key)]) return null; // defaults, never restored state
        try{ return nativeStorage.getItem(_nsKey(key)); }catch(_){ return null; }
      },
      setItem: function(key,value){
        if(SUPPRESSED_WRITE_SET[String(key)]) return; // suppress legacy-migration + mirror writes
        if(SETTINGS_MIRROR_WRITE_SET[String(key)]) return; // R4: settings read-only, no mirror writes
        try{ nativeStorage.setItem(_nsKey(key),String(value)); }catch(_){}
      },
      removeItem: function(key){
        if(SUPPRESSED_WRITE_SET[String(key)]) return;
        if(SETTINGS_MIRROR_WRITE_SET[String(key)]) return; // R4: no mirror eviction either
        try{ nativeStorage.removeItem(_nsKey(key)); }catch(_){}
      },
      /* Prefixed clear: must never wipe keys belonging to the ordinary WebUI
       * tab or another embed generation (R6 storage fencing). */
      clear: function(){
        try{
          var doomed=[];
          for(var i=0;i<nativeStorage.length;i++){
            var k=nativeStorage.key(i);
            if(k!==null && String(k).indexOf(STORAGE_PREFIX)===0) doomed.push(k);
          }
          for(var j=0;j<doomed.length;j++){ try{ nativeStorage.removeItem(doomed[j]); }catch(_){} }
        }catch(_){}
      },
      key: function(i){
        try{ return nativeStorage.key(i); }catch(_){ return null; }
      },
      get length(){ try{ return nativeStorage.length; }catch(_){ return 0; } }
    };
    try{
      Object.defineProperty(window,'localStorage',{value:store,configurable:false,writable:false});
    }catch(_){ window.localStorage=store; }
    return store;
  }

  var __embedStorage = installEmbedStorage(window.localStorage);

  /* ── CSRF monkey-patch neutralization (contract §3) ──────────────────────
   * index.html line ~32 assigns window.__HERMES_CONFIG__={...,csrfToken:...};
   * the fetch wrapper at line ~34 bails out when the token is falsy. Trap the
   * assignment and blank the token so the wrapper never installs. The broker
   * adds any server-required headers parent-side. */
  try{
    var __cfg=null;
    Object.defineProperty(window,'__HERMES_CONFIG__',{
      configurable:false,
      get:function(){ return __cfg; },
      set:function(v){
        try{ if(v&&Object.prototype.hasOwnProperty.call(v,'csrfToken')) v.csrfToken=''; }catch(_){}
        __cfg=v;
      }
    });
  }catch(_){ /* non-configurable host: fetch shim below still strips CSRF headers */ }

  /* ── §1/§11 Broker handshake + channel ────────────────────────────────── */

  var PARENT_TARGET = window.parent;
  var broker = {
    ready:false,
    pinnedOrigin:null,
    usedNonces:{},          // §11.3 single-use nonce
    caps:['stream','settings-read'], // R7: caps the frame actually provides (media/upload deferred to Phase 3)
    capsSource:'static',    // R7: where the caps list came from (static adapter contract, not negotiated)
    get build(){ try{ return String(window.__HERMES_WEBUI_BUNDLE_VERSION__||'spike'); }catch(_){ return 'spike'; } }
  };

  function _post(msg){
    /* S3 (SEC-AUDIT-P2): before the handshake the only outbound message is
     * `ready` in reply to a verified-shape hello, and the payload carries no
     * secrets (bearer never enters the frame, §11.8) — '*' is the bootstrap
     * target. After the pin exists, every postMessage is restricted to the
     * pinned shell origin (defense-in-depth: a same-origin XSS can no longer
     * observe broker traffic addressed to the real parent). */
    var target = broker.pinnedOrigin || '*';
    try{ PARENT_TARGET.postMessage(msg, target); }catch(_){}
  }

  function _uuid(){
    try{ return crypto.randomUUID(); }
    catch(_){ return 'op-'+Date.now()+'-'+Math.random().toString(36).slice(2); }
  }

  function handleHello(ev){
    var d=ev.data;
    if(!d||d.t!=='hello'||d.v!==1) return;
    if(typeof d.gen!=='string'||d.gen!==GEN) return;              // generation fence (§2)
    if(typeof d.nonce!=='string'||!d.nonce) return;
    if(broker.usedNonces[d.nonce]) return;                        // §11.3 replay — rejected even after a completed handshake
    /* Record EVERY nonce we see, including post-handshake hellos, so nonce
     * reuse is permanently rejected regardless of handshake state. */
    if(broker.ready) return;                                      // single handshake
    if(broker.pinnedOrigin!==null && ev.origin!==broker.pinnedOrigin) return; // §11.1
    if(typeof broker.pinnedOrigin!=='string'){
      try{
        var declared=null;
        try{ declared=new URLSearchParams(location.search).get('embed_parent'); }catch(_){}
        /* S4 (SEC-AUDIT-P2): a declared embed_parent is authoritative — the
         * first hello MUST match it, not merely fail to contradict it. The
         * shell always passes it, so a same-origin forgery that omits or
         * mismatched the declaration is rejected instead of pinned. */
        if(declared && ev.origin!==declared) return;
        if(declared) broker.declaredParent=declared;
      }catch(_){}
    }
    broker.usedNonces[d.nonce]=true;
    broker.pinnedOrigin=ev.origin;
    broker.ready=true;
    _post({t:'ready',v:1,gen:GEN,nonce:d.nonce,server:{build:broker.build,caps:broker.caps.slice()}});
    _drainPending();
  }

  /* R3 degraded-mode disclosure: broker-policy failures are surfaced as a
   * small non-intrusive notice instead of silent deadness. The 5 fail-closed
   * feed families (R1: SSE event feeds are NOT device-auth routes in Phase 2)
   * emit broker:policy errors; every distinct degraded feature path is
   * disclosed once, no toast spam. */
  var _degradedPaths={};
  function _recordDegraded(kind,reason){
    try{
      var key=String(kind||'unknown')+'|'+String(reason||'policy');
      if(_degradedPaths[key]) return;
      _degradedPaths[key]=true;
      if(typeof window.__hermesEmbedDegradedNotice==='function'){
        window.__hermesEmbedDegradedNotice(kind,reason);
      }
    }catch(_){}
  }
  window.__hermesEmbedRecordDegraded=_recordDegraded;

  window.addEventListener('message',function(ev){
    if(broker.pinnedOrigin!==null && ev.origin!==broker.pinnedOrigin) return; // §11.1 strict after pin
    var d=ev.data;
    if(!d||typeof d!=='object') return;
    if(d.t==='hello'){ handleHello(ev); return; }
    if(!broker.ready) return;
    if(d.t==='res'){ _resolveRes(d); return; }
    if(d.t==='stream-ev'){ _dispatchStreamEv(d); return; }
    if(d.t==='stream-end'){ _dispatchStreamEnd(d); return; }
    /* CDP/browser-capability shapes are absent from the channel (§8/§11.8):
     * anything else is ignored and never dispatched. */
  });

  /* ── §3 fetch shim — relative api/* routed through t:req ops ──────────── */

  var nativeFetch = (typeof window.fetch==='function') ? window.fetch.bind(window) : null;
  var pending = {};         // op → {resolve,reject}
  var pendingDeadline = {}; // op → timer

  function _isBrokerableUrl(url){
    try{
      var u=(url instanceof URL)?url:new URL(String(url),(document.baseURI||location.href));
      if(u.origin!==location.origin) return false;      // §3 absolute/external rejected
      return /(^|\/)api\//.test(u.pathname)?(/^\/.*api\//.test(u.pathname)||u.pathname.indexOf('api/')===0):false;
    }catch(_){ return false; }
  }
  /* Simpler, exact rule: same-origin and the path contains an `api/` segment
   * at the start (relative mounts) or `/api/` (root mount). */
  function _brokerable(url){
    try{
      var u=(url instanceof URL)?url:new URL(String(url),(document.baseURI||location.href));
      if(u.origin!==location.origin) return false;
      var p=u.pathname.replace(/^\/+/,'');
      return p==='api'||p.indexOf('api/')===0;
    }catch(_){ return false; }
  }

  function _dropAuthHeaders(headers){
    /* Frame never supplies auth headers (§3); strip before dispatch. */
    try{
      ['authorization','x-hermes-extension-id','x-hermes-profile','x-hermes-csrf-token'].forEach(function(h){ headers.delete(h); });
    }catch(_){}
    return headers;
  }

  function brokerFetch(url,init){
    init=init||{};
    var u;
    try{ u=new URL(String(url),(document.baseURI||location.href)); }catch(e){ return Promise.reject(e); }
    var method=String(init.method||'GET').toUpperCase();
    var headers=_dropAuthHeaders(new Headers(init.headers||undefined));
    var body=init.body;
    var query={};
    u.searchParams.forEach(function(v,k){ query[k]=v; });
    var op=_uuid();
    var deadlineMs=(Number(init.timeoutMs)>0)?Number(init.timeoutMs):25000;
    return new Promise(function(resolve,reject){
      pending[op]={resolve:resolve,reject:reject,url:u.href};
      pendingDeadline[op]=setTimeout(function(){
        _settleRes({op:op,status:0,error:'timeout'});
      },deadlineMs);
      if(!broker.ready){
        /* No verified handshake yet: fail closed rather than fetch with
         * ambient credentials (contract §3/§10). */
        _settleRes({op:op,status:0,error:'policy'});
        return;
      }
      _post({t:'req',op:op,method:method,path:u.pathname.replace(/^\/+/,''),
             query:query,body:body,deadlineMs:deadlineMs});
    });
  }

  function _settleRes(d){
    var entry=pending[d.op];
    if(!entry) return;
    clearTimeout(pendingDeadline[d.op]);
    delete pendingDeadline[d.op];
    delete pending[d.op];
    if(d.status===0){
      var err=new Error('broker:'+(d.error||'network'));
      err.brokerError=d.error||'network';
      /* R3: broker policy failures (fail-closed feeds) must be disclosed, not
       * silently swallowed by the caller. Only the policy family counts as a
       * degraded-mode signal — timeouts/network are retried paths. */
      if(d.error==='policy') _recordDegraded('request:'+String(entry.url||''),'policy');
      entry.reject(err);
      return;
    }
    entry.resolve(_makeResponse(d,entry.url));
  }

  function _resolveRes(d){ _settleRes(d); }

  function _makeResponse(d,url){
    var body=d.body;
    var headers=d.headers||{};
    var ct=headers['content-type']||headers['Content-Type']||'';
    var textBody=(typeof body==='string')?body:(body===undefined||body===null?'':JSON.stringify(body));
    if(!ct && body && typeof body==='object') ct='application/json';
    var hdrs={ get:function(name){ return headers[String(name).toLowerCase()]||null; },
               has:function(name){ return Object.prototype.hasOwnProperty.call(headers,String(name).toLowerCase()); } };
    var status=Number(d.status)||0;
    return {
      ok:status>=200&&status<300,
      status:status,
      statusText:headers['status-text']||'',
      url:url,
      headers:hdrs,
      text:function(){ return Promise.resolve(textBody); },
      json:function(){ return Promise.resolve(typeof body==='string'?JSON.parse(body):body); },
      blob:function(){ return Promise.reject(new Error('broker:media-deferred')); }
    };
  }

  window.fetch=function(input,init){
    if(_brokerable(input)) return brokerFetch(input,init);
    if(nativeFetch) return nativeFetch(input,init);
    return Promise.reject(new Error('broker:no-native-fetch'));
  };

  /* ── §4 EventSource-compatible shim for the 5 ES families ─────────────── */

  var SUB_KINDS={
    'api/chat/stream':'chat',
    'api/session/stream':'session',
    'api/sessions/approval/stream':'approval',
    'api/sessions/clarify/stream':'clarify',
    'api/sessions/events':'sessions-events',
    'api/sessions/gateway/stream':'gateway'
  };

  function _kindFor(path){
    var p=String(path).replace(/^\/+/,'');
    var best=null;
    for(var k in SUB_KINDS){ if(p===k||p.indexOf(k+'?')===0||p.indexOf(k)===0){ if(!best||k.length>best.length) best=k; } }
    /* Defense in depth (R7 hardening): unknown feed paths get NO kind — the
     * stream-sub is never even posted parent-side, so a frame cannot smuggle
     * a subscription for a feed the shell never allowlisted. */
    return best?SUB_KINDS[best]:null;
  }

  function EmbedEventSource(url,cfg){
    cfg=cfg||{};
    var self=this;
    this.url=String(url);
    this.withCredentials=!!cfg.withCredentials;
    this.readyState=0; // CONNECTING
    this.onopen=null; this.onmessage=null; this.onerror=null;
    this._listeners={};       // event name → [fn]
    this._closed=false;
    var u;
    try{ u=new URL(this.url,(document.baseURI||location.href)); }catch(e){ this._fail(); return; }
    this._path=u.pathname.replace(/^\/+/,'');
    this._query={}; u.searchParams.forEach(function(v,k){ self._query[k]=v; });
    this._sub=_uuid();
    SUBS[this._sub]=this;
    /* Subscription is queued until the handshake completes; _drainPending
     * flushes it. Streams to feeds outside the allowlist fail closed
     * parent-side (§4) — we surface that as an error event. */
    /* R7 hardening: refuse construction up front for paths outside SUB_KINDS —
     * independent of handshake state — so an unknown feed never queues, never
     * posts a stream-sub, and is disclosed as degraded (R3), not silent. */
    if(_kindFor(this._path)===null){
      _recordDegraded(String(this._path||'unknown'),'policy');
      this._fail();
      return;
    }
    this._subscribe();
  }
  EmbedEventSource.CONNECTING=0;
  EmbedEventSource.OPEN=1;
  EmbedEventSource.CLOSED=2;
  EmbedEventSource.prototype._subscribe=function(){
    if(this._closed) return;
    var kind=_kindFor(this._path);
    /* R7 hardening: refuse to construct (post) streams whose kind is not in
     * SUB_KINDS. Unknown feed paths fail closed frame-side AND are disclosed
     * as a degraded feature rather than left silently dead (R3). */
    if(kind===null){
      _recordDegraded(String(this._path||'unknown'),'policy');
      this._fail();
      return;
    }
    _post({t:'stream-sub',sub:this._sub,kind:kind,url:this._urlWithQuery(),lastEventId:this.lastEventId||''});
    this._markOpen();
  };
  EmbedEventSource.prototype._urlWithQuery=function(){
    var self=this, parts=[];
    for(var k in this._query){ parts.push(encodeURIComponent(k)+'='+encodeURIComponent(self._query[k])); }
    return this._path+(parts.length?'?'+parts.join('&'):'');
  };
  EmbedEventSource.prototype._markOpen=function(){
    if(this.readyState===1) return;
    this.readyState=1; // OPEN
    var ev={type:'open',target:this};
    if(typeof this.onopen==='function'){ try{ this.onopen(ev); }catch(_){} }
    this._emit('open',ev);
  };
  EmbedEventSource.prototype._emit=function(type,ev){
    var ls=this._listeners[type];
    if(!ls) return;
    for(var i=0;i<ls.length;i++){ try{ ls[i].call(this,ev); }catch(_){} }
  };
  EmbedEventSource.prototype.addEventListener=function(type,fn){
    (this._listeners[type]=this._listeners[type]||[]).push(fn);
  };
  EmbedEventSource.prototype.removeEventListener=function(type,fn){
    var ls=this._listeners[type]||[];
    var i=ls.indexOf(fn);
    if(i>=0) ls.splice(i,1);
  };
  EmbedEventSource.prototype._fail=function(){
    if(this._closed) return;
    this.readyState=2;
    var ev={type:'error',target:this};
    if(typeof this.onerror==='function'){ try{ this.onerror(ev); }catch(_){} }
    this._emit('error',ev);
  };
  EmbedEventSource.prototype._onServerEvent=function(event,data){
    if(this._closed) return;
    this._markOpen();
    var ev={type:event||'message',data:data===undefined?'':String(data),target:this,lastEventId:''};
    this._emit(ev.type,ev);
    if(ev.type!=='message' && typeof this.onmessage==='function' && event==='message'){}
    if(ev.type==='message' && typeof this.onmessage==='function'){ try{ this.onmessage(ev); }catch(_){} }
  };
  EmbedEventSource.prototype._onServerEnd=function(reason){
    if(this._closed) return;
    delete SUBS[this._sub];
    if(reason==='error'||reason==='revoked'){ this._fail(); return; }
    /* reason==='closed': server closed cleanly. Mirror native ES by closing
     * — attachLiveStream treats closed ES via readyState, and reconnect logic
     * re-subscribes by constructing a fresh EventSource. */
    this.close();
  };
  EmbedEventSource.prototype.close=function(){
    if(this._closed) return;
    this._closed=true;
    this.readyState=2; // CLOSED
    if(SUBS[this._sub]){ delete SUBS[this._sub]; }
    if(broker.ready){ _post({t:'stream-end',sub:this._sub,reason:'closed'}); }
  };
  EmbedEventSource.prototype.dispatchEvent=function(ev){ this._emit(ev&&ev.type,ev); return true; };

  var SUBS={};
  window.EmbedEventSource=EmbedEventSource;
  /* Replace the constructor used by messages.js `_wireSSE`, sessions.js, and
   * ui.js — messages.js needs zero edits (open/message/error/close + named
   * listeners preserved). */
  try{ window.EventSource=EmbedEventSource; }
  catch(_){ window.EventSource=EmbedEventSource; }

  function _dispatchStreamEv(d){
    var es=SUBS[d.sub];
    if(!es) return;
    es._onServerEvent(d.event,d.data);
  }
  function _dispatchStreamEnd(d){
    var es=SUBS[d.sub];
    if(!es) return;
    es._onServerEnd(d.reason||'closed');
  }

  /* ── Pending-queue flush after handshake ──────────────────────────────── */

  var _pendingSubs=[];
  EmbedEventSource.prototype.__queueIfPending=function(){};
  var _origSubscribe=EmbedEventSource.prototype._subscribe;
  EmbedEventSource.prototype._subscribe=function(){
    if(!broker.ready){ _pendingSubs.push(this); return; }
    _origSubscribe.call(this);
  };
  function _drainPending(){
    var q=_pendingSubs; _pendingSubs=[];
    for(var i=0;i<q.length;i++){ try{ q[i]._subscribe(); }catch(_){} }
    /* replay params (_runJournalReplayParams) are already inside the URL
     * query captured at construction — they pass through unchanged (§4). */
  }

  /* ── Test/inspection surface (spike only) ─────────────────────────────── */
  window.__HERMES_EMBED_HOST__={
    version:1,
    gen:GEN,
    storagePrefix:STORAGE_PREFIX,
    storage:__embedStorage,
    suppressedWriteKeys:SUPPRESSED_WRITE_KEYS.slice(),
    broker:broker,
    capsSource:'static',   // R7: honest caps provenance on the inspection surface
    caps:broker.caps.slice(),
    degradedPaths:_degradedPaths,
    recordDegraded:_recordDegraded,
    EmbedEventSource:EmbedEventSource,
    isBrokerable:function(u){ return _brokerable(u); }
  };
})();
