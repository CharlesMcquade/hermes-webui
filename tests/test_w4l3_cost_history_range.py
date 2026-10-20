"""S03: absolute start/end date ranges on /api/provider/cost-history.

Fork delta over upstream's relative-days API: the route accepts absolute
``start``/``end`` (YYYY-MM-DD, inclusive) and composes them with the existing
relative-days provider call — one atomic window per response, never mixed
authoritative/legacy buckets. Malformed ranges are rejected with 400 before
any provider call.
"""
from datetime import date, timedelta
from urllib.parse import urlparse

import pytest

from api import routes


def _request(query, monkeypatch, captured):
    def fake_history(provider_id, days=7):
        captured.append((provider_id, days))
        return {"ok": True, "provider": provider_id, "days": days, "snapshots": []}

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "get_provider_cost_history", fake_history)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kw: (status, payload))
    monkeypatch.setattr(routes, "bad", lambda handler, message, status=400: (status, {"error": message}))
    return routes.handle_get(
        types_simple_handler(),
        urlparse(f"/api/provider/cost-history?{query}"),
    )


class types_simple_handler:
    headers = {}

    def __getattr__(self, name):
        return lambda *a, **kw: None


def test_legacy_days_param_still_works(monkeypatch):
    captured = []
    status, payload = _request("provider=openrouter&days=14", monkeypatch, captured)
    assert status == 200
    assert captured == [("openrouter", 14)]
    assert payload["ok"] is True


def test_absolute_start_end_compose_to_inclusive_days_window(monkeypatch):
    captured = []
    status, payload = _request(
        "provider=openrouter&start=2026-10-01&end=2026-10-07", monkeypatch, captured)
    assert status == 200
    # Inclusive on both ends: Oct 1..Oct 7 = 7 days.
    assert captured == [("openrouter", 7)]
    assert payload["range"] == {"start": "2026-10-01", "end": "2026-10-07", "inclusive": True}
    assert payload["range_source"] == "absolute"


def test_absolute_range_wins_over_days(monkeypatch):
    captured = []
    _request("provider=openrouter&days=3&start=2026-10-01&end=2026-10-02",
             monkeypatch, captured)
    assert captured == [("openrouter", 2)]


def test_single_day_range(monkeypatch):
    captured = []
    _request("provider=openrouter&start=2026-10-19&end=2026-10-19", monkeypatch, captured)
    assert captured == [("openrouter", 1)]


@pytest.mark.parametrize("query", [
    "provider=openrouter&start=2026-10-01",            # end missing
    "provider=openrouter&end=2026-10-07",              # start missing
    "provider=openrouter&start=2026-10-1&end=2026-10-07",   # malformed start
    "provider=openrouter&start=2026-13-01&end=2026-10-07",  # impossible month
    "provider=openrouter&start=2026-10-07&end=2026-10-01",  # inverted range
    "provider=openrouter&start=2026-10-01&end=2026-10-07&start=2026-09-01",  # duplicate
])
def test_invalid_absolute_ranges_rejected_before_provider_call(monkeypatch, query):
    captured = []
    status, payload = _request(query, monkeypatch, captured)
    assert status == 400
    assert captured == []
    assert payload["error"]


def test_range_spanning_more_than_a_year_is_rejected(monkeypatch):
    captured = []
    end = date.today() - timedelta(days=30)
    start = end - timedelta(days=370)
    status, _payload = _request(
        f"provider=openrouter&start={start.isoformat()}&end={end.isoformat()}",
        monkeypatch, captured)
    assert status == 400
    assert captured == []


def test_future_end_clamped_to_today(monkeypatch):
    captured = []
    today = date.today()
    start = today - timedelta(days=4)
    status, payload = _request(
        f"provider=openrouter&start={start.isoformat()}&end=2099-01-01",
        monkeypatch, captured)
    assert status == 200
    assert captured == [("openrouter", 5)]  # start..today inclusive
    assert payload["range"]["end"] == today.isoformat()
    assert payload["clamped"] is True
