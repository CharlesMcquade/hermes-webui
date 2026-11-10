from api.run_journal import RunJournalWriter, _read_jsonl


def test_metering_publishes_live_without_journal_row(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)
    published = []

    first = writer.append_and_publish_sse_event("token", {"text": "a"}, published.append)
    meter = writer.append_and_publish_sse_event("metering", {"tokens": 3}, published.append)
    second = writer.append_and_publish_sse_event("token", {"text": "b"}, published.append)

    assert meter is None
    assert len(published) == 3
    assert published[1] is None  # non-journaled envelope: no seq / event_id
    assert published[0] is first and published[2] is second

    rows, malformed = _read_jsonl(writer._path)
    assert not malformed
    assert [r["event"] for r in rows] == ["token", "token"]
    assert second["seq"] == first["seq"] + 1
    assert first["event"] == "token" and first["event_id"]
    assert first["payload"] == {"text": "a"}
