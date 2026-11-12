#!/usr/bin/env python3
"""Synthetic, isolated baseline for long-session WebUI payload construction."""
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

RUNS = 5
BODY_BYTES = 4096
LOREM = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
         "tempor incididunt ut labore et dolore magna aliqua " * 80)


class _GetHandler:
    """Same in-process route-test double as test_webui_state_db_reconciliation."""
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    @property
    def query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, *args, **kwargs):
        pass


def _make_state_db(path: Path, sid: str, rows):
    """Fixture writer copied from test_webui_state_db_reconciliation."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Reconcile", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        tool_calls = row.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, str):
            tool_calls = json.dumps(tool_calls, ensure_ascii=False)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_calls, tool_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_call_id"),
                tool_calls,
                row.get("tool_name"),
            ),
        )
    conn.commit()
    conn.close()


def _content(index):
    prefix = f"synthetic message {index:05d}: "
    return (prefix + LOREM)[:BODY_BYTES]


def _messages(count, turns):
    rows, call_number, last_call = [], 0, None
    for turn in range(turns):
        start, end = turn * count // turns, (turn + 1) * count // turns
        for index in range(start, end):
            row = {"content": _content(index), "timestamp": 1000.0 + index / 100}
            if index == start:
                row["role"] = "user"
            elif (index - start) % 2:
                call_number += 1
                last_call = f"call_{call_number:05d}"
                row.update(role="assistant", tool_calls=[{
                    "id": last_call, "type": "function",
                    "function": {"name": "synthetic_lookup", "arguments": "{\"query\":\"lorem\"}"},
                }])
            else:
                row.update(role="tool", tool_call_id=last_call, tool_name="synthetic_lookup")
            rows.append(row)
    assert len(rows) == count
    assert sum(row["role"] == "user" for row in rows) == turns
    return rows


def _is_within(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _stats(values):
    return statistics.median(values), max(values)


def _fmt(values):
    p50, high = _stats(values)
    return f"{p50:.1f} / {high:.1f}"


def _mib(size):
    return f"{size / (1024 * 1024):.2f}"


def main():
    production_home = Path.home().resolve() / ".hermes"
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    with tempfile.TemporaryDirectory(prefix="ls-m1-", dir="/tmp") as temporary:
        state = Path(temporary).resolve()
        assert not _is_within(state, production_home)
        os.environ.update({
            "HOME": str(state), "TMPDIR": "/tmp",
            "HERMES_HOME": str(state), "HERMES_BASE_HOME": str(state),
            "HERMES_WEBUI_STATE_DIR": str(state),
            "HERMES_WEBUI_TEST_STATE_DIR": str(state),
            "HERMES_WEBUI_DEFAULT_WORKSPACE": str(state / "workspace"),
            "HERMES_CONFIG_PATH": str(state / "config.yaml"),
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
        })
        for key in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL", "HERMES_WEBUI_PASSWORD"):
            os.environ.pop(key, None)

        import api.config as config
        import api.models as models
        import api.profiles as profiles
        import api.routes as routes
        import api.streaming as streaming
        from api.helpers import _json_response_body

        session_dir = state / "sessions"
        config.STATE_DIR = state
        config.SESSION_DIR = session_dir
        config.SESSION_INDEX_FILE = session_dir / "_index.json"
        models.SESSION_DIR = session_dir
        models.SESSION_INDEX_FILE = session_dir / "_index.json"
        models.SESSIONS = OrderedDict()
        routes.SESSIONS = models.SESSIONS
        profiles.get_active_hermes_home = lambda: state
        models._active_state_db_path = lambda: state / "state.db"
        routes._active_state_db_path = lambda: state / "state.db"
        session_dir.mkdir(parents=True, exist_ok=True)
        assert _is_within(models.SESSION_DIR, state)
        assert _is_within(models._active_state_db_path(), state)

        def install_session(sid, messages):
            session = models.Session(
                session_id=sid, title="Reconcile", workspace=str(state),
                model="test-model", messages=messages,
                created_at=1000.0, updated_at=1001.0,
            )
            session.save(touch_updated_at=False)
            models.SESSIONS.pop(sid, None)
            return session

        def get_once(sid, limit):
            handler = _GetHandler(
                f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit={limit}"
            )
            started = time.perf_counter()
            routes.handle_get(handler, urlparse(handler.path))
            elapsed = (time.perf_counter() - started) * 1000
            assert handler.status == 200, handler.wfile.getvalue()[:500]
            return elapsed, len(handler.wfile.getvalue())

        def done_once(session):
            started = time.perf_counter()
            payload = streaming._session_payload_with_full_messages(session)
            body = _json_response_body(payload, pretty=False)
            return (time.perf_counter() - started) * 1000, len(body)

        shapes = [(500, 1), (2000, 1), (5000, 1), (5000, 40)]
        print("Times are milliseconds, p50 / max over 5 runs; cold is one first call. JSON sizes are uncompressed MiB.\n")
        print("| Messages | Turns | 30 cold | 30 warm p50/max | all p50/max | done p50/max | 30 MiB | all MiB | done MiB |")
        print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for count, turns in shapes:
            sid = f"bench_{count}_{turns}"
            rows = _messages(count, turns)
            session = install_session(sid, rows)
            _make_state_db(state / "state.db", sid, rows)
            cold_ms, tail_bytes = get_once(sid, 30)
            tail_runs = [get_once(sid, 30)[0] for _ in range(RUNS)]
            all_results = [get_once(sid, "all") for _ in range(RUNS)]
            done_results = [done_once(session) for _ in range(RUNS)]
            print(f"| {count} | {turns} | {cold_ms:.1f} | {_fmt(tail_runs)} | "
                  f"{_fmt([r[0] for r in all_results])} | {_fmt([r[0] for r in done_results])} | "
                  f"{_mib(tail_bytes)} | {_mib(all_results[-1][1])} | {_mib(done_results[-1][1])} |")
            (state / "state.db").unlink()


if __name__ == "__main__":
    main()
