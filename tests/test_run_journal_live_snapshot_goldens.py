"""Golden digests for ``_run_journal_live_snapshot`` (live-turn snapshot fold).

Each case writes a SYNTHETIC run journal into ``tmp_path`` (never the real
``~/.hermes``), folds it through the normal read path and compares the sha256
of the canonical JSON snapshot against a hardcoded golden.  A refactor of the
fold must keep every digest byte-identical.

To regenerate after an *intentional* output change::

    HERMES_PRINT_GOLDENS=1 pytest tests/test_run_journal_live_snapshot_goldens.py -s
"""

import hashlib
import json
import os

import pytest

import api.routes as routes
from api import run_journal as RJ

SESSION_ID = "session_golden"


# ── fixture builder ─────────────────────────────────────────────────────────

class JournalBuilder:
    """Accumulates deterministic journal rows (seq/created_at are synthetic)."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.rows: list[str] = []
        self.seq = 0

    def add(self, event: str, payload: dict | None = None) -> "JournalBuilder":
        self.seq += 1
        self.rows.append(json.dumps({
            "version": 1,
            "event_id": f"{self.run_id}:{self.seq}",
            "seq": self.seq,
            "run_id": self.run_id,
            "session_id": SESSION_ID,
            "event": event,
            "type": event,
            "created_at": 1_700_000_000 + self.seq,
            "payload": payload or {},
        }, sort_keys=True))
        return self

    def raw(self, line: str) -> "JournalBuilder":
        self.rows.append(line)
        return self

    def text(self) -> str:
        return "\n".join(self.rows) + "\n"


def _case_reasoning_only(b):
    for chunk in ["Let me ", "think about ", "this carefully", ".\n", "Step 2."]:
        b.add("reasoning", {"text": chunk})


def _case_interim_echo(b):
    for chunk in ["I will check", " the   config\nfile first."]:
        b.add("reasoning", {"text": chunk})
    b.add("interim_assistant", {"text": "  I will  check the config\n\n file first.  "})
    b.add("token", {"text": "Done checking."})
    b.add("interim_assistant", {"text": "Done checking.", "already_streamed": True})
    b.add("interim_assistant", {"text": "Second segment.", "reasoning_echo": True})


def _case_tools(b):
    b.add("token", {"text": "Running tools."})
    b.add("tool", {"name": "terminal", "tool_call_id": "call_1",
                   "preview": "ls -la", "args": {"command": "ls -la"}})
    b.add("tool", {"name": "clarify", "tool_call_id": "call_x"})
    b.add("tool", {"name": "read_file", "tool_call_id": "call_2", "args": {"path": "a.py"}})
    b.add("tool_complete", {"name": "terminal", "tool_call_id": "call_1",
                            "preview": "total 0", "duration": 0.5})
    b.add("tool_complete", {"name": "read_file", "args": {"path": "a.py", "limit": 10}})
    b.add("token", {"text": " All done."})


def _case_metering(b):
    for i in range(6):
        b.add("metering", {"input_tokens": i * 10, "output_tokens": i})
        b.add("token", {"text": f"t{i} "})
        if i == 3:
            b.add("reasoning", {"text": "mid-thought"})
    b.add("metering", {"input_tokens": 99})


def _case_steer(b):
    b.add("token", {"text": "Working on A."})
    b.add("steer", {"text": "Actually do B instead"})
    b.add("interim_assistant", {"text": "Switching to B."})
    b.add("steer_applied", {"text": "Actually do B instead"})
    b.add("token", {"text": "B is done."})


def _case_terminal_success(b):
    b.add("reasoning", {"text": "plan"})
    b.add("token", {"text": "Final answer."})
    b.add("done", {"status": "completed"})


def _case_terminal_error(b):
    b.add("token", {"text": "Partial"})
    b.add("tool", {"name": "web_search", "tool_call_id": "call_e"})
    b.add("error", {"message": "provider exploded", "status": "error"})


def _case_malformed_tail(b):
    b.add("token", {"text": "Before the tear."})
    b.raw('{"version": 1, "seq": 99, "event": "token", "payload": {"text": "torn')


def _case_rows_after_terminal(b):
    b.add("token", {"text": "Answer."})
    b.add("done", {"status": "completed"})
    b.add("token", {"text": " late token"})
    b.add("tool", {"name": "terminal", "tool_call_id": "call_late"})


def _case_large_mixed(b):
    for i in range(5000):
        kind = i % 10
        if kind in (0, 1, 2):
            b.add("metering", {"output_tokens": i})
        elif kind in (3, 4):
            b.add("reasoning", {"text": f"r{i} "})
        elif kind == 5:
            b.add("token", {"text": f"w{i} "})
        elif kind == 6 and i % 100 == 6:
            b.add("interim_assistant", {"text": f"Interim {i}"})
        elif kind == 7 and i % 50 == 7:
            b.add("tool", {"name": "terminal", "tool_call_id": f"c{i}", "args": {"n": i}})
        elif kind == 8 and i % 50 == 8:
            b.add("tool_complete", {"name": "terminal", "tool_call_id": f"c{i - 1}",
                                    "preview": f"out {i}"})
        else:
            b.add("metering", {"tick": i})


CASES = {
    "reasoning_only": _case_reasoning_only,
    "interim_echo": _case_interim_echo,
    "tools": _case_tools,
    "metering_interleaved": _case_metering,
    "steer": _case_steer,
    "terminal_success": _case_terminal_success,
    "terminal_error": _case_terminal_error,
    "malformed_tail": _case_malformed_tail,
    "rows_after_terminal": _case_rows_after_terminal,
    "large_mixed": _case_large_mixed,
}

GOLDENS = {
    "interim_echo": "110ae189265f5e9f6e1a22c0e401825152febbdbea97608e82ec8794e1fa5d2c",
    "large_mixed": "a66e0b0f2cccfdc5899404ef6fdf32892e66f045d42968363e18bf363f5b0100",
    "malformed_tail": "143f7f3402b858f100ef42d931c16d886e7ba1e46bc1ef0baed50a9cd69cb01d",
    "metering_interleaved": "462cd208bc83787b50459ab8aace0e63070fd46f76bd7a716deec6a6a2a3cde2",
    "reasoning_only": "8c8acab44c4451508f86d696fe29fbe0c114c8e63044343c12b3ab18bf28b328",
    "rows_after_terminal": "b2d69d554e79185b8682ff6aa11be8061aadf421496774bf21bf1d3f067cece6",
    "steer": "dba03190b11e37f721bf00b7ab0ad119af38a3b3d551b70988eb4520f2cd0d89",
    "terminal_error": "e1c38c3ca444792d1c670feff160d6302357af935df7632b073bff9ab8ff4753",
    "terminal_success": "288588a46d1d7392fc156c118d9066fd8df9afb625ec7679329d1542c7e84028",
    "tools": "026a5f76d4091b28636af2615a5955f9fa532931857801fb3a570b2820e88e7c",
}


# ── helpers ─────────────────────────────────────────────────────────────────

def build_journal(name: str) -> JournalBuilder:
    b = JournalBuilder(f"run_{name}")
    CASES[name](b)
    return b


def write_journal_text(root, run_id: str, text: str):
    session_root = root / RJ.RUN_JOURNAL_DIR_NAME / SESSION_ID
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"{run_id}.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def canonical_digest(snapshot) -> str:
    blob = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def snapshot_for_text(root, run_id: str, text: str):
    write_journal_text(root, run_id, text)
    return routes._run_journal_live_snapshot(run_id)


def iter_prefix_cases(names=None, cut_points=(0.25, 0.5, 0.75)):
    """Yield ``(name, run_id, prefix_text, suffix_text, whole_text)``.

    Splits each case on row boundaries.  Intended for a later
    resumable-fold PR to assert fold(prefix)+resume(suffix) == fold(whole);
    no incremental logic lives here.
    """
    for name in names or CASES:
        b = build_journal(name)
        rows = b.rows
        for frac in cut_points:
            cut = max(1, int(len(rows) * frac))
            prefix = "\n".join(rows[:cut]) + "\n"
            suffix = "\n".join(rows[cut:]) + ("\n" if rows[cut:] else "")
            yield name, b.run_id, prefix, suffix, b.text()


def assert_prefix_suffix_equals_whole(root, resume_fold):
    """Hook for the resumable-fold PR: ``resume_fold(prefix_snapshot_state,
    suffix_text)`` must reproduce the whole-file digest.  Unused today."""
    for name, run_id, prefix, suffix, whole in iter_prefix_cases():
        expected = canonical_digest(snapshot_for_text(root, run_id, whole))
        got = canonical_digest(resume_fold(root, run_id, prefix, suffix))
        assert got == expected, f"{name}: prefix+suffix fold diverged"


@pytest.fixture
def journal_root(tmp_path, monkeypatch):
    monkeypatch.setattr(RJ, "_default_session_dir", lambda: tmp_path)
    return tmp_path


# ── tests ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(CASES))
def test_live_snapshot_golden_digest(journal_root, name):
    b = build_journal(name)
    snapshot = snapshot_for_text(journal_root, b.run_id, b.text())
    digest = canonical_digest(snapshot)
    if os.environ.get("HERMES_PRINT_GOLDENS"):
        print(f'GOLDEN    "{name}": "{digest}",')
    assert digest == GOLDENS.get(name), f"{name} snapshot digest changed"


def test_live_snapshot_deterministic_and_prefix_cases(journal_root):
    for name in CASES:
        b = build_journal(name)
        first = canonical_digest(snapshot_for_text(journal_root, b.run_id, b.text()))
        second = canonical_digest(snapshot_for_text(journal_root, b.run_id, b.text()))
        assert first == second, f"{name} snapshot is non-deterministic"
        # Normal read path: the file on disk round-trips through read_run_events.
        events = RJ.read_run_events(SESSION_ID, b.run_id).get("events") or []
        assert events, name
    cases = list(iter_prefix_cases())
    assert len(cases) == 3 * len(CASES)
    for name, _run_id, prefix, suffix, whole in cases:
        assert prefix + suffix == whole, name
