"""Tests for per-symbol fundamentals + 17D snapshot persistence (SQL + Obsidian)."""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "research.db")
    app.init_db()
    return original


def test_store_and_reload_fundamentals(tmp_path):
    original = _temp_db(tmp_path)
    try:
        data = {"trailing_pe": 30.0, "sector": "Tech", "revenue_growth": 0.2}
        app._store_fundamentals_snapshot("2330.TW", data)
        conn = app.get_db()
        row = conn.execute(
            "SELECT data FROM fundamentals_snapshots WHERE symbol='2330.TW' AND date=?",
            (date.today().isoformat(),),
        ).fetchone()
        conn.close()
        assert row is not None
        parsed = json.loads(row["data"])
        assert parsed["trailing_pe"] == 30.0
    finally:
        app.DB = original


def test_store_fundamentals_idempotent_per_day(tmp_path):
    original = _temp_db(tmp_path)
    try:
        app._store_fundamentals_snapshot("AAPL", {"trailing_pe": 30})
        app._store_fundamentals_snapshot("AAPL", {"trailing_pe": 35})  # same day overwrite
        conn = app.get_db()
        rows = conn.execute("SELECT data FROM fundamentals_snapshots WHERE symbol='AAPL'").fetchall()
        conn.close()
        assert len(rows) == 1
        assert json.loads(rows[0]["data"])["trailing_pe"] == 35
    finally:
        app.DB = original


def test_store_matrix_and_bias_history(tmp_path):
    original = _temp_db(tmp_path)
    try:
        # Seed 3 days of history manually
        conn = app.get_db()
        for offset, bias, score in [(2, "bearish", -1.0), (1, "neutral", 0.1), (0, "bullish", 1.3)]:
            d = (date.today() - timedelta(days=offset)).isoformat()
            conn.execute(
                """INSERT OR REPLACE INTO technical_matrix_snapshots
                   (symbol, date, period, bias, net_score, confidence, risk_level, data)
                   VALUES (?, ?, '6mo', ?, ?, 0.6, 'low', '{}')""",
                ("TEST", d, bias, score),
            )
        conn.commit()
        conn.close()

        hist = app._load_matrix_bias_history("TEST", limit=7)
        assert len(hist) == 3
        # chronological order
        assert hist[0]["bias"] == "bearish"
        assert hist[-1]["bias"] == "bullish"
    finally:
        app.DB = original


def test_store_matrix_snapshot_extracts_summary(tmp_path):
    original = _temp_db(tmp_path)
    try:
        matrix = {
            "summary": {"bias": "bullish", "net_score": 1.5, "confidence": 0.7, "risk_level": "medium"},
            "dimensions": [],
        }
        app._store_technical_matrix_snapshot("NVDA", matrix, "6mo")
        conn = app.get_db()
        row = conn.execute(
            "SELECT bias, net_score, confidence, risk_level FROM technical_matrix_snapshots WHERE symbol='NVDA'"
        ).fetchone()
        conn.close()
        assert row["bias"] == "bullish"
        assert row["net_score"] == 1.5
        assert row["risk_level"] == "medium"
    finally:
        app.DB = original


def test_obsidian_fundamentals_note_written(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    data = {
        "sector": "Technology", "industry": "Semiconductors",
        "trailing_pe": 33.0, "forward_pe": 20.0, "revenue_growth": 0.35,
        "return_on_equity": 0.36, "target_mean_price": 1200.0,
        "recommendation_key": "strong_buy", "num_analysts": 33,
    }
    app._obsidian_write_fundamentals(vault, "2330.TW", data)
    note = vault / "Fundamentals" / "2330.TW.md"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "type: fundamentals" in text
    assert "strong_buy" in text


def _synthetic_history(days=500):
    import numpy as np
    import pandas as pd
    rng = np.random.RandomState(3)
    idx = pd.date_range("2024-06-01", periods=days, freq="B")
    price = 100 + np.cumsum(rng.normal(0.05, 1.2, days))
    return pd.DataFrame({
        "Open": price - 0.3, "High": price + 0.6, "Low": price - 0.6,
        "Close": price, "Volume": rng.randint(8000, 30000, days),
    }, index=idx)


def test_backfill_creates_historical_snapshots(tmp_path):
    original = _temp_db(tmp_path)
    try:
        hist = _synthetic_history()
        with patch.object(app, "fetch_history", return_value=(hist, "test")), \
             patch.object(app, "fetch_benchmark_close", return_value=hist["Close"]):
            res = app._backfill_technical_matrix_history("TEST", lookback_days=120, step_days=5)
        assert res["filled"] > 0
        assert res["errors"] == 0
        rows = app._load_matrix_bias_history("TEST", limit=60)
        assert len(rows) == res["filled"]
        # dates are chronological + each has a bias
        assert all(r.get("bias") for r in rows)
        assert rows[0]["date"] < rows[-1]["date"]
    finally:
        app.DB = original


def test_backfill_idempotent(tmp_path):
    original = _temp_db(tmp_path)
    try:
        hist = _synthetic_history()
        with patch.object(app, "fetch_history", return_value=(hist, "test")), \
             patch.object(app, "fetch_benchmark_close", return_value=hist["Close"]):
            first = app._backfill_technical_matrix_history("TEST", lookback_days=120, step_days=5)
            second = app._backfill_technical_matrix_history("TEST", lookback_days=120, step_days=5)
        assert first["filled"] > 0
        assert second["filled"] == 0
        assert second["skipped"] >= first["filled"]
    finally:
        app.DB = original


def test_backfill_insufficient_history(tmp_path):
    original = _temp_db(tmp_path)
    try:
        short = _synthetic_history(days=30)
        with patch.object(app, "fetch_history", return_value=(short, "test")), \
             patch.object(app, "fetch_benchmark_close", return_value=short["Close"]):
            res = app._backfill_technical_matrix_history("TEST", lookback_days=120, step_days=5)
        assert res["filled"] == 0
        assert res.get("errors", 0) >= 1
    finally:
        app.DB = original


def test_persist_symbol_research_writes_sql(tmp_path):
    original = _temp_db(tmp_path)
    try:
        matrix = {"summary": {"bias": "bullish", "net_score": 1.0, "confidence": 0.6, "risk_level": "low"}}
        with patch.object(app, "_get_vault", return_value=None):  # skip obsidian
            app._persist_symbol_research("AAPL", {"trailing_pe": 30}, matrix)
        conn = app.get_db()
        f = conn.execute("SELECT 1 FROM fundamentals_snapshots WHERE symbol='AAPL'").fetchone()
        m = conn.execute("SELECT 1 FROM technical_matrix_snapshots WHERE symbol='AAPL'").fetchone()
        conn.close()
        assert f is not None
        assert m is not None
    finally:
        app.DB = original
