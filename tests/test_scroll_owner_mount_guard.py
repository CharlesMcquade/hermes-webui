"""Browser behavior of mount scheduling while follow interpretation is guarded."""
from tests.test_scroll_owner_live_reconciliation import isolated_webui, page as page  # noqa: F401


def test_compensation_guard_does_not_block_required_mount(page):
    result = page.evaluate("""() => {
      S.busy=false;S.activeStreamId=null;delete INFLIGHT.fixture;
      S.messages=Array.from({length:240},(_,i)=>({role:i%2?'assistant':'user',
        content:'Row '+i+'\\n\\n'+'Public text. '.repeat(50)}));
      renderMessages();
      const c=$('messages');
      // Isolate this scroll dispatch from any rAF queued by initial rendering.
      cancelAnimationFrame(_messageVirtualScrollRaf);_messageVirtualScrollRaf=0;
      c.scrollTop=0;
      _programmaticScroll=true;_programmaticScrollSetAt=performance.now();
      const pinned=_scrollPinned;
      c.dispatchEvent(new Event('scroll'));
      return {scheduled:_messageVirtualScrollRaf!==0,pinned,
        afterPinned:_scrollPinned,fresh:_freshProgrammaticScrollActive()};
    }""")
    assert result['fresh'] and result['scheduled'], result
    assert result['afterPinned'] == result['pinned'], result
    # Wait for the required rAF mount AND the ResizeObserver delivery it can
    # trigger, rather than sampling at two arbitrary wall-clock instants.
    page.wait_for_function("""() => {
      const idle=window._mountGuardIdleProbe||(window._mountGuardIdleProbe={revision:-1,frames:0});
      const revision=_messageWindowRevision, observed=_messageWindowObserved;
      if(_messageVirtualScrollRaf!==0||!observed||observed.revision!==revision||
         observed.top!==$('messages').scrollTop){
        idle.revision=-1;idle.frames=0;return false;
      }
      idle.frames=idle.revision===revision?idle.frames+1:1;
      idle.revision=revision;
      return idle.frames>=3;
    }""", polling='raf')
    before = page.evaluate('_messageWindowRevision')
    page.evaluate("""() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
    assert page.evaluate('_messageWindowRevision') == before, 'idle render loop'
