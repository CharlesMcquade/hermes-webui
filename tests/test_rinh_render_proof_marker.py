"""R-INH Phase 3: the canonical renderer (static/messages.js) stamps its
revision marker `data-render-proof="rinh-p3"` on every live assistant row it
creates. Source-text guard so the marker cannot silently regress."""
import pathlib


def _messages_js():
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / "static" / "messages.js").read_text(encoding="utf-8")


def test_live_assistant_row_stamps_render_proof_marker():
    js = _messages_js()
    assert js.count('assistantRow.setAttribute(\'data-live-assistant\',\'1\');') == 1
    idx = js.index('assistantRow.setAttribute(\'data-live-assistant\',\'1\');')
    after = js[idx: idx + 600]
    assert "assistantRow.setAttribute('data-render-proof','rinh-p3');" in after
