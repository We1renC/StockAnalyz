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
