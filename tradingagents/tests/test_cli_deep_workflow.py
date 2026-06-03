"""Tests for CLI deep-analysis workflow SMC integration."""

from unittest.mock import patch

import app


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
