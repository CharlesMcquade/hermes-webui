"""Incremental steer-acceptance scan must match the full ``_read_jsonl`` decision."""
import json
import os

import pytest

import api.run_journal as rj


def _full(path):
    events, malformed = rj._read_jsonl(path)
    return bool(malformed), any(e.get("terminal") for e in events)


def _row(seq, terminal=False):
    return json.dumps({"seq": seq, "event": "token", "terminal": terminal}) + "\n"


@pytest.fixture
def jpath(tmp_path):
    rj._ACCEPT_SCAN_CACHE.clear()
    p = tmp_path / "run.jsonl"
    p.write_text("".join(_row(i) for i in range(5)))
    yield p
    rj._ACCEPT_SCAN_CACHE.clear()


def _check(p):
    with rj._lock_for(p):
        got = rj._accept_scan_locked(p)
    assert got == _full(p)
    return got


def _append(p, text):
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(text)


def test_append_between_calls_uses_cache(jpath):
    assert _check(jpath) == (False, False)
    _append(jpath, _row(5))
    assert _check(jpath) == (False, False)
    assert rj._ACCEPT_SCAN_CACHE[str(jpath)][2] == jpath.stat().st_size


def test_terminal_from_other_writer_instance(tmp_path):
    rj._ACCEPT_SCAN_CACHE.clear()
    w1 = rj.RunJournalWriter("sess1", "run1", session_dir=tmp_path)
    w2 = rj.RunJournalWriter("sess1", "run1", session_dir=tmp_path)
    ok = w1.accept_and_append_if_nonterminal("steer", {"x": 1}, lambda: True)
    assert ok[0] is True and ok[2] is None
    w2.append_sse_event("done", {})
    assert _check(w1._path)[1] is True
    res = w1.accept_and_append_if_nonterminal("steer", {"x": 2}, lambda: True)
    assert res[:3] == (False, None, "terminal")


def test_truncation_falls_back(jpath):
    _check(jpath)
    jpath.write_text(_row(0))
    assert _check(jpath) == (False, False)
    jpath.write_text(_row(0, terminal=True))  # shrink to terminal
    assert _check(jpath) == (False, True)


def test_inode_replacement(jpath):
    _check(jpath)
    repl = jpath.with_name("new.jsonl")
    repl.write_text("".join(_row(i) for i in range(10)) + _row(10, terminal=True))
    os.replace(repl, jpath)
    assert _check(jpath) == (False, True)


def test_same_size_rewrite_of_committed_region(jpath):
    _check(jpath)
    data = jpath.read_bytes()
    jpath.write_bytes(data.replace(b'"terminal": false}\n', b'"terminal": 1}    \n', 1))
    assert _check(jpath) == (False, True)


def test_malformed_trailing_line(jpath):
    _check(jpath)
    _append(jpath, "{not json\n")
    assert _check(jpath) == (True, False)
    _append(jpath, _row(9))
    assert _check(jpath) == (True, False)


def test_partial_line_then_completion(jpath):
    _check(jpath)
    line = _row(5, terminal=True)
    _append(jpath, line[:10])
    assert _check(jpath) == (True, False)  # torn tail is malformed in full read too
    size_before = rj._ACCEPT_SCAN_CACHE[str(jpath)][2]
    assert size_before < jpath.stat().st_size
    _append(jpath, line[10:])
    assert _check(jpath) == (False, True)


def test_unterminated_valid_tail_and_blank_lines(jpath):
    _check(jpath)
    _append(jpath, "\n  \n" + _row(6, terminal=True).rstrip("\n"))
    assert _check(jpath) == (False, True)


def test_missing_file(tmp_path):
    rj._ACCEPT_SCAN_CACHE.clear()
    assert _check(tmp_path / "absent.jsonl") == (False, False)


def test_cache_bounded(tmp_path):
    rj._ACCEPT_SCAN_CACHE.clear()
    for i in range(rj._ACCEPT_SCAN_CACHE_MAX + 10):
        p = tmp_path / f"r{i}.jsonl"
        p.write_text(_row(0))
        _check(p)
    assert len(rj._ACCEPT_SCAN_CACHE) <= rj._ACCEPT_SCAN_CACHE_MAX
