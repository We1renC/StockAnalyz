"""Tests for official Taiwan daily fallback and portfolio backtest."""
import json
from unittest.mock import patch

import pandas as pd

import app  # type: ignore


def _fake_resp(payload):
    class Resp:
        def read(self):
            return json.dumps(payload).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    return Resp()


def test_fetch_twse_daily_history_parses_stock_day_rows():
    payload = {
        "data": [
            ["113/05/02", "1,000", "20,000", "20.00", "20.50", "19.80", "20.30", "+0.30", "10"],
            ["113/05/03", "2,000", "41,000", "20.30", "21.00", "20.20", "20.80", "+0.50", "20"],
        ]
    }
    with patch.object(app, "_month_starts", return_value=[pd.Timestamp("2024-05-01").date()]), \
         patch.object(app, "urlopen", return_value=_fake_resp(payload)):
        h = app.fetch_twse_daily_history("2330.TW", months=1)

    assert len(h) == 2
    assert h["Close"].iloc[-1] == 20.8
    assert h.attrs["source"] == "twse_daily"


def test_backtest_position_uses_history_metrics():
    h = pd.DataFrame({
        "Open": [10.0, 11.0, 12.0],
        "High": [11.0, 12.0, 13.0],
        "Low": [9.0, 10.0, 11.0],
        "Close": [10.0, 12.0, 11.0],
        "Volume": [1000, 1000, 1000],
    }, index=pd.to_datetime(["2026-03-01", "2026-04-01", "2026-05-01"]))
    with patch.object(app, "fetch_history", return_value=(h, "test_history")):
        result = app._run_backtest_for_position({
            "symbol": "TEST.TW",
            "name": "測試股",
            "shares": 100,
            "cost_price": 9.0,
            "currency": "TWD",
        }, months=6)

    assert result["source"] == "test_history"
    assert result["period_return_pct"] == 10.0
    assert result["position_pnl"] == 200
    assert result["buy_hold_pnl"] == 100


def test_api_smc_backtest_uses_history_and_returns_metrics():
    h = pd.DataFrame({
        "Open": [10, 10.5, 11.5, 12.8, 11, 9.6, 9.1, 9.8, 11.6, 14, 14.8, 13, 12.1, 10.8, 10.2, 12.4, 15.2, 16.1, 14.6, 15.5, 16.8, 17.9, 16.4, 17.2, 19],
        "High": [11, 12, 13, 12.9, 11.2, 10.2, 10, 11.8, 14.2, 15, 14.9, 13.4, 12.7, 11.1, 12.6, 15.6, 16.4, 16.2, 15.8, 17.1, 18.2, 18, 17.5, 19.3, 20.2],
        "Low": [9, 10, 11, 10.8, 9.2, 8.8, 8.9, 9.7, 11.5, 13.4, 12.6, 11.8, 10.5, 9.4, 10.1, 12.3, 14.9, 14.2, 14.1, 15.4, 16.6, 16, 16.1, 17, 18.7],
        "Close": [10.5, 11.5, 12.8, 11, 9.6, 9.1, 9.8, 11.6, 14, 14.8, 13, 12.1, 10.8, 10.2, 12.4, 15.2, 16.1, 14.6, 15.5, 16.8, 17.9, 16.4, 17.2, 19, 19.7],
        "Volume": [100] * 25,
    }, index=pd.date_range("2026-01-01", periods=25, freq="D"))
    with patch.object(app, "fetch_history", return_value=(h, "test_history")):
        result = app.api_smc_backtest("TEST.TW", period="1y", min_bars=20, entry_threshold=1, require_qualified=False)

    assert result["source"] == "test_history"
    assert result["market"] == "tw"
    assert "metrics" in result
    assert result["lookahead_policy"].startswith("signals are computed")
