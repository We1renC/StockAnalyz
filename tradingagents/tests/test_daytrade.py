"""Tests for daytrade endpoint parameters (bypass_cache)."""
import json
from pathlib import Path
from unittest.mock import patch

import app


def _setup_temp_db(tmp_path):
    db_file = tmp_path / "test_portfolio.db"
    original = app.DB
    app.DB = str(db_file)
    app.init_db()
    return original


def _restore_db(original):
    app.DB = original


def _insert_position(symbol="2330.TW", name="台積電", shares=10, cost_price=1000, currency="TWD"):
    conn = app.get_db()
    conn.execute(
        """INSERT INTO positions
           (symbol, name, category, shares, cost_price, currency, purchase_date)
           VALUES (?, ?, '半導體', ?, ?, ?, '2026-06-02')""",
        (symbol, name, shares, cost_price, currency),
    )
    conn.commit()
    conn.close()


def test_api_diagnose_bypass_cache(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("2330.TW", "台積電")
        
        # Initially, there is no price cache. A normal api_diagnose should return an error.
        res1 = app.api_diagnose("2330.TW", bypass_cache=False)
        assert "error" in res1
        assert res1["error"] == "no price cache for symbol"

        # Now with bypass_cache=True, it should trigger fetch_indicators and store it.
        fake_ind = {"price": 1050.0, "rsi": 55.0, "change_1d": 1.5, "ma20": 1000.0, "ma60": 980.0, "beta": 1.1, "change_1m": 5.0, "low52": 800.0, "high52": 1100.0}
        with patch.object(app, "fetch_indicators", return_value=fake_ind) as mock_fetch:
            res2 = app.api_diagnose("2330.TW", bypass_cache=True)
            mock_fetch.assert_called_once_with("2330.TW")

        assert "error" not in res2
        assert res2["symbol"] == "2330.TW"
        assert res2["indicators"]["price"] == 1050.0
        
        # Verify it was indeed written to DB
        conn = app.get_db()
        row = conn.execute("SELECT data FROM price_cache WHERE symbol='2330.TW'").fetchone()
        conn.close()
        assert row is not None
        cached_data = json.loads(row["data"])
        assert cached_data["price"] == 1050.0
    finally:
        _restore_db(original)


def test_api_technical_matrix_bypass_cache(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("2330.TW", "台積電")
        
        fake_matrix = {"symbol": "2330.TW", "generated_at": "2026-06-02", "markers": [], "bias": "neutral", "score": 0}
        with patch.object(app, "_build_technical_matrix_payload", return_value=fake_matrix) as mock_build:
            res = app.api_technical_matrix("2330.TW", period="1y", include_history_markers=False, bypass_cache=True)
            mock_build.assert_called_once_with("2330.TW", "1y", use_cache=False, include_history_markers=False)
            
        assert res["symbol"] == "2330.TW"
    finally:
        _restore_db(original)


def test_chart_period_config_daytrade():
    c1 = app._chart_period_config("1m")
    assert c1 == {"period": "1d", "interval": "1m"}
    c5 = app._chart_period_config("5m")
    assert c5 == {"period": "1d", "interval": "1m"}

