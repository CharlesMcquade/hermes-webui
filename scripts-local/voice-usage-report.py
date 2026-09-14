#!/usr/bin/env python3
"""Roll up live-voice (OpenAI Realtime) token usage for Hermes WebUI.

Reads two sources:
  1. ``$HERMES_HOME/state.db`` table ``session_model_usage``
     (rows with task='voice_realtime' — written when WebUI's
     ``sync_to_insights`` setting is on).
  2. ``$HERMES_WEBUI_STATE_DIR/voice_usage.jsonl`` — the per-response
     detail log, which additionally carries the audio/text token
     breakdown that state.db has no columns for.

Examples:
  python3 scripts-local/voice-usage-report.py              # all sessions, table
  python3 scripts-local/voice-usage-report.py --days 7     # last 7 days only
  python3 scripts-local/voice-usage-report.py --session <id> --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path


def _state_db_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home).expanduser() / "state.db"


def _jsonl_path() -> Path:
    base = os.environ.get("HERMES_WEBUI_STATE_DIR") or str(
        Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "webui"
    )
    return Path(base).expanduser() / "voice_usage.jsonl"


def _load_db_rows(min_ts: float | None) -> list[dict]:
    path = _state_db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT session_id, model, task, api_call_count, input_tokens, output_tokens, "
            "cache_read_tokens, reasoning_tokens, first_seen, last_seen "
            "FROM session_model_usage WHERE task='voice_realtime'"
        )
        params: tuple = ()
        if min_ts is not None:
            sql += " AND last_seen >= ?"
            params = (min_ts,)
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _load_jsonl_rows(min_ts: float | None) -> list[dict]:
    path = _jsonl_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            if min_ts is not None and isinstance(ts, (int, float)) and ts < min_ts:
                continue
            rows.append(row)
    return rows


def _empty() -> dict:
    return {
        "responses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
    }


def _acc(bucket: dict, **kw) -> None:
    for key, value in kw.items():
        bucket[key] += int(value or 0)


def aggregate(db_rows: list[dict], jsonl_rows: list[dict]) -> dict[str, dict]:
    """Per-session aggregate; prefers JSONL detail, falls back to db rows."""
    sessions: dict[str, dict] = defaultdict(_empty)
    seen_responses: set[str] = set()

    for row in jsonl_rows:
        sid = str(row.get("session_id") or "(unknown)")
        rid = str(row.get("response_id") or "")
        if rid and rid in seen_responses:
            continue  # log-level belt: server already dedupes
        if rid:
            seen_responses.add(rid)
        details_in = row.get("input_token_details") or {}
        details_out = row.get("output_token_details") or {}
        _acc(
            sessions[sid],
            responses=1,
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cached_tokens=row.get("cached_tokens"),
            reasoning_tokens=row.get("reasoning_tokens"),
            input_audio_tokens=details_in.get("audio_tokens"),
            output_audio_tokens=details_out.get("audio_tokens"),
        )

    covered = set(sessions)
    for row in db_rows:
        sid = row["session_id"]
        if sid in covered:
            continue  # db rows are a subset of the JSONL detail
        _acc(
            sessions[sid],
            responses=row.get("api_call_count"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cached_tokens=row.get("cache_read_tokens"),
            reasoning_tokens=row.get("reasoning_tokens"),
        )
    return dict(sessions)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="only this session id")
    ap.add_argument("--days", type=float, help="only usage from the last N days")
    ap.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    args = ap.parse_args()

    min_ts = (time.time() - args.days * 86400) if args.days else None
    db_rows = _load_db_rows(min_ts)
    jsonl_rows = _load_jsonl_rows(min_ts)

    if args.session:
        db_rows = [r for r in db_rows if r["session_id"] == args.session]
        jsonl_rows = [r for r in jsonl_rows if r.get("session_id") == args.session]

    sessions = aggregate(db_rows, jsonl_rows)

    if args.as_json:
        print(json.dumps({
            "sources": {
                "state_db": str(_state_db_path()),
                "detail_log": str(_jsonl_path()),
            },
            "sessions": dict(sorted(sessions.items())),
        }, indent=2, sort_keys=True))
        return 0

    if not sessions:
        print("No realtime voice usage recorded yet.")
        print(f"  state.db:   {_state_db_path()}")
        print(f"  detail log: {_jsonl_path()}")
        return 0

    totals = _empty()
    print(f"{'session':<22} {'resp':>5} {'in':>9} {'out':>9} {'cached':>9} {'aud-in':>9} {'aud-out':>9}")
    for sid in sorted(sessions):
        s = sessions[sid]
        _acc(
            totals,
            responses=s["responses"], input_tokens=s["input_tokens"], output_tokens=s["output_tokens"],
            cached_tokens=s["cached_tokens"], input_audio_tokens=s["input_audio_tokens"],
            output_audio_tokens=s["output_audio_tokens"],
        )
        print(
            f"{sid:<22} {s['responses']:>5} {s['input_tokens']:>9,} {s['output_tokens']:>9,} "
            f"{s['cached_tokens']:>9,} {s['input_audio_tokens']:>9,} {s['output_audio_tokens']:>9,}"
        )
    print("-" * 74)
    print(
        f"{'TOTAL':<22} {totals['responses']:>5} {totals['input_tokens']:>9,} "
        f"{totals['output_tokens']:>9,} {totals['cached_tokens']:>9,} "
        f"{totals['input_audio_tokens']:>9,} {totals['output_audio_tokens']:>9,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
