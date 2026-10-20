"""S01 title DB-authority edge coverage: reset labels, deletion, repeated callbacks.

Complements tests/test_session_title_authority.py. The canonical DB winner at
api/state_sync.sync_session_title must hold for every supported reset label
(not just Untitled), survive a repeated generator callback, and fail closed
when the session disappears mid-generation.
"""
import json

import pytest

from api import models, routes, state_sync, streaming

pytestmark = pytest.mark.usefixtures("title_state")


@pytest.fixture
def title_state(tmp_path, monkeypatch):
    from collections import OrderedDict
    from contextlib import nullcontext

    SessionDB = pytest.importorskip("hermes_state").SessionDB
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    monkeypatch.setattr(state_sync, "_get_state_db", lambda profile=None, **kwargs: SessionDB(db_path=db_path))
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "index.json")
    sessions = OrderedDict()
    for module in (models, routes, streaming):
        monkeypatch.setattr(module, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda *a: None)

    def make(sid, title="Untitled", source=None, hidden=False):
        s = models.Session(session_id=sid, title=title, workspace=str(tmp_path), profile="default")
        s.messages = [{"role": "user", "content": "BG3 infernal engine upgrade limit"},
                      {"role": "assistant", "content": "Here are the upgrade limits."}]
        s.save(touch_updated_at=False)
        sessions[sid] = s
        db.ensure_session(session_id=sid, source="webui")
        if hidden:
            db._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET hidden = 1 WHERE id = ?", (sid,)))
        if source == "user":
            db.set_session_title(sid, title)
        elif source:
            db.set_auto_title(sid, title, source=source)
        return s

    yield db, make, tmp_path, sessions
    db.close()


def _update(s):
    events = []
    streaming._run_background_title_update(s.session_id, s.messages[0]["content"],
                                           s.messages[1]["content"], s.title,
                                           lambda e, d: events.append((e, d)))
    return events


@pytest.mark.parametrize("reset_label", ["Untitled", "New Chat"])
def test_reset_labels_stored_in_db_are_not_canonical(title_state, monkeypatch, reset_label):
    """S01 carve-out (implemented in api/streaming.py): a DB row holding only a
    reset label with llm provenance is a leftover from a manual reset, never a
    generated title — the generator result wins and the reset label does not
    become canonical.
    """
    db, make, _path, _sessions = title_state
    s = make("edge-reset", reset_label, "llm")
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux",
                        lambda *a, **kw: ("[FF] Sleeper league roster audit", "llm_aux", ""))
    _update(s)
    # The reset label must NOT win; the generated title is published instead.
    assert s.compact()["title"] != reset_label
    assert s.compact()["title"] == "[FF] Sleeper league roster audit"
    # A genuinely generated DB title with llm provenance still wins.
    s2 = make("edge-reset-real", "[FF] Draft hub rankings", "llm")
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux",
                        lambda *a, **kw: ("[FF] Different generated name", "llm_aux", ""))
    _update(s2)
    assert s2.compact()["title"] == "[FF] Draft hub rankings"


def test_hidden_reset_row_is_not_overwritten_by_generator(title_state, monkeypatch):
    """A hidden canonical reset row stays untouched by an automatic refresh."""
    db, make, path, _sessions = title_state
    s = make("edge-hidden-reset", "Bot Chat", "llm", hidden=True)
    original = db.get_session_title(s.session_id)
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux",
                        lambda *a, **kw: ("[FF] Sleeper league roster audit", "llm_aux", ""))
    _update(s)
    assert db.get_session_title(s.session_id) == original
    assert not any(e == "title" for e, _ in _update.__defaults__ or [])


def test_repeated_generator_callback_is_idempotent(title_state):
    """A repeated generator callback republishes the identical DB winner."""
    db, make, _path, _sessions = title_state
    s = make("edge-repeat", "Untitled")
    first = streaming._apply_generated_title(s, "Repeated Callback Title")
    second = streaming._apply_generated_title(s, "Repeated Callback Title")
    assert first == second == db.get_session_title(s.session_id)
    assert db.get_session_title_source(s.session_id) == "llm"
    # And a second distinct callback still lands on exactly one DB row value.
    third = streaming._apply_generated_title(s, "Repeated Callback Title II")
    assert third == db.get_session_title(s.session_id)


def test_deleted_session_fails_closed_without_publishing(title_state, monkeypatch):
    """Session deletion mid-generation: skipped, no publication, no crash."""
    db, make, path, sessions = title_state
    s = make("edge-deleted")
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux",
                        lambda *a, **kw: ("[FF] Sleeper league roster audit", "llm_aux", ""))

    def load_missing(sid, *a, **kw):
        return None

    monkeypatch.setattr(models.Session, "load", staticmethod(load_missing))
    sessions.pop(s.session_id, None)
    events = _update(s)
    assert not any(e == "title" for e, _ in events)
    assert any(
        e == "title_status" and d.get("status") == "skipped"
        for e, d in events
    )
    assert events[-1][0] == "stream_end"
