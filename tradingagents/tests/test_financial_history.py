"""Tests for historical financial reports (yfinance structured API, not scraping)."""
import json
from datetime import date

import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import app


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "fin.db")
    app.init_db()
    return original


def _fake_income_stmt():
    # columns = quarter end dates (newest first), rows = line items
    cols = pd.to_datetime(["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30",
                           "2025-03-31", "2024-12-31"])
    data = {
        "Total Revenue":    [1100, 1400, 1000, 950, 940, 1200],
        "Net Income":       [300, 420, 270, 230, 250, 350],
        "Diluted EPS":      [2.0, 2.8, 1.85, 1.5, 1.6, 2.4],
        "Gross Profit":     [540, 690, 480, 430, 450, 600],
        "Operating Income": [350, 500, 320, 280, 290, 450],
    }
    return pd.DataFrame(data, index=cols).T  # index=line items, columns=quarters


def test_fetch_financial_history_normalizes(monkeypatch):
    fake_ticker = MagicMock()
    fake_ticker.quarterly_income_stmt = _fake_income_stmt()
    with patch.object(app.yf, "Ticker", return_value=fake_ticker):
        app._FINANCIALS_CACHE.clear()
        recs = app.fetch_financial_history("TEST")
    assert len(recs) >= 5
    latest = recs[0]
    assert latest["period"] == "2026-03-31"
    assert latest["revenue"] == 1100
    assert latest["eps"] == 2.0
    assert latest["gross_margin"] == round(540 / 1100 * 100, 1)
    # YoY for 2026-03-31 vs 2025-03-31 (940 rev): (1100/940 - 1)*100
    assert latest["revenue_yoy"] == round((1100 / 940 - 1) * 100, 1)


def test_fetch_financial_history_empty_for_etf():
    fake_ticker = MagicMock()
    fake_ticker.quarterly_income_stmt = pd.DataFrame()  # ETF → empty
    with patch.object(app.yf, "Ticker", return_value=fake_ticker):
        app._FINANCIALS_CACHE.clear()
        recs = app.fetch_financial_history("00981A.TW")
    assert recs == []


def test_store_and_load_financial_reports(tmp_path):
    original = _temp_db(tmp_path)
    try:
        recs = [
            {"period": "2026-03-31", "revenue": 1100, "eps": 2.0, "revenue_yoy": 17.0},
            {"period": "2025-12-31", "revenue": 1400, "eps": 2.8},
        ]
        n = app._store_financial_reports("AAPL", recs)
        assert n == 2
        loaded = app._load_financial_reports("AAPL", limit=8)
        assert len(loaded) == 2
        # newest first
        assert loaded[0]["period"] == "2026-03-31"
    finally:
        app.DB = original


def test_financial_reports_immutable_on_restore(tmp_path):
    original = _temp_db(tmp_path)
    try:
        app._store_financial_reports("AAPL", [{"period": "2026-03-31", "revenue": 1100}])
        # Re-store same period with same data (a reported quarter is final)
        app._store_financial_reports("AAPL", [{"period": "2026-03-31", "revenue": 1100}])
        conn = app.get_db()
        rows = conn.execute("SELECT COUNT(*) AS c FROM financial_reports WHERE symbol='AAPL'").fetchone()
        conn.close()
        assert rows["c"] == 1  # no duplicate
    finally:
        app.DB = original


def test_financials_history_text():
    recs = [
        {"period": "2026-03-31", "revenue": 1.1e11, "revenue_yoy": 16.6, "eps": 2.0,
         "eps_yoy": 21.8, "gross_margin": 49.3, "net_margin": 26.6},
    ]
    text = app._build_financials_history_text("AAPL", records=recs)
    assert "歷史財報趨勢" in text
    assert "YoY +16.6%" in text
    assert "EPS 2.0" in text


def test_financials_history_text_empty():
    assert app._build_financials_history_text("ETF", records=[]) == ""


def test_obsidian_financials_note(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    recs = [{"period": "2026-03-31", "revenue": 1.1e11, "revenue_yoy": 16.6, "eps": 2.0,
             "eps_yoy": 21.8, "gross_margin": 49.3, "net_margin": 26.6}]
    app._obsidian_write_financials(vault, "AAPL", recs)
    note = vault / "Fundamentals" / "AAPL_財報歷史.md"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "type: financial-history" in text
    assert "2026-03-31" in text


def test_etf_fundamentals_enriched(monkeypatch):
    """ETF should get is_fund + etf block (category/assets/returns/holdings)."""
    fake_info = {
        "quoteType": "ETF", "category": "Large Blend", "fundFamily": "SSGA",
        "totalAssets": 7.35e11, "navPrice": 758.3, "yield": 0.0103,
        "ytdReturn": 5.68, "threeYearAverageReturn": 0.23, "fiveYearAverageReturn": 0.14,
        "beta3Year": 1.0, "legalType": "Exchange Traded Fund",
    }
    fake_ticker = MagicMock()
    fake_ticker.info = fake_info
    # top_holdings DataFrame
    th = pd.DataFrame(
        {"Name": ["NVIDIA", "Apple"], "Holding Percent": [0.078, 0.064]},
        index=["NVDA", "AAPL"],
    )
    fake_ticker.funds_data.top_holdings = th
    with patch.object(app.yf, "Ticker", return_value=fake_ticker):
        app._FUNDAMENTALS_CACHE.clear()
        f = app.fetch_fundamentals("SPY")
    assert f["is_fund"] is True
    assert f["etf"]["category"] == "Large Blend"
    assert f["etf"]["top_holdings"][0]["symbol"] == "NVDA"
    assert f["etf"]["top_holdings"][0]["weight"] == 7.8


def test_etf_fundamentals_text_block():
    f = {
        "is_fund": True,
        "etf": {
            "category": "Large Blend", "fund_family": "SSGA", "total_assets": 7.35e11,
            "nav": 758.3, "yield": 0.0103, "ytd_return": 5.68,
            "three_year_return": 0.23, "five_year_return": 0.14, "beta_3y": 1.0,
            "top_holdings": [{"symbol": "NVDA", "name": "NVIDIA", "weight": 7.85}],
        },
    }
    text = app._build_fundamentals_text("SPY", 760.0, fundamentals=f)
    assert "ETF / 基金概況" in text
    assert "Large Blend" in text
    assert "前 5 大持股" in text
    assert "NVDA 7.85%" in text
