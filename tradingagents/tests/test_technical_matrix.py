"""Tests for the 17-dimensional technical analysis matrix."""

import pandas as pd

from technical_matrix import DIMENSION_DEFS, build_technical_matrix


def _history() -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=90, freq="D")
    rows = []
    for i, _ in enumerate(dates):
        base = 100 + i * 0.45
        open_ = base
        close = base + (1.2 if i % 5 else -0.8)
        high = max(open_, close) + 1.5
        low = min(open_, close) - 1.2
        volume = 1000 + i * 20
        rows.append((open_, high, low, close, volume))
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    # Force a latest lower-wick rejection and volume effort marker.
    h.iloc[-1, h.columns.get_loc("Open")] = 135
    h.iloc[-1, h.columns.get_loc("High")] = 138
    h.iloc[-1, h.columns.get_loc("Low")] = 125
    h.iloc[-1, h.columns.get_loc("Close")] = 137
    h.iloc[-1, h.columns.get_loc("Volume")] = 5000
    return h


def test_build_technical_matrix_preserves_all_17_dimensions():
    matrix = build_technical_matrix("TEST", _history(), benchmark_close=_history()["Close"] * 1.01, source="unit")

    assert matrix["symbol"] == "TEST"
    assert matrix["summary"]["dimension_count"] == 17
    assert "execution_plan" in matrix
    assert "marker_summary" in matrix
    assert len(matrix["dimensions"]) == 17
    assert [d["id"] for d in matrix["dimensions"]] == [d[0] for d in DIMENSION_DEFS]
    assert matrix["workflow"] == [d[0] for d in DIMENSION_DEFS]
    assert matrix["markers"]
    assert "order_book" in matrix["dimension_status"]
    assert matrix["dimension_status"]["order_book"] == "unavailable"
    assert matrix["data_gaps"]


def test_each_dimension_has_institutional_decision_fields():
    matrix = build_technical_matrix("TEST", _history())

    for dim in matrix["dimensions"]:
        assert {"status", "observations", "metrics", "levels", "markers", "data_gaps"}.issubset(dim)
        assert {"score", "bias", "confidence", "severity", "signals"}.issubset(dim)
        assert dim["bias"] in {"strong_bullish", "bullish", "neutral", "bearish", "strong_bearish"}
        assert 0 <= dim["confidence"] <= 1


def test_external_payload_dimensions_become_computed():
    matrix = build_technical_matrix(
        "TEST",
        _history(),
        order_flow={"aggressive_buy_volume": 900, "aggressive_sell_volume": 200, "cvd_delta": 450},
        breadth={"percent_above_50ma": 88, "percent_above_200ma": 72},
        options_profile={"spot": 140, "gamma_flip": 135, "gamma_wall": 150, "gamma_regime": "positive"},
        order_book={"bids": [{"price": 136, "size": 5000}], "asks": [{"price": 141, "size": 1000}]},
        events=[{"time": "2026-04-01", "label": "Earnings"}],
    )
    status = matrix["dimension_status"]

    assert status["microstructure_orderflow"] == "computed"
    assert status["breadth_internals"] == "computed"
    assert status["options_gex"] == "computed"
    assert status["order_book"] == "computed"
    assert status["event_calendar"] == "computed"
    assert matrix["summary"]["computed_count"] >= 13


def test_obsidian_snapshot_writer_creates_symbol_indexes(tmp_path):
    from app import _obsidian_write_technical_matrix

    matrix = build_technical_matrix("TEST", _history(), source="unit")
    note_path = _obsidian_write_technical_matrix(tmp_path, matrix)

    assert note_path.exists()
    assert "Matrix JSON" in note_path.read_text(encoding="utf-8")
    assert (tmp_path / "TechnicalAnalysis" / "Symbols" / "TEST" / "技術矩陣入口.md").exists()
    assert (tmp_path / "TechnicalAnalysis" / "技術矩陣總覽.md").exists()


def test_technical_matrix_marker_schema_matches_lightweight_charts():
    matrix = build_technical_matrix("TEST", _history())
    marker = matrix["markers"][0]

    assert {"time", "position", "color", "shape", "text"}.issubset(marker)
    assert marker["position"] in {"aboveBar", "belowBar", "inBar"}
    assert marker["shape"] in {"arrowUp", "arrowDown", "circle", "square"}
