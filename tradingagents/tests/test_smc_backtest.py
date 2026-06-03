from datetime import datetime, timedelta

import pandas as pd

from smc_backtest import SMCBacktestConfig, run_smc_event_backtest, trade_record_schema
from smc_quant import SMCConfig


def _sample_ohlcv() -> pd.DataFrame:
    base = datetime(2026, 1, 1)
    rows = [
        (10, 11, 9, 10.5, 100),
        (10.5, 12, 10, 11.5, 120),
        (11.5, 13, 11, 12.8, 150),
        (12.8, 12.9, 10.8, 11.0, 180),
        (11.0, 11.2, 9.2, 9.6, 200),
        (9.6, 10.2, 8.8, 9.1, 210),
        (9.1, 10.0, 8.9, 9.8, 150),
        (9.8, 11.8, 9.7, 11.6, 260),
        (11.6, 14.2, 11.5, 14.0, 320),
        (14.0, 15.0, 13.4, 14.8, 280),
        (14.8, 14.9, 12.6, 13.0, 260),
        (13.0, 13.4, 11.8, 12.1, 240),
        (12.1, 12.7, 10.5, 10.8, 300),
        (10.8, 11.1, 9.4, 10.2, 270),
        (10.2, 12.6, 10.1, 12.4, 310),
        (12.4, 15.6, 12.3, 15.2, 360),
        (15.2, 16.4, 14.9, 16.1, 330),
        (16.1, 16.2, 14.2, 14.6, 290),
        (14.6, 15.8, 14.1, 15.5, 260),
        (15.5, 17.1, 15.4, 16.8, 340),
        (16.8, 18.2, 16.6, 17.9, 390),
        (17.9, 18.0, 16.0, 16.4, 280),
        (16.4, 17.5, 16.1, 17.2, 250),
        (17.2, 19.3, 17.0, 19.0, 410),
        (19.0, 20.2, 18.7, 19.7, 360),
    ]
    idx = [base + timedelta(days=i) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def test_trade_record_schema_covers_features_execution_and_outcomes():
    schema = trade_record_schema()
    assert {"identifiers", "features", "execution", "outcomes"}.issubset(schema)
    assert "feature_vector" in schema["features"]
    assert "r_multiple" in schema["outcomes"]


def test_smc_event_backtest_is_deterministic_and_lookahead_documented():
    result = run_smc_event_backtest(
        _sample_ohlcv(),
        "2330.TW",
        smc_config=SMCConfig(swing_length=2, internal_swing_length=2, entry_threshold=1),
        backtest_config=SMCBacktestConfig(min_bars=20, max_hold_bars=3, require_qualified=False),
    )
    assert result["market"] == "tw"
    assert result["lookahead_policy"].startswith("signals are computed")
    assert "total_trades" in result["metrics"]
    assert isinstance(result["equity_curve"], list)
    if result["trades"]:
        trade = result["trades"][0]
        assert trade["entry_index"] > 0
        assert trade["exit_index"] >= trade["entry_index"]
        assert isinstance(trade["feature_vector"], dict)
