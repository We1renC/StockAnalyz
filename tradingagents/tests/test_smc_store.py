from unittest.mock import patch

import pandas as pd

import app
from smc_report import (
    build_smc_report_html,
    build_smc_scan_report_html,
    build_smc_learning_health_report_html,
    build_smc_daily_report_html,
)
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


def test_build_smc_report_html_renders_key_sections():
    html = build_smc_report_html(
        {
            "run_count": 1,
            "trade_count": 2,
            "symbols": [{"symbol": "AAA", "market": "us", "trade_count": 2, "win_rate": 0.5, "expectancy_r": 0.2, "pnl": 20, "avg_holding_bars": 2.5}],
            "latest_runs": [{"symbol": "AAA", "period": "6mo", "total_trades": 2, "win_rate": 0.5, "profit_factor": 1.4, "expectancy_r": 0.2, "created_at": "2026-06-04T09:00:00Z"}],
        },
        title="Custom SMC Report",
    )
    assert "Custom SMC Report" in html
    assert "Symbol Summary" in html
    assert "AAA" in html


def test_build_smc_scan_report_html_renders_key_sections():
    html = build_smc_scan_report_html(
        {
            "scope": "all",
            "period": "6mo",
            "universe": [{"symbol": "AAA", "name": "Alpha"}, {"symbol": "BBB", "name": "Beta"}],
            "summary": {
                "symbol_count": 2,
                "signal_count": 3,
                "qualified_count": 1,
                "avg_score": 8.3,
                "avg_rr": 1.9,
                "model_breakdown": {"AMD": 2, "Silver Bullet": 1},
                "market_breakdown": {"us": 2, "tw": 1},
            },
            "results": [
                {
                    "symbol": "AAA",
                    "name": "Alpha",
                    "market": "us",
                    "model": "AMD",
                    "direction": "long",
                    "score": 9,
                    "entry": 100,
                    "stop": 95,
                    "tp1": 110,
                    "rr": 2.0,
                    "dol_target": {"type": "PDH", "level": 110},
                    "status": "qualified",
                }
            ],
        },
        title="Custom SMC Scan",
    )
    assert "Custom SMC Scan" in html
    assert "Signal Ranking" in html
    assert "Scanned Universe" in html
    assert "Model Breakdown" in html
    assert "Alpha" in html


def test_build_smc_learning_health_report_html_renders_key_sections():
    html = build_smc_learning_health_report_html(
        {
            "symbol": "AAA",
            "overview": {"total_trades": 18, "win_rate": 0.61, "expectancy_r": 0.42, "profit_factor": 1.8},
            "decay": {"is_decaying": False, "recent_expectancy": 0.38, "warning_message": None},
            "calibration": {"changes": ["Adjusted killzone (weight 1 -> 2)"], "kelly_cap_pct": 0.015, "proposed_weights": {"killzone": 2}},
            "validation": {"overfitting_risk_level": "low"},
            "top_positive_factors": [{"factor": "killzone", "count": 10, "win_rate": 0.7, "expected_r": 0.8, "diff_expectancy": 0.3}],
            "top_negative_factors": [{"factor": "ote_zone", "count": 6, "win_rate": 0.3, "expected_r": -0.4, "diff_expectancy": -0.5}],
            "model_ranking": [{"model": "AMD", "count": 8, "win_rate": 0.62, "expected_r": 0.7}],
            "feature_importance": [{"feature": "killzone", "importance": 0.42, "direction": 1}],
        },
        title="Custom SMC Health",
    )
    assert "Custom SMC Health" in html
    assert "Top Positive Factors" in html
    assert "Calibration Changes" in html
    assert "AMD" in html


def test_build_smc_daily_report_html_renders_key_sections():
    html = build_smc_daily_report_html(
        {
            "scope": "all",
            "period": "6mo",
            "overview": {
                "backtest_run_count": 3,
                "backtest_trade_count": 6,
                "health_win_rate": 0.61,
                "health_expectancy_r": 0.42,
                "kelly_cap_pct": 0.015,
            },
            "scan": {"summary": {"symbol_count": 5, "signal_count": 4, "qualified_count": 2}},
            "health": {"decay": {"is_decaying": False, "warning_message": None}},
            "top_signals": [
                {"symbol": "AAA", "market": "us", "model": "AMD", "direction": "long", "score": 9, "entry": 100, "tp1": 110, "rr": 2.0, "status": "qualified"}
            ],
            "top_backtests": [
                {"symbol": "AAA", "market": "us", "trade_count": 6, "win_rate": 0.6, "expectancy_r": 0.4, "pnl": 120}
            ],
            "recent_runs": [
                {"symbol": "AAA", "period": "6mo", "total_trades": 6, "win_rate": 0.6, "profit_factor": 1.8, "expectancy_r": 0.4}
            ],
        },
        title="Custom SMC Daily",
    )
    assert "Custom SMC Daily" in html
    assert "Top Signals" in html
    assert "Backtest Symbol Summary" in html
    assert "Recent Backtest Runs" in html
    assert "AAA" in html


def test_api_smc_backtest_report_html_returns_html(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        persist_backtest_run(conn, _sample_backtest_result("AAA"), period="6mo", source="test")
        conn.close()

        response = app.api_smc_backtest_report_html(symbol="AAA", limit_runs=10)
        assert "SMC Backtest Report - AAA" in response.body.decode("utf-8")
        assert "AAA" in response.body.decode("utf-8")
    finally:
        app.DB = original


def test_api_smc_scan_returns_ranked_results(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        conn.execute("INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,?,?)", ("AAPL", "Apple", "us", "USD"))
        conn.execute("INSERT INTO positions (symbol,name,category,shares,cost_price,currency,purchase_date) VALUES (?,?,?,1,100,?,?)", ("AAPL", "Apple", "us", "USD", "2026-06-04"))
        conn.commit()
        conn.close()

        with patch.object(app, "fetch_history", return_value=(_sample_ohlcv(), "yfinance")):
            result = app.api_smc_scan(period="6mo", swing_length=2, internal_swing_length=2)

        assert "results" in result
        assert "summary" in result
        assert isinstance(result["results"], list)
        assert result["summary"]["symbol_count"] == 1
        if result["results"]:
            assert result["results"][0]["symbol"] == "AAPL"
            assert result["results"][0]["name"] == "Apple"
            assert result["results"][0]["market"] == "us"
            assert result["results"][0]["source"] == "yfinance"
            assert "dol_distance" in result["results"][0]
            assert "dol_direction" in result["results"][0]
    finally:
        app.DB = original


def test_api_smc_scan_uses_runtime_cluster_weights(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        conn.execute(
            "INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,?,?)",
            ("AAPL", "Apple", "us", "USD"),
        )
        conn.commit()
        conn.close()

        seen = {}

        def _fake_build(*args, **kwargs):
            seen["cluster_weight_table"] = kwargs.get("cluster_weight_table")
            seen["cluster_key_hint"] = kwargs.get("cluster_key_hint")
            return {"signals": [], "market": "us", "summary": {}}

        with patch.object(app, "fetch_history", return_value=(_sample_ohlcv(), "yfinance")):
            with patch.object(app, "load_runtime_cluster_weight_table", return_value={"demo": {"w": 1.0}}):
                with patch.object(app, "build_smc_analysis", side_effect=_fake_build):
                    result = app.api_smc_scan(period="6mo", swing_length=2, internal_swing_length=2)

        assert result["summary"]["symbol_count"] == 1
        assert seen["cluster_weight_table"] == {"demo": {"w": 1.0}}
        assert seen["cluster_key_hint"] == ("runtime", "AAPL", "6mo", None)
    finally:
        app.DB = original


def test_api_smc_scan_report_html_returns_html(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        conn.execute("INSERT INTO watchlist (symbol,name,category,currency) VALUES (?,?,?,?)", ("AAPL", "Apple", "us", "USD"))
        conn.commit()
        conn.close()

        with patch.object(app, "fetch_history", return_value=(_sample_ohlcv(), "yfinance")):
            response = app.api_smc_scan_report_html(scope="watchlist", period="6mo", swing_length=2, internal_swing_length=2)
        body = response.body.decode("utf-8")
        assert "SMC Scan Report - watchlist" in body
        assert "Signal Ranking" in body
        assert "AAPL" in body
    finally:
        app.DB = original


def test_api_smc_learning_health_and_html(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        persist_backtest_run(conn, _sample_backtest_result("AAA"), period="6mo", source="test")
        persist_backtest_run(conn, _sample_backtest_result("BBB"), period="6mo", source="test")
        persist_backtest_run(conn, _sample_backtest_result("CCC"), period="6mo", source="test")
        conn.close()

        payload = app.api_smc_learning_health()
        assert payload["ok"] is True
        assert payload["overview"]["total_trades"] == 6
        assert "top_positive_factors" in payload
        assert "calibration" in payload
        assert "decay" in payload

        response = app.api_smc_learning_report_html()
        body = response.body.decode("utf-8")
        assert "SMC Strategy Health Report" in body
        assert "Top Positive Factors" in body
        assert "Calibration Changes" in body
    finally:
        app.DB = original


def test_api_smc_daily_report_and_html(tmp_path):
    original = _temp_db(tmp_path)
    try:
        conn = app.get_db()
        persist_backtest_run(conn, _sample_backtest_result("AAA"), period="6mo", source="test")
        persist_backtest_run(conn, _sample_backtest_result("BBB"), period="6mo", source="test")
        conn.close()

        fake_scan = {
            "scope": "all",
            "period": "6mo",
            "summary": {"symbol_count": 2, "signal_count": 1, "qualified_count": 1},
            "results": [{"symbol": "AAA", "market": "us", "model": "AMD", "direction": "long", "score": 9, "entry": 100, "tp1": 110, "rr": 2.0, "status": "qualified"}],
        }
        with patch.object(app, "api_smc_scan", return_value=fake_scan):
            payload = app.api_smc_daily_report(scope="all", period="6mo")
            assert payload["overview"]["backtest_run_count"] == 2
            assert payload["scan"]["summary"]["signal_count"] == 1
            assert payload["top_signals"][0]["symbol"] == "AAA"

            response = app.api_smc_daily_report_html(scope="all", period="6mo")
        body = response.body.decode("utf-8")
        assert "SMC Daily Report - all" in body
        assert "Top Signals" in body
        assert "AAA" in body
    finally:
        app.DB = original
