"""Retirement guards for unrelated #7497 emoji and #7498 cost overlays.

Content width and readable Markdown tables are intentional local features with
their own regression coverage in test_content_width_and_table_readability.py.
These assertions target only the retired emoji bundle and exact-cost machinery,
not generic Markdown tables, font typography, provider cost dashboards, or
Insights date windows that upstream may provide independently.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def test_7497_emoji_overlay_stays_retired():
    sources = {
        name: (STATIC / name).read_text(encoding="utf-8")
        for name in (
            "boot.js",
            "embed-host.js",
            "index.html",
            "messages.js",
            "panels.js",
            "style.css",
            "terminal.js",
        )
    }
    combined = "\n".join(sources.values())
    assert "noto-color-emoji" not in combined
    assert "--font-emoji" not in combined
    assert not (STATIC / "vendor" / "noto-color-emoji").exists()


def test_7498_exact_agent_cost_reconciliation_stays_retired():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    assert "def _insights_agent_model_usage(" not in routes
    assert "population_session_ids" not in routes
    assert "webui_fallback_by_id" not in routes
    assert "Agent model ledger" not in routes
    assert not (
        ROOT / "docs" / "fork-changes" / "authoritative-insights-model-cost.md"
    ).exists()
