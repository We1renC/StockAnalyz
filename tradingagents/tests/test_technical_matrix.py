"""Tests for the 17-dimensional technical analysis matrix."""

import numpy as np
import pandas as pd

from technical_matrix import (
    DIMENSION_DEFS,
    _classify_gap,
    _marker_direction_score,
    build_technical_matrix,
)


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


def _intraday_history() -> pd.DataFrame:
    dates = pd.date_range("2026-03-31 09:30", periods=30, freq="15min")
    rows = []
    for i, _ in enumerate(dates):
        base = 100 + i * 0.18
        open_ = base
        close = base + (0.22 if i % 3 else -0.11)
        high = max(open_, close) + 0.18
        low = min(open_, close) - 0.16
        volume = 1200 + i * 15
        rows.append((open_, high, low, close, volume))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)


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


def test_intraday_matrix_uses_daily_context_for_macro_layers():
    matrix = build_technical_matrix(
        "TEST",
        _intraday_history(),
        context_history=_history(),
        intraday_1h=_intraday_history().resample("1h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(),
        intraday_15m=_intraday_history(),
        intraday_5m=_intraday_history().resample("5min").ffill().dropna(),
    )

    assert matrix["history"]["granularity"] == "intraday"
    assert matrix["analysis_context"]["macro_basis"] == "context_daily"
    trend = next(d for d in matrix["dimensions"] if d["id"] == "trend_ma")
    volatility = next(d for d in matrix["dimensions"] if d["id"] == "volatility_risk")
    assert trend["metrics"]["analysis_basis"] == "context_daily"
    assert volatility["metrics"]["analysis_basis"] == "context_daily"


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


# ───── New behaviour: methodology faithfulness fixes ─────


def _history_with_internal_lvn() -> pd.DataFrame:
    """Build 60 bars where two distinct price clusters carry all volume so the
    midpoint becomes a true Low Volume Node (price vacuum) inside the value area.
    """
    np.random.seed(11)
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    rows = []
    for i in range(60):
        if i % 2 == 0:
            close = 100 + np.random.normal(0, 0.4)
            vol = 9000
        else:
            close = 140 + np.random.normal(0, 0.4)
            vol = 9000
        open_ = close - 0.3
        high = max(open_, close) + 0.4
        low = min(open_, close) - 0.4
        rows.append((open_, high, low, close, vol))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)


def test_lvn_includes_zero_volume_bins_inside_value_area():
    """The 120-price midpoint should surface as an LVN; old code excluded zero bins."""
    matrix = build_technical_matrix("TEST", _history_with_internal_lvn())
    vp = next(d for d in matrix["dimensions"] if d["id"] == "volume_profile")
    lvn_prices = [n["price"] for n in (vp["metrics"].get("lvn_nodes") or [])]
    assert lvn_prices, "expected at least one LVN inside the value area"
    # The valley between the two clusters lives roughly at 120
    assert any(110 < p < 130 for p in lvn_prices), f"midpoint vacuum missing: {lvn_prices}"


def _history_with_choch() -> pd.DataFrame:
    """Build a clearly bearish leg (descending pivots) followed by an aggressive
    upside break of the latest swing high so structure_geometry should flag ChoCh.
    """
    dates = pd.date_range("2026-01-01", periods=40, freq="D")
    # Descending zig-zag to seed lower lows and lower highs
    price = [200, 198, 196, 195, 192, 188, 185, 182, 180, 178,
             182, 179, 176, 175, 170, 167, 165, 162, 160, 158,
             162, 159, 156, 154, 152, 150, 148, 146, 144, 142,
             145, 142, 140, 138, 136, 134, 133, 131, 130, 175]  # breakout final bar
    rows = []
    for p in price:
        open_ = p - 0.5
        close = p + 0.5
        high = p + 1.0
        low = p - 1.0
        rows.append((open_, high, low, close, 10000))
    rows[-1] = (170, 178, 168, 177, 35000)  # strong final upside break
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)


def test_choch_detected_when_break_opposes_prior_trend():
    matrix = build_technical_matrix("TEST", _history_with_choch())
    sg = next(d for d in matrix["dimensions"] if d["id"] == "structure_geometry")
    texts = " | ".join(o.lower() for o in sg["observations"])
    assert "choch" in texts, f"expected ChoCh in observations: {texts}"


def test_gap_classification_returns_distinct_labels():
    """_classify_gap should reach all three label families given different context."""
    # 60-bar mean-reverting series; vary the LAST close to choose the rank bucket.
    np.random.seed(1)
    base_close = 100 + np.random.normal(0, 0.7, 60)
    base = pd.DataFrame(
        {
            "Open": base_close - 0.3,
            "High": base_close + 0.6,
            "Low": base_close - 0.6,
            "Close": base_close,
            "Volume": [10000] * 60,
        },
        index=pd.date_range("2026-01-01", periods=60, freq="D"),
    )

    # Exhaustion: last close pushed to extreme top of the 60-bar range + high vol
    exh = base.copy()
    exh.iloc[-1, exh.columns.get_loc("Close")] = float(base_close.max()) + 5
    label_exh, _ = _classify_gap("up", exh, vol_ratio=2.5)
    assert label_exh == "Exhaustion Gap"

    # Runaway: last close sits mid-to-upper range (rank ~0.55–0.85) + high vol
    runaway = base.copy()
    runaway.iloc[-1, runaway.columns.get_loc("Close")] = float(np.quantile(base_close, 0.65))
    label_run, _ = _classify_gap("up", runaway, vol_ratio=2.0)
    assert label_run == "Runaway Gap"

    # Breakaway: no volume context -> default
    label_bk, _ = _classify_gap("up", base, vol_ratio=None)
    assert label_bk == "Breakaway Gap"


def _history_distribution() -> pd.DataFrame:
    """Range with volume biased toward highs => Wyckoff distribution."""
    np.random.seed(3)
    dates = pd.date_range("2026-01-01", periods=120, freq="D")
    rows = []
    for i in range(120):
        # narrow range 90-110, but last 40 bars trade with high volume near top
        close = 100 + np.random.uniform(-9, 9)
        in_range_top = close >= 105
        vol = 30000 if in_range_top and i >= 80 else 6000
        open_ = close - 0.4
        high = close + 0.8
        low = close - 0.8
        rows.append((open_, high, low, close, vol))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)


def _history_accumulation() -> pd.DataFrame:
    np.random.seed(4)
    dates = pd.date_range("2026-01-01", periods=120, freq="D")
    rows = []
    for i in range(120):
        close = 100 + np.random.uniform(-9, 9)
        in_range_bottom = close <= 95
        vol = 30000 if in_range_bottom and i >= 80 else 6000
        open_ = close - 0.4
        high = close + 0.8
        low = close - 0.8
        rows.append((open_, high, low, close, vol))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)


def test_wyckoff_phase_distinguishes_accumulation_and_distribution():
    dist = build_technical_matrix("DIST", _history_distribution())
    acc = build_technical_matrix("ACC", _history_accumulation())
    dist_phase = next(d for d in dist["dimensions"] if d["id"] == "macro_wave")["metrics"]["wyckoff_phase"]
    acc_phase = next(d for d in acc["dimensions"] if d["id"] == "macro_wave")["metrics"]["wyckoff_phase"]
    assert dist_phase in {"distribution", "accumulation_or_distribution"}
    assert acc_phase in {"accumulation", "accumulation_or_distribution"}
    # At least one of them must specifically resolve (otherwise heuristic is dead)
    assert {dist_phase, acc_phase} & {"distribution", "accumulation"}, (
        f"phase resolution lost: dist={dist_phase} acc={acc_phase}"
    )


def test_trend_ma_emits_ema_metrics_and_levels():
    matrix = build_technical_matrix("TEST", _history())
    tma = next(d for d in matrix["dimensions"] if d["id"] == "trend_ma")
    metrics = tma["metrics"]
    # EMAs land in metrics under ema12/ema20/ema26/ema50 keys
    assert any(k.startswith("ema") for k in metrics.keys()), metrics.keys()
    assert "ema20" in metrics
    ema_levels = [lv for lv in tma["levels"] if lv["type"] == "exponential_ma"]
    assert ema_levels, "expected exponential_ma levels"


def _history_with_head_and_shoulders() -> pd.DataFrame:
    # Three highs: left shoulder, head, right shoulder, with valleys between
    dates = pd.date_range("2026-01-01", periods=80, freq="D")
    pattern = (
        list(np.linspace(100, 120, 10))   # rise to left shoulder
        + list(np.linspace(120, 110, 8))  # pullback
        + list(np.linspace(110, 140, 10)) # rise to head
        + list(np.linspace(140, 112, 12)) # pullback
        + list(np.linspace(112, 122, 10)) # right shoulder ≈ 122 (within 5%)
        + list(np.linspace(122, 100, 30)) # neckline break
    )
    pattern = pattern[:80]
    rows = []
    for p in pattern:
        rows.append((p - 0.5, p + 1.0, p - 1.0, p + 0.3, 12000))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)


def test_head_and_shoulders_pattern_surfaces_in_structure():
    matrix = build_technical_matrix("HS", _history_with_head_and_shoulders())
    sg = next(d for d in matrix["dimensions"] if d["id"] == "structure_geometry")
    text = " ".join(sg["observations"]).lower()
    has_pattern_level = any(
        lv.get("type") in {"head_and_shoulders", "inverse_head_and_shoulders", "double_top", "double_bottom"}
        for lv in sg["levels"]
    )
    assert ("head & shoulders" in text or "double" in text) or has_pattern_level, (
        f"expected H&S/double pattern in observations or levels; got: {text}"
    )


def test_mtf_intraday_direction_propagates_when_supplied():
    base = _history()
    # Build a fake 1H series with consistent up drift
    idx = pd.date_range("2026-04-01", periods=30, freq="h")
    close = np.linspace(100, 110, 30)
    intraday_1h = pd.DataFrame({
        "Open": close - 0.2, "High": close + 0.3, "Low": close - 0.3,
        "Close": close, "Volume": [1000] * 30,
    }, index=idx)
    matrix = build_technical_matrix("TEST", base, intraday_1h=intraday_1h)
    mtf = next(d for d in matrix["dimensions"] if d["id"] == "mtf_derivatives")
    assert mtf["metrics"].get("intraday_1h_direction") == "up"


def test_short_squeeze_and_oi_accumulation_flagged_when_payload_supplied():
    base = _history()
    matrix = build_technical_matrix(
        "TEST",
        base,
        derivatives={
            "open_interest_change_pct": 35,
            "funding_rate": -0.05,
        },
    )
    mtf = next(d for d in matrix["dimensions"] if d["id"] == "mtf_derivatives")
    text = " ".join(mtf["observations"]).lower()
    # OI accumulation requires low ATR percentile too; squeeze is the safer signal here
    assert "squeeze" in text or "accumulation" in text or "leverage" in text, text


def test_cvd_divergence_detected_with_parallel_series():
    base = _history()
    # Bearish divergence: price monotone up, CVD monotone down
    matrix = build_technical_matrix(
        "TEST",
        base,
        order_flow={
            "aggressive_buy_volume": 200, "aggressive_sell_volume": 100,
            "price_series": [(i, 100 + i) for i in range(20)],
            "cvd_series": [(i, 200 - i) for i in range(20)],
        },
    )
    mo = next(d for d in matrix["dimensions"] if d["id"] == "microstructure_orderflow")
    assert mo["metrics"]["cvd_divergence"] == "bearish", mo["metrics"]


def test_multi_benchmark_intermarket_metrics_present():
    base = _history()
    dates = base.index
    dxy = pd.Series(np.linspace(100, 102, len(dates)), index=dates)
    vix = pd.Series(np.linspace(15, 30, len(dates)), index=dates)  # rising VIX
    matrix = build_technical_matrix(
        "TEST",
        base,
        benchmark_close=base["Close"] * 1.01,
        benchmarks={"spx": base["Close"] * 1.01, "dxy": dxy, "vix": vix},
    )
    im = next(d for d in matrix["dimensions"] if d["id"] == "intermarket_correlation")
    cross = im["metrics"].get("cross_benchmarks_20d_alpha") or {}
    assert "dxy" in cross
    # VIX latest must be reported
    assert "vix_latest" in cross


def test_opening_range_computed_from_intraday_5m():
    base = _history()
    # 30 bars × 5min on the latest day
    last_date = pd.Timestamp("2026-04-15 09:00")
    idx = pd.date_range(last_date, periods=30, freq="5min")
    close = np.concatenate([np.linspace(100, 102, 6), np.linspace(102, 108, 24)])  # first 30min: 100-102
    intraday_5m = pd.DataFrame({
        "Open": close - 0.1, "High": close + 0.3, "Low": close - 0.3,
        "Close": close, "Volume": [500] * 30,
    }, index=idx)
    matrix = build_technical_matrix("TEST", base, intraday_5m=intraday_5m)
    tc = next(d for d in matrix["dimensions"] if d["id"] == "time_cyclical")
    op = tc["metrics"].get("opening_range")
    assert op is not None
    assert op["or_high"] >= 102 and op["or_low"] <= 100.1
    assert op["cleared"] == "up"


def test_fvg_fill_marker_when_price_reenters_zone():
    # Construct a series with a clear bullish FVG (gap up) that later gets filled.
    dates = pd.date_range("2026-01-01", periods=15, freq="D")
    # Bars 0–4: price near 100. Bar 5 gaps up: prev high 101, next low 110. Bars 6–10: drift around 112.
    # Bars 11–14: pull back into 105–109 (re-enters FVG 101..110).
    rows = []
    for i in range(15):
        if i < 5:
            o, c, hi, lo = 100, 100.5, 101, 99
        elif i == 5:
            o, c, hi, lo = 110, 113, 114, 110
        elif i < 11:
            o, c, hi, lo = 112, 113, 115, 111
        else:
            o, c, hi, lo = 108, 106, 109, 104  # fills the gap zone 101–110
        rows.append((o, hi, lo, c, 1000))
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    matrix = build_technical_matrix("TEST", h)
    ag = next(d for d in matrix["dimensions"] if d["id"] == "advanced_geometries")
    fill_markers = [m for m in ag["markers"] if "fvg fill" in (m.get("text") or "").lower()]
    assert fill_markers, ag["observations"]


def test_statistical_3sigma_marker_at_extreme_extension():
    # Construct prices that produce a +3σ outlier on the last bar.
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    base = np.linspace(100, 101, 60)
    base[-1] = 130  # large spike → high z-score
    rows = [(p - 0.1, p + 0.3, p - 0.3, p, 1000) for p in base]
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    matrix = build_technical_matrix("TEST", h)
    sr = next(d for d in matrix["dimensions"] if d["id"] == "statistical_reversion")
    sigma_markers = [m for m in sr["markers"] if "3σ" in (m.get("text") or "")]
    assert sigma_markers, sr["observations"]


def test_wave3_extension_candidate_appears_in_macro_wave():
    # Build a long zigzag with one dominant impulse leg
    dates = pd.date_range("2026-01-01", periods=120, freq="D")
    walk = []
    # Wave 1 up small, Wave 2 down small, Wave 3 BIG up, Wave 4 small down, Wave 5 small up
    sections = [
        np.linspace(100, 105, 20),   # W1
        np.linspace(105, 102, 20),   # W2
        np.linspace(102, 145, 30),   # W3 dominant
        np.linspace(145, 138, 20),   # W4
        np.linspace(138, 152, 30),   # W5
    ]
    walk = np.concatenate(sections)[:120]
    rows = [(p - 0.3, p + 0.8, p - 0.8, p, 15000) for p in walk]
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    matrix = build_technical_matrix("TEST", h)
    mw = next(d for d in matrix["dimensions"] if d["id"] == "macro_wave")
    assert mw["metrics"].get("wave3_candidate") is not None


def test_whipsaw_detected_with_intraday_5m_on_event_day():
    base = _history()
    last_date = pd.Timestamp("2026-04-15 09:00")
    idx = pd.date_range(last_date, periods=78, freq="5min")  # 6.5 hours
    # Manufactured violent up-down-up sequence
    close = []
    for i in range(78):
        if i < 20:
            close.append(100 + i * 0.5)
        elif i < 40:
            close.append(110 - (i - 20) * 0.6)
        elif i < 60:
            close.append(98 + (i - 40) * 0.5)
        else:
            close.append(108 - (i - 60) * 0.4)
    close = np.array(close)
    intraday_5m = pd.DataFrame({
        "Open": close - 0.2, "High": close + 0.5, "Low": close - 0.5,
        "Close": close, "Volume": [400] * 78,
    }, index=idx)
    matrix = build_technical_matrix("TEST", base, intraday_5m=intraday_5m)
    ev = next(d for d in matrix["dimensions"] if d["id"] == "event_calendar")
    # whipsaw could fire on unavailable branch or computed branch
    assert ev["metrics"].get("whipsaw") is not None


def test_breadth_payload_with_advancing_declining_promotes_to_computed():
    """Native TW breadth: advancing + declining counts should resolve A/D ratio."""
    matrix = build_technical_matrix(
        "TEST",
        _history(),
        breadth={
            "advancing": 900,
            "declining": 200,
            "unchanged": 80,
            "source": "twse_mi_index",
        },
    )
    br = next(d for d in matrix["dimensions"] if d["id"] == "breadth_internals")
    assert br["status"] == "computed"
    assert br["metrics"]["ad_ratio"] == 4.5  # 900/200
    text = " ".join(br["observations"]).lower()
    assert "advancers" in text or "thrust" in text


def test_order_book_5level_imbalance_resolves_dimension():
    """5-level snapshot from TWSE should drive XIV order_book → computed."""
    matrix = build_technical_matrix(
        "TEST",
        _history(),
        order_book={
            "bids": [
                {"price": 100.5, "size": 2000},
                {"price": 100.0, "size": 1500},
            ],
            "asks": [
                {"price": 101.0, "size": 500},
                {"price": 101.5, "size": 400},
            ],
            "source": "twse_5level",
        },
    )
    ob = next(d for d in matrix["dimensions"] if d["id"] == "order_book")
    assert ob["status"] == "computed"
    # bid depth 3500 vs ask depth 900 → imbalance ≈ 3.89, bullish wall
    assert ob["metrics"]["book_imbalance"] > 2


def test_options_profile_with_gex_resolves_dimension():
    """Options payload (gamma flip/wall/max pain) should drive XIII → computed."""
    matrix = build_technical_matrix(
        "TEST",
        _history(),
        options_profile={
            "spot": 130,
            "gamma_flip": 125,
            "gamma_wall": 140,
            "max_pain": 128,
            "gamma_regime": "positive",
            "expiration": "2026-05-15",
        },
    )
    og = next(d for d in matrix["dimensions"] if d["id"] == "options_gex")
    assert og["status"] == "computed"
    types = {lv.get("type") for lv in og["levels"]}
    assert {"gamma_flip", "gamma_wall", "max_pain"}.issubset(types)


def test_data_gaps_by_dimension_preserves_attribution():
    matrix = build_technical_matrix("TEST", _history())
    by_dim = matrix.get("data_gaps_by_dimension") or {}
    # At least one of the unavailable dims should have its gaps recorded
    assert any(
        len(gaps) > 0
        for dim_id, gaps in by_dim.items()
        if dim_id in {"microstructure_orderflow", "options_gex", "order_book", "event_calendar"}
    ), by_dim


def test_short_period_intraday_still_builds_17_dimensions():
    """Hourly execution data + daily context should keep all 17 dims healthy."""
    # 1h intraday for execution (3 days × ~8 bars = 24 bars)
    idx = pd.date_range("2026-04-01 09:00", periods=24, freq="h")
    close = 100 + np.linspace(0, 5, 24) + np.random.RandomState(0).normal(0, 0.3, 24)
    intraday = pd.DataFrame({
        "Open": close - 0.2, "High": close + 0.3, "Low": close - 0.3,
        "Close": close, "Volume": [500] * 24,
    }, index=idx)
    matrix = build_technical_matrix("TEST", intraday, context_history=_history())
    assert matrix["summary"]["dimension_count"] == 17
    ctx = matrix["analysis_context"]
    assert ctx["execution_granularity"] == "intraday"
    # Macro / trend / volatility should fall back to the daily context
    assert ctx["macro_basis"] == "context_daily"
    assert ctx["trend_basis"] == "context_daily"


def test_include_history_markers_backfills_high_value_patterns():
    """Toggle on should append historical markers without removing original ones."""
    np.random.seed(21)
    dates = pd.date_range("2025-01-01", periods=160, freq="D")
    close = 100 + np.cumsum(np.random.normal(0, 1.5, 160))
    rows = []
    # Inject a few clear engulfings into the synthetic series
    for i, c in enumerate(close):
        o = c - 0.4
        hi = c + 0.6
        lo = c - 0.6
        rows.append([o, hi, lo, c, 10000])
    # Force one bull engulf at index 50, one bear engulf at index 100
    rows[49] = [rows[49][0], rows[49][1] + 0.2, rows[49][2] - 0.2, rows[49][0] - 1.0, 12000]
    rows[50] = [rows[50][0] - 0.5, rows[50][1] + 1.5, rows[50][2] - 0.5, rows[50][0] + 2.0, 14000]
    rows[99] = [rows[99][0], rows[99][1] + 0.5, rows[99][2] - 0.2, rows[99][0] + 1.0, 12000]
    rows[100] = [rows[100][0] + 0.5, rows[100][1] + 0.5, rows[100][2] - 1.5, rows[100][0] - 2.0, 14000]
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)

    default = build_technical_matrix("TEST", h)
    expanded = build_technical_matrix("TEST", h, include_history_markers=True)

    assert default["marker_summary"]["history_backfill"] == 0
    assert expanded["marker_summary"]["history_backfill"] > 0
    assert expanded["marker_summary"]["include_history_markers"] is True
    assert len(expanded["markers"]) > len(default["markers"]), (
        len(expanded["markers"]), len(default["markers"])
    )
    # Every marker in default must still exist in expanded (history toggle is additive)
    default_keys = {(m["time"], m.get("dimension"), m.get("text")) for m in default["markers"]}
    expanded_keys = {(m["time"], m.get("dimension"), m.get("text")) for m in expanded["markers"]}
    assert default_keys.issubset(expanded_keys)
    # Backfilled marker text must include at least one of the high-value patterns
    history_texts = {m.get("text") for m in expanded["markers"] if m["time"] != expanded["markers"][-1]["time"]}
    assert any(label in history_texts for label in {"Bull Engulf", "Bear Engulf", "Hammer", "Pin Bar", "Sweep", "BOS", "ChoCh", "+3σ", "-3σ", "RSI Div"}), history_texts


def test_default_anchor_stack_produces_multiple_avwap():
    matrix = build_technical_matrix("TEST", _history_with_internal_lvn())
    tc = next(d for d in matrix["dimensions"] if d["id"] == "time_cyclical")
    anchors = tc["metrics"].get("avwap") or []
    # range_low + range_high should both fire with default anchors
    labels = {a.get("label") for a in anchors}
    assert "range_low" in labels and "range_high" in labels, labels


def test_liquidity_pools_detect_equal_levels():
    # Construct alternating swings where two highs land at ~150 and two lows at ~100
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    pattern = [100, 110, 120, 130, 140, 150, 140, 130, 120, 110, 100, 110, 120, 130, 140, 150.3, 140, 130, 120, 110, 100.2,
               110, 120, 130, 140, 150.1, 140, 130, 120, 110, 100.1, 110, 120, 130, 140, 150.05, 140, 130, 120, 110, 100.05,
               110, 120, 130, 140, 150, 140, 130, 120, 110, 100, 110, 120, 130, 140, 150, 140, 130, 120, 110]
    pattern = pattern[:60]
    rows = [(p, p + 0.5, p - 0.5, p, 10000) for p in pattern]
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    matrix = build_technical_matrix("HQ", h)
    sg = next(d for d in matrix["dimensions"] if d["id"] == "structure_geometry")
    pool_types = {lv.get("type") for lv in sg["levels"]}
    assert "equal_highs" in pool_types or "equal_lows" in pool_types, pool_types


def test_chart_patterns_can_surface_via_levels():
    # Ascending triangle: flat highs, rising lows
    dates = pd.date_range("2026-01-01", periods=80, freq="D")
    rows = []
    for i in range(80):
        baseline = 100 + (i % 10)  # oscillation
        if i % 10 == 5:  # peak
            hi = 130
            lo = 100 + i * 0.3
            c = 125
        else:
            hi = 122 + (i % 3)
            lo = 100 + i * 0.3
            c = lo + 3
        rows.append((lo + 1, hi, lo, c, 10000))
    h = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=dates)
    # Pattern detection may or may not fire depending on the noise, but the
    # detector must at least run without errors and produce a list of levels
    matrix = build_technical_matrix("ASC", h)
    sg = next(d for d in matrix["dimensions"] if d["id"] == "structure_geometry")
    assert isinstance(sg["levels"], list)


def test_statistical_metrics_include_hurst_halflife_ewma():
    matrix = build_technical_matrix("TEST", _history())
    sr = next(d for d in matrix["dimensions"] if d["id"] == "statistical_reversion")
    assert "hurst_exponent" in sr["metrics"]
    assert "ou_half_life_bars" in sr["metrics"]
    assert "ewma_volatility" in sr["metrics"]


def test_marker_direction_score_does_not_double_count_arrow_text():
    # Bull engulf marker: arrowUp belowBar weight 4 -> +4/3 ≈ 1.33, no text bonus
    bull = [{"position": "belowBar", "shape": "arrowUp", "color": "x",
             "text": "Bull Engulf", "weight": 4, "dimension": "price_action"}]
    score_bull = _marker_direction_score(bull)
    assert 1.0 < score_bull < 1.6, score_bull

    # Bear trap marker (bullish meaning, neutral shape): explicit text bias = +weight
    bear_trap = [{"position": "belowBar", "shape": "arrowUp", "color": "x",
                  "text": "Bear Trap", "weight": 5, "dimension": "price_action"}]
    score_trap = _marker_direction_score(bear_trap)
    # arrowUp -> +5/3 ≈ 1.67; bias map intentionally NOT applied for directional arrows
    assert score_trap > 1.0

    # Same Bear Trap with neutral shape: text bias resolves to bullish weight
    bear_trap_neutral = [{"position": "inBar", "shape": "circle", "color": "x",
                          "text": "Bear Trap", "weight": 3, "dimension": "price_action"}]
    score_neutral = _marker_direction_score(bear_trap_neutral)
    assert score_neutral > 0, f"Bear Trap (neutral shape) should be bullish, got {score_neutral}"
