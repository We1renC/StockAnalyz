"""Tests that the LLM analysis context includes 17D matrix + fundamentals."""
from unittest.mock import patch

import app


def test_fundamentals_text_formats_metrics():
    fake = {
        "sector": "Technology", "industry": "Semiconductors",
        "trailing_pe": 33.0, "forward_pe": 19.8, "price_to_book": 10.7, "peg_ratio": 1.2,
        "trailing_eps": 73.5, "forward_eps": 90.0,
        "revenue_growth": 0.35, "earnings_growth": 0.58,
        "gross_margins": 0.62, "operating_margins": 0.45, "profit_margins": 0.46,
        "return_on_equity": 0.36, "debt_to_equity": 20.0, "dividend_yield": 1.01,
        "target_mean_price": 1200.0, "target_low_price": 1000.0, "target_high_price": 1400.0,
        "recommendation_key": "strong_buy", "num_analysts": 33, "market_cap": 1e12,
        "free_cashflow": 1e9,
    }
    with patch.object(app, "fetch_fundamentals", return_value=fake):
        text = app._build_fundamentals_text("2330.TW", price=1000.0)
    assert "本益比 TTM 33.00" in text
    assert "營收年增 35.0%" in text
    assert "ROE 36.0%" in text
    # 殖利率不應被 ×100（1.01% 不是 101%）
    assert "殖利率 1.01%" in text
    # 目標價相對現價
    assert "+20.0%" in text  # 1200 / 1000 - 1
    assert "strong_buy" in text


def test_fundamentals_text_handles_empty():
    with patch.object(app, "fetch_fundamentals", return_value={}):
        text = app._build_fundamentals_text("XXX", price=None)
    assert "暫時無法取得" in text


def test_technical_matrix_text_digest():
    fake_matrix = {
        "summary": {"bias": "bullish", "net_score": 1.2, "confidence": 0.7, "risk_level": "low",
                     "computed_count": 14, "partial_count": 1, "unavailable_count": 2},
        "execution_plan": {
            "entries": [{"type": "pullback_to_confluence", "price": 100}],
            "stops": [{"type": "stop_2atr_long", "price": 90}],
            "targets": [{"type": "resistance", "price": 120}],
            "risk_notes": ["test note"],
        },
        "confluence_zones": [{"center": 99, "score": 8}],
        "dimensions": [
            {"id": "trend_ma", "name": "Trend", "status": "computed", "bias": "bullish", "score": 1.5},
            {"id": "momentum", "name": "Momentum", "status": "computed", "bias": "neutral", "score": 0.1},
        ],
        "interactions": [{"name": "Confluence Zone", "status": "active"}],
        "data_gaps": ["x", "y"],
    }
    with patch.object(app, "_build_technical_matrix_payload", return_value=fake_matrix):
        text = app._build_technical_matrix_text("TEST")
    assert "整體偏向：bullish" in text
    assert "Trend=bullish" in text
    assert "Momentum" not in text  # neutral 不列入關鍵維度
    assert "系統建議進場區" in text
    assert "共振區" in text
    assert "資料缺口 2 項" in text


def test_technical_matrix_text_handles_failure():
    with patch.object(app, "_build_technical_matrix_payload", side_effect=Exception("boom")):
        text = app._build_technical_matrix_text("TEST")
    assert "暫時無法計算" in text


def test_build_context_includes_all_layers(tmp_path):
    # Isolated DB with one position + price cache
    original = app.DB
    app.DB = str(tmp_path / "ctx.db")
    try:
        app.init_db()
        conn = app.get_db()
        conn.execute(
            "INSERT INTO positions (symbol, name, category, shares, cost_price, currency, purchase_date) VALUES (?,?,?,?,?,?,?)",
            ("2330.TW", "台積電", "半導體", 10, 1000, "TWD", "2026-01-01"),
        )
        c = conn.cursor()
        app.store_price_cache(c, "2330.TW", {"price": 1100, "rsi": 60, "source": "test"})
        conn.commit()
        conn.close()

        with patch.object(app, "_build_technical_matrix_text", return_value="【17D 全景技術矩陣】mock"), \
             patch.object(app, "_build_fundamentals_text", return_value="【基本面與估值】mock"):
            ctx = app._build_context("2330.TW")
        assert "error" not in ctx
        body = ctx["context"]
        assert "【即時技術指標】" in body
        assert "【17D 全景技術矩陣】" in body
        assert "【基本面與估值】" in body
        assert "【持倉狀態】" in body
    finally:
        app.DB = original
