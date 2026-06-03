"""Tests for CLI deep-analysis workflow SMC integration."""

import json
from unittest.mock import patch

import app


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "cli.db")
    app.init_db()
    return original


def test_cli_step_keys_include_smc_in_quick_and_full():
    quick = app._cli_step_keys("quick")
    full = app._cli_step_keys("full")
    assert "smc_report" in quick
    assert "smc_report" in full
    assert quick == [
        "market_report",
        "smc_report",
        "fundamentals_report",
        "investment_debate_state",
        "final_trade_decision",
    ]


def test_cli_prompts_reference_smc_report():
    prompts = {step["key"]: step["prompt"] for step in app._CLI_DEEP_STEPS}
    assert "SMC" in prompts["smc_report"]
    assert "{smc_report}" in prompts["investment_debate_state"]
    assert "{smc_report}" in prompts["risk_debate_state"]
    assert "{smc_report}" in prompts["final_trade_decision"]


def test_fetch_stock_context_prefers_rich_build_context():
    with patch.object(app, "_build_context", return_value={"context": "RICH-CONTEXT"}), \
         patch.object(app, "_fetch_stock_context_fallback", return_value="FALLBACK"):
        text = app._fetch_stock_context("AAPL")
    assert text == "RICH-CONTEXT"


def test_fetch_stock_context_falls_back_when_rich_context_missing():
    with patch.object(app, "_build_context", return_value={"error": "no cache"}), \
         patch.object(app, "_fetch_stock_context_fallback", return_value="FALLBACK"):
        text = app._fetch_stock_context("AAPL")
    assert text == "FALLBACK"


def test_augment_sections_with_smc_injects_when_missing():
    with patch.object(app, "_build_smc_text", return_value="【SMC 結構與回測】mock"):
        sections = app._augment_sections_with_smc("AAPL", {"market_report": "市場"})
    assert sections["market_report"] == "市場"
    assert sections["smc_report"] == "【SMC 結構與回測】mock"


def test_augment_sections_with_smc_preserves_existing_report():
    with patch.object(app, "_build_smc_text", return_value="SHOULD_NOT_USE"):
        sections = app._augment_sections_with_smc("AAPL", {"smc_report": "已有 SMC"})
    assert sections["smc_report"] == "已有 SMC"


def test_store_analysis_result_persists_smc_and_updates_watchlist(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        conn.execute(
            "INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,?,?)",
            ("AAPL", "Apple", "tech", "USD"),
        )
        conn.commit()
        conn.close()

        with patch.object(app, "_get_vault", return_value=None), \
             patch.object(app, "_build_smc_text", return_value="【SMC 結構與回測】mock"), \
             patch.object(app, "_build_smc_snapshot_payload", return_value={"available": True, "bias": "bullish"}):
            stored = app._store_analysis_result(
                symbol="AAPL",
                mode="tradingagents_full",
                provider="anthropic",
                model="claude-opus-4-7",
                elapsed=12.3,
                sections={"final_trade_decision": "1. 進場 100~105\n2. 停損 96\n3. 停利 118"},
                decision_text="1. 進場 100~105\n2. 停損 96\n3. 停利 118",
            )

        assert stored["decision_summary"].startswith("1. 進場")
        assert stored["sections"]["smc_report"] == "【SMC 結構與回測】mock"
        assert stored["sections"]["smc"]["bias"] == "bullish"
        assert stored["wrote_watchlist_levels"] is True

        conn = app.get_db()
        row = conn.execute("SELECT sections FROM analysis_results WHERE symbol='AAPL'").fetchone()
        watch = conn.execute(
            "SELECT target_entry, target_add, target_stop, target_profit FROM watchlist WHERE symbol='AAPL'"
        ).fetchone()
        conn.close()
        saved_sections = json.loads(row["sections"])
        assert saved_sections["smc_report"] == "【SMC 結構與回測】mock"
        assert saved_sections["smc"]["bias"] == "bullish"
        assert watch["target_entry"] == 100
        assert watch["target_add"] == 105
        assert watch["target_stop"] == 96
        assert watch["target_profit"] == 118
    finally:
        app.DB = original
