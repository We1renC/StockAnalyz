from unittest.mock import patch

import pandas as pd

import app
from smc_store import persist_backtest_run, summarize_backtest_report


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "smc.db")
    app.init_db()
    return original


def _sample_backtest_result(symbol="AAA"):
    return {
        "symbol": symbol,
        "market": "us",
        "timeframe": "6mo",
        "generated_at": "2026-06-04T09:00:00",
        "bars": 120,
        "metrics": {
            "total_trades": 2,
            "wins": 1,
            "losses": 1,
            "win_rate": 0.5,
            "profit_factor": 1.4,
            "expectancy_r": 0.2,
            "max_drawdown": -500.0,
            "ending_equity": 101000.0,
        },
        "trades": [
            {
                "trade_id": f"{symbol}-1",
                "symbol": symbol,
                "market": "us",
                "timeframe": "6mo",
                "direction": "long",
                "model": "OTE Retracement",
                "entry_time": "2026-06-01T09:30:00",
                "exit_time": "2026-06-02T09:30:00",
                "entry": 100.0,
                "exit": 105.0,
                "stop": 95.0,
                "tp1": 105.0,
                "qty": 10,
                "pnl": 50.0,
                "r_multiple": 1.0,
                "score": 9,
                "threshold": 8,
                "feature_vector": {"ote_zone": True},
                "dol_target": {"type": "PDH", "level": 105.0},
                "exit_reason": "tp1",
                "holding_bars": 3,
                "win": True,
            },
            {
                "trade_id": f"{symbol}-2",
                "symbol": symbol,
                "market": "us",
                "timeframe": "6mo",
                "direction": "short",
                "model": "Sweep + CHoCH",
                "entry_time": "2026-06-03T09:30:00",
                "exit_time": "2026-06-04T09:30:00",
                "entry": 103.0,
                "exit": 106.0,
                "stop": 106.0,
                "tp1": 97.0,
                "qty": 10,
                "pnl": -30.0,
                "r_multiple": -1.0,
                "score": 8,
                "threshold": 8,
                "feature_vector": {"liquidity_sweep": True},
                "dol_target": {"type": "SSL", "level": 97.0},
                "exit_reason": "stop",
                "holding_bars": 2,
                "win": False,
            },
        ],
    }


def _sample_ohlcv():
    rows = {
        "Open": [10, 10.5, 11.5, 12.8, 11, 9.6, 9.1, 9.8, 11.6, 14, 14.8, 13, 12.1, 10.8, 10.2, 12.4, 15.2, 16.1, 14.6, 15.5, 16.8, 17.9, 16.4, 17.2, 19],
        "High": [11, 12, 13, 12.9, 11.2, 10.2, 10, 11.8, 14.2, 15, 14.9, 13.4, 12.7, 11.1, 12.6, 15.6, 16.4, 16.2, 15.8, 17.1, 18.2, 18, 17.5, 19.3, 20.2],
        "Low": [9, 10, 11, 10.8, 9.2, 8.8, 8.9, 9.7, 11.5, 13.4, 12.6, 11.8, 10.5, 9.4, 10.1, 12.3, 14.9, 14.2, 14.1, 15.4, 16.6, 16, 16.1, 17, 18.7],
        "Close": [10.5, 11.5, 12.8, 11, 9.6, 9.1, 9.8, 11.6, 14, 14.8, 13, 12.1, 10.8, 10.2, 12.4, 15.2, 16.1, 14.6, 15.5, 16.8, 17.9, 16.4, 17.2, 19, 19.7],
        "Volume": [100] * 25,
    }
    return pd.DataFrame(rows, index=pd.date_range("2026-01-01", periods=25, freq="D"))


def test_persist_and_summarize_backtest_runs(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        run_id = persist_backtest_run(conn, _sample_backtest_result("AAA"), period="6mo", source="test")
        conn.close()
        assert run_id > 0

        conn = app.get_db()
        report = summarize_backtest_report(conn, symbol="AAA")
        conn.close()
        assert report["run_count"] == 1
        assert report["trade_count"] == 2
        assert report["symbols"][0]["symbol"] == "AAA"
        assert report["symbols"][0]["expectancy_r"] == 0.0
    finally:
        app.DB = original


def test_api_smc_backtest_store_persists_rows(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "fetch_history", return_value=(_sample_ohlcv(), "test_history")):
            result = app.api_smc_backtest_store("TESTA", period="1y", min_bars=20, entry_threshold=1, require_qualified=False)
        assert result["ok"] is True

        conn = app.get_db()
        run = conn.execute("SELECT symbol, source FROM smc_backtest_runs WHERE id=?", (result["run_id"],)).fetchone()
        conn.close()
        assert run is not None
        assert run["symbol"] == "TESTA"
        assert run["source"] == "test_history"
    finally:
        app.DB = original


def test_api_smc_backtest_batch_stores_and_ranks_results(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        conn.execute("INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,?,?)", ("AAA", "AAA", "x", "USD"))
        conn.execute("INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,?,?)", ("BBB", "BBB", "x", "USD"))
        conn.commit()
        conn.close()

        def fake_backtest(df, symbol, timeframe="6mo", smc_config=None, backtest_config=None, weights=None):
            result = _sample_backtest_result(symbol)
            if symbol == "BBB":
                result["metrics"]["expectancy_r"] = 0.8
                result["metrics"]["profit_factor"] = 2.0
            return result

        with patch.object(app, "fetch_history", return_value=(_sample_ohlcv(), "test_history")), \
             patch.object(app, "run_smc_event_backtest", side_effect=fake_backtest):
            result = app.api_smc_backtest_batch(scope="watchlist", period="6mo", limit=2, store_runs=True)

        assert result["stored_runs"] == 2
        assert result["ranking"][0]["symbol"] == "BBB"

        conn = app.get_db()
        count = conn.execute("SELECT COUNT(*) FROM smc_backtest_runs").fetchone()[0]
        conn.close()
        assert count == 2
    finally:
        app.DB = original
