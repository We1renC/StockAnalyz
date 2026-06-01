"""17-dimensional institutional technical analysis matrix.

The module keeps every dimension from the source methodology as a distinct
analysis surface. Dimensions that require unavailable market data still emit a
structured unavailable/partial result instead of being merged away.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd


DIMENSION_DEFS = [
    ("price_action", "Core Price Action & Candlestick Layer"),
    ("trend_ma", "Trend & Moving Average Systems"),
    ("volume_profile", "Volume & Market Profile Layer"),
    ("momentum", "Momentum & Oscillators Layer"),
    ("structure_geometry", "Market Structure & Geometry Layer"),
    ("volatility_risk", "Volatility & Risk Management Indicators"),
    ("mtf_derivatives", "Multi-Timeframe & Derivatives Layer"),
    ("microstructure_orderflow", "Market Microstructure & Order Flow Layer"),
    ("intermarket_correlation", "Intermarket Analysis & Correlation Layer"),
    ("breadth_internals", "Market Breadth & Internals Layer"),
    ("time_cyclical", "Time & Cyclical Analysis Layer"),
    ("advanced_geometries", "Advanced Structural & Harmonic Geometries Layer"),
    ("options_gex", "Options Mechanics & GEX Profile"),
    ("order_book", "Depth of Market & Order Book Dynamics"),
    ("statistical_reversion", "Statistical Mechanics & Mean Reversion Gauges"),
    ("macro_wave", "Macro Cyclicality & Wave Mechanics"),
    ("event_calendar", "Macro Event Timeline & Calendar Drag"),
]


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _round(value: Any, digits: int = 2) -> Optional[float]:
    number = _as_float(value)
    return round(number, digits) if number is not None else None


def _clean_history(history: pd.DataFrame) -> pd.DataFrame:
    required = ["Open", "High", "Low", "Close"]
    missing = [col for col in required if col not in history.columns]
    if missing:
        raise ValueError(f"missing OHLC columns: {', '.join(missing)}")
    h = history.copy()
    h = h.dropna(subset=required)
    if "Volume" not in h.columns:
        h["Volume"] = 0
    h["Volume"] = h["Volume"].fillna(0)
    h.index = pd.to_datetime(h.index).tz_localize(None)
    return h.sort_index()


def _is_intraday_history(history: Optional[pd.DataFrame]) -> bool:
    if history is None or len(history.index) < 2:
        return False
    diffs = pd.Series(history.index[1:] - history.index[:-1])
    median = diffs.median()
    return pd.notna(median) and median < pd.Timedelta(days=1)


def _time(ts: Any) -> int:
    return int(pd.Timestamp(ts).timestamp())


def _marker(
    ts: Any,
    position: str,
    color: str,
    shape: str,
    text: str,
    dimension: str,
    weight: int = 1,
    price: Any = None,
) -> dict:
    return {
        "time": _time(ts),
        "position": position,
        "color": color,
        "shape": shape,
        "text": text,
        "dimension": dimension,
        "weight": weight,
        "price": _round(price, 2),
    }


def _clip(value: Any, low: float, high: float) -> float:
    number = _as_float(value)
    if number is None:
        return 0.0
    return max(low, min(high, number))


def _bias_from_score(score: Any) -> str:
    number = _as_float(score) or 0.0
    if number >= 2.25:
        return "strong_bullish"
    if number >= 0.75:
        return "bullish"
    if number <= -2.25:
        return "strong_bearish"
    if number <= -0.75:
        return "bearish"
    return "neutral"


def _confidence_for_status(status: str, has_metrics: bool = False, has_gaps: bool = False) -> float:
    base = {"computed": 0.74, "partial": 0.46, "unavailable": 0.10}.get(status, 0.35)
    if has_metrics:
        base += 0.08
    if has_gaps:
        base -= 0.08
    return _clip(base, 0.05, 0.92)


def _severity_from_value(value: float) -> str:
    if value >= 2.0:
        return "high"
    if value >= 1.0:
        return "medium"
    return "low"


def _dimension(
    dimension_id: str,
    status: str,
    observations: list[str],
    metrics: Optional[dict] = None,
    markers: Optional[list[dict]] = None,
    levels: Optional[list[dict]] = None,
    data_gaps: Optional[list[str]] = None,
    score: float = 0.0,
    bias: Optional[str] = None,
    confidence: Optional[float] = None,
    severity: str = "low",
    signals: Optional[list[dict]] = None,
) -> dict:
    name = dict(DIMENSION_DEFS).get(dimension_id, dimension_id)
    metrics = metrics or {}
    data_gaps = data_gaps or []
    final_score = _clip(score, -4.0, 4.0)
    return {
        "id": dimension_id,
        "name": name,
        "status": status,
        "observations": observations,
        "metrics": metrics,
        "levels": levels or [],
        "markers": markers or [],
        "data_gaps": data_gaps,
        "score": _round(final_score, 2),
        "bias": bias or _bias_from_score(final_score),
        "confidence": _round(
            confidence if confidence is not None else _confidence_for_status(status, bool(metrics), bool(data_gaps)),
            2,
        ),
        "severity": severity,
        "signals": signals or [],
    }


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def _stochastic(h: pd.DataFrame, period: int = 14, smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    low_min = h["Low"].rolling(period).min()
    high_max = h["High"].rolling(period).max()
    k = 100 * (h["Close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(smooth).mean()
    return k, d


def _atr(h: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = h["Close"].shift(1)
    tr = pd.concat([
        h["High"] - h["Low"],
        (h["High"] - prev_close).abs(),
        (h["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _find_pivots(h: pd.DataFrame, window: int = 2) -> list[dict]:
    pivots: list[dict] = []
    if len(h) < window * 2 + 1:
        return pivots
    highs = h["High"]
    lows = h["Low"]
    for i in range(window, len(h) - window):
        high_slice = highs.iloc[i - window:i + window + 1]
        low_slice = lows.iloc[i - window:i + window + 1]
        ts = h.index[i]
        if highs.iloc[i] == high_slice.max():
            pivots.append({"index": i, "time": ts, "kind": "high", "price": float(highs.iloc[i])})
        if lows.iloc[i] == low_slice.min():
            pivots.append({"index": i, "time": ts, "kind": "low", "price": float(lows.iloc[i])})
    return pivots


def _series_return(close: pd.Series, periods: int) -> Optional[float]:
    if len(close) <= periods:
        return None
    base = close.iloc[-periods - 1]
    if not base:
        return None
    return float((close.iloc[-1] / base - 1) * 100)


def _classify_gap(
    direction: str,
    h: pd.DataFrame,
    vol_ratio: Optional[float],
) -> tuple[str, str]:
    """Classify a gap as breakaway / runaway / exhaustion per Wyckoff context.

    Returns (label, observation).
    """
    close = h["Close"]
    last_close = close.iloc[-1]
    # Position within the recent 60-bar range; tail-of-trend = exhaustion candidate
    window = close.tail(60)
    if len(window) >= 20 and window.max() > window.min():
        rank = (window <= last_close).mean()
    else:
        rank = 0.5
    high_vol = vol_ratio is not None and vol_ratio >= 1.6
    if direction == "up":
        if rank >= 0.9 and high_vol:
            return "Exhaustion Gap", "Upside gap near recent highs with elevated volume; exhaustion gap risk."
        if rank >= 0.55 and high_vol:
            return "Runaway Gap", "Upside runaway/measuring gap inside an existing markup leg."
        return "Breakaway Gap", "Upside breakaway gap leaving the prior consolidation."
    if rank <= 0.1 and high_vol:
        return "Exhaustion Gap", "Downside gap near recent lows with elevated volume; capitulation exhaustion gap."
    if rank <= 0.45 and high_vol:
        return "Runaway Gap", "Downside runaway gap inside an existing markdown leg."
    return "Breakaway Gap", "Downside breakaway gap leaving the prior consolidation."


def _price_action(h: pd.DataFrame, pivots: list[dict]) -> dict:
    markers: list[dict] = []
    observations: list[str] = []
    last = h.iloc[-1]
    prev = h.iloc[-2] if len(h) > 1 else last
    body = abs(last["Close"] - last["Open"])
    full_range = max(last["High"] - last["Low"], 1e-9)
    upper = last["High"] - max(last["Open"], last["Close"])
    lower = min(last["Open"], last["Close"]) - last["Low"]
    body_ratio = body / full_range
    upper_ratio = upper / full_range
    lower_ratio = lower / full_range
    ts = h.index[-1]

    # Volume context for gap classification
    vol_ratio = None
    if "Volume" in h.columns and len(h) >= 20:
        vol_ma20 = h["Volume"].rolling(20).mean().iloc[-1]
        if vol_ma20:
            vol_ratio = float(h["Volume"].iloc[-1]) / float(vol_ma20)

    if len(h) > 1:
        bull_engulf = prev["Close"] < prev["Open"] and last["Close"] > last["Open"] and last["Close"] > prev["Open"] and last["Open"] < prev["Close"]
        bear_engulf = prev["Close"] > prev["Open"] and last["Close"] < last["Open"] and last["Close"] < prev["Open"] and last["Open"] > prev["Close"]
        if bull_engulf:
            observations.append("Bullish engulfing: sudden structural demand pivot.")
            markers.append(_marker(ts, "belowBar", "#22c55e", "arrowUp", "Bull Engulf", "price_action", 4, last["Low"]))
        if bear_engulf:
            observations.append("Bearish engulfing: sudden structural supply pivot.")
            markers.append(_marker(ts, "aboveBar", "#ef4444", "arrowDown", "Bear Engulf", "price_action", 4, last["High"]))

        if last["High"] < prev["High"] and last["Low"] > prev["Low"]:
            observations.append("Inside bar: latest candle is compressed inside the prior auction range.")
            markers.append(_marker(ts, "inBar", "#94a3b8", "square", "Inside", "price_action", 2, last["Close"]))

        if last["Open"] > prev["High"] * 1.003:
            label, obs = _classify_gap("up", h, vol_ratio)
            observations.append(obs)
            markers.append(_marker(ts, "belowBar", "#f97316", "circle", label, "price_action", 3, last["Low"]))
        if last["Open"] < prev["Low"] * 0.997:
            label, obs = _classify_gap("down", h, vol_ratio)
            observations.append(obs)
            markers.append(_marker(ts, "aboveBar", "#38bdf8", "circle", label, "price_action", 3, last["High"]))

    # Pin Bar / Hammer pattern: very small body with single dominant wick (>= 60% of range)
    if lower_ratio >= 0.6 and body_ratio <= 0.3 and upper_ratio <= 0.2:
        observations.append("Hammer / bullish pin bar: capitulation wick rejected from below.")
        markers.append(_marker(ts, "belowBar", "#34d399", "arrowUp", "Hammer", "price_action", 4, last["Low"]))
    elif lower_ratio > 0.55 and body_ratio < 0.35:
        observations.append("Long lower wick: downside price rejection and possible liquidity sweep defense.")
        markers.append(_marker(ts, "belowBar", "#34d399", "arrowUp", "Rejection", "price_action", 3, last["Low"]))
    if upper_ratio >= 0.6 and body_ratio <= 0.3 and lower_ratio <= 0.2:
        observations.append("Shooting star / bearish pin bar: failed auction rejected from above.")
        markers.append(_marker(ts, "aboveBar", "#f87171", "arrowDown", "Pin Bar", "price_action", 4, last["High"]))
    elif upper_ratio > 0.55 and body_ratio < 0.35:
        observations.append("Long upper wick: overhead selling pressure and failed higher auction.")
        markers.append(_marker(ts, "aboveBar", "#f87171", "arrowDown", "Rejection", "price_action", 3, last["High"]))

    if len(h) >= 22:
        prev_low = h["Low"].iloc[-22:-1].min()
        prev_high = h["High"].iloc[-22:-1].max()
        if last["Low"] < prev_low and last["Close"] > prev_low:
            observations.append("Liquidity sweep below recent low with close back inside range.")
            markers.append(_marker(ts, "belowBar", "#a3e635", "arrowUp", "Sweep", "price_action", 5, last["Low"]))
        if last["High"] > prev_high and last["Close"] < prev_high:
            observations.append("Liquidity sweep above recent high with close back inside range.")
            markers.append(_marker(ts, "aboveBar", "#fb7185", "arrowDown", "Sweep", "price_action", 5, last["High"]))

    if len(h) >= 24:
        prev_range = h.iloc[-23:-3]
        range_high = prev_range["High"].max()
        range_low = prev_range["Low"].min()
        prior = h.iloc[-2]
        if prior["Close"] > range_high and last["Close"] < range_high:
            observations.append("Bull trap / fakeout: breakout close failed back into the prior range on the next bar.")
            markers.append(_marker(ts, "aboveBar", "#ef4444", "arrowDown", "Bull Trap", "price_action", 5, last["High"]))
        if prior["Close"] < range_low and last["Close"] > range_low:
            observations.append("Bear trap / fakeout: breakdown close failed back into the prior range on the next bar.")
            markers.append(_marker(ts, "belowBar", "#22c55e", "arrowUp", "Bear Trap", "price_action", 5, last["Low"]))

    if not observations:
        observations.append("No major single-candle reversal, gap, or sweep signal on the latest bar.")

    return _dimension(
        "price_action",
        "computed",
        observations,
        metrics={
            "body_ratio": _round(body_ratio, 3),
            "upper_wick_ratio": _round(upper_ratio, 3),
            "lower_wick_ratio": _round(lower_ratio, 3),
            "latest_close": _round(last["Close"], 2),
            "pivot_count": len(pivots),
        },
        markers=markers,
    )


def _trend_ma(h: pd.DataFrame, *, price_override: Any = None, basis: str = "primary") -> dict:
    close = h["Close"]
    markers: list[dict] = []
    observations: list[str] = []
    levels: list[dict] = []
    ma_periods = [5, 10, 20, 50, 60, 200, 240]
    ma = {f"ma{p}": close.rolling(p).mean() for p in ma_periods}
    # Design checklist also names EMA explicitly ("MA / EMA"). Compute the most
    # common institutional EMAs alongside the SMA matrix so both populations of
    # systematic traders see their reference lines.
    ema_periods = [12, 20, 26, 50]
    ema = {f"ema{p}": close.ewm(span=p, adjust=False).mean() for p in ema_periods}
    latest = {key: _round(series.iloc[-1], 2) if series.notna().any() else None for key, series in ma.items()}
    latest_ema = {key: _round(series.iloc[-1], 2) if series.notna().any() else None for key, series in ema.items()}
    price = _as_float(price_override)
    if price is None:
        price = float(close.iloc[-1])

    available_short = [latest.get("ma5"), latest.get("ma10"), latest.get("ma20")]
    available_struct = [latest.get("ma20"), latest.get("ma60"), latest.get("ma200")]
    bullish_alignment = all(x is not None for x in available_struct) and price > latest["ma20"] > latest["ma60"] > latest["ma200"]
    bearish_alignment = all(x is not None for x in available_struct) and price < latest["ma20"] < latest["ma60"] < latest["ma200"]
    if bullish_alignment:
        observations.append("Bullish MA alignment across price, 20MA, 60MA, and 200MA.")
    elif bearish_alignment:
        observations.append("Bearish MA alignment across price, 20MA, 60MA, and 200MA.")
    elif all(x is not None for x in available_short):
        observations.append("Short-term MA stack is available but macro alignment is mixed or incomplete.")
    else:
        observations.append("Insufficient long-history MA stack for full institutional trend classification.")

    for key, value in latest.items():
        if value is not None:
            period = key.replace("ma", "")
            levels.append({"type": "moving_average", "label": key.upper(), "price": value, "period": int(period)})
    for key, value in latest_ema.items():
        if value is not None:
            period = key.replace("ema", "")
            levels.append({"type": "exponential_ma", "label": key.upper(), "price": value, "period": int(period)})

    for fast_key, slow_key, label in (("ma5", "ma20", "5/20"), ("ma20", "ma60", "20/60"), ("ma60", "ma200", "60/200")):
        fast = ma[fast_key]
        slow = ma[slow_key]
        cross_up = (fast.shift(1) <= slow.shift(1)) & (fast > slow)
        cross_down = (fast.shift(1) >= slow.shift(1)) & (fast < slow)
        recent_up = cross_up[cross_up].tail(3)
        recent_down = cross_down[cross_down].tail(3)
        for ts in recent_up.index:
            markers.append(_marker(ts, "belowBar", "#facc15", "arrowUp", f"GC {label}", "trend_ma", 4))
        for ts in recent_down.index:
            markers.append(_marker(ts, "aboveBar", "#94a3b8", "arrowDown", f"DC {label}", "trend_ma", 4))

    bb_basis = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_basis + 2 * bb_std
    bb_lower = bb_basis - 2 * bb_std
    width = (bb_upper - bb_lower) / bb_basis.replace(0, np.nan)
    latest_width = _as_float(width.iloc[-1]) if width.notna().any() else None
    width_pct = None
    if latest_width is not None and width.dropna().size > 20:
        width_pct = float((width.dropna() <= latest_width).mean() * 100)
        if width_pct <= 15:
            observations.append("Bollinger squeeze: volatility compression is in the lowest historical band.")
            markers.append(_marker(h.index[-1], "inBar", "#38bdf8", "circle", "Squeeze", "trend_ma", 3))
        elif close.iloc[-1] > bb_upper.iloc[-1]:
            observations.append("Walking upper Bollinger band; do not treat overbought alone as a short signal.")
        elif close.iloc[-1] < bb_lower.iloc[-1]:
            observations.append("Walking lower Bollinger band; panic or markdown expansion is active.")

    if _as_float(bb_upper.iloc[-1]) is not None:
        levels.extend([
            {"type": "bollinger", "label": "BB Upper", "price": _round(bb_upper.iloc[-1], 2)},
            {"type": "bollinger", "label": "BB Basis", "price": _round(bb_basis.iloc[-1], 2)},
            {"type": "bollinger", "label": "BB Lower", "price": _round(bb_lower.iloc[-1], 2)},
        ])

    dynamic_touch = None
    if latest.get("ma20"):
        tolerance = max(price * 0.008, 0.01)
        recent_touch = (h["Low"].tail(12) <= ma["ma20"].tail(12) + tolerance) & (h["Close"].tail(12) >= ma["ma20"].tail(12))
        dynamic_touch = int(recent_touch.fillna(False).sum())
        if dynamic_touch >= 2 and price >= latest["ma20"]:
            observations.append("20MA dynamic support has been defended repeatedly in the recent window.")

    metrics = {**latest, **latest_ema, "latest_close": _round(price, 2), "price_vs_ma20_pct": None, "bollinger_width_percentile": _round(width_pct, 1)}
    if latest.get("ma20"):
        metrics["price_vs_ma20_pct"] = _round((price / latest["ma20"] - 1) * 100, 2)
    if latest_ema.get("ema20"):
        metrics["price_vs_ema20_pct"] = _round((price / latest_ema["ema20"] - 1) * 100, 2)
    metrics["dynamic_support_touches_20ma"] = dynamic_touch
    # EMA12/26 cross is the canonical Bollinger / MACD-style fast/slow signal
    if latest_ema.get("ema12") and latest_ema.get("ema26"):
        ema_fast, ema_slow = latest_ema["ema12"], latest_ema["ema26"]
        if ema_fast > ema_slow and price > ema_fast:
            observations.append("EMA12 above EMA26 with price above the faster line; momentum bias up.")
        elif ema_fast < ema_slow and price < ema_fast:
            observations.append("EMA12 below EMA26 with price below the faster line; momentum bias down.")
    metrics["analysis_basis"] = basis
    return _dimension("trend_ma", "computed", observations, metrics=metrics, markers=markers, levels=levels)


def _volume_profile(h: pd.DataFrame) -> dict:
    observations: list[str] = []
    markers: list[dict] = []
    levels: list[dict] = []
    vol = h["Volume"].fillna(0)
    close = h["Close"]
    last = h.iloc[-1]
    vol_ma20 = vol.rolling(20).mean()
    vol_ratio = _as_float(vol.iloc[-1] / vol_ma20.iloc[-1]) if len(vol_ma20.dropna()) else None
    if vol_ratio is not None:
        if vol_ratio >= 2:
            observations.append("Volume spike above 2x 20-day volume MA; institutional effort likely present.")
            markers.append(_marker(h.index[-1], "aboveBar", "#f59e0b", "circle", "Vol Spike", "volume_profile", 3, last["High"]))
        elif vol_ratio <= 0.5:
            observations.append("Volume dry-up below 0.5x 20-day volume MA; supply/demand participation is thin.")
        else:
            observations.append("Volume is near recent average; no standalone institutional effort signal.")

    full_range = max(last["High"] - last["Low"], 1e-9)
    body_ratio = abs(last["Close"] - last["Open"]) / full_range
    if vol_ratio is not None and vol_ratio >= 2 and body_ratio <= 0.25:
        observations.append("Effort vs result divergence: high effort with muted candle body suggests absorption.")
        markers.append(_marker(h.index[-1], "aboveBar", "#f43f5e", "square", "Absorb", "volume_profile", 5, last["High"]))

    profile = []
    lvn_nodes = []
    poc = vah = val = None
    if len(h) >= 20 and vol.sum() > 0:
        low = float(h["Low"].min())
        high = float(h["High"].max())
        if high > low:
            bins = np.linspace(low, high, 25)
            labels = (bins[:-1] + bins[1:]) / 2
            idx = np.digitize(close, bins) - 1
            bucket = np.zeros(len(labels))
            for i, v in zip(idx, vol):
                if 0 <= i < len(bucket):
                    bucket[i] += float(v)
            total = bucket.sum()
            if total > 0:
                poc_i = int(bucket.argmax())
                poc = float(labels[poc_i])
                order = np.argsort(bucket)[::-1]
                covered = 0.0
                selected = []
                for i in order:
                    selected.append(i)
                    covered += bucket[i]
                    if covered / total >= 0.70:
                        break
                val = float(labels[min(selected)])
                vah = float(labels[max(selected)])
                profile = [
                    {"price": _round(labels[i], 2), "volume_share": _round(bucket[i] / total * 100, 2)}
                    for i in order[:6]
                    if bucket[i] > 0
                ]
                # LVN = price vacuums: lowest-volume bins (including zero) inside the
                # traversed range. Restrict to bins between VAL and VAH so we surface
                # internal valleys instead of always-empty edge buckets, and tie-break
                # by distance to the nearest non-zero bin so true vacuum centers beat
                # zero bins that merely touch a busy cluster.
                inside_mask = (labels >= val) & (labels <= vah)
                inside_indices = [i for i in range(len(bucket)) if inside_mask[i]]
                busy_indices = [i for i in range(len(bucket)) if bucket[i] > 0]

                def _dist_to_busy(idx: int) -> int:
                    if not busy_indices:
                        return 0
                    return min(abs(idx - j) for j in busy_indices)

                lvn_order = sorted(
                    inside_indices,
                    key=lambda i: (bucket[i], -_dist_to_busy(i)),
                )
                lvn_nodes = [
                    {"price": _round(labels[i], 2), "volume_share": _round(bucket[i] / total * 100, 2)}
                    for i in lvn_order[:4]
                ]
                levels.extend([
                    {"type": "volume_poc", "label": "POC", "price": _round(poc, 2)},
                    {"type": "value_area", "label": "VAH", "price": _round(vah, 2)},
                    {"type": "value_area", "label": "VAL", "price": _round(val, 2)},
                ])
                for node in profile[:3]:
                    levels.append({"type": "hvn", "label": "HVN", "price": node["price"], "volume_share": node["volume_share"]})
                for node in lvn_nodes[:3]:
                    levels.append({"type": "lvn", "label": "LVN", "price": node["price"], "volume_share": node["volume_share"]})
                observations.append("Volume profile generated from close-price volume allocation approximation.")

    if len(h) >= 40 and vol_ratio is not None and vol_ratio >= 2:
        ret20 = _series_return(close, 20)
        if ret20 is not None and ret20 > 15 and last["Close"] >= close.tail(40).quantile(0.9):
            observations.append("Buying climax watch: price is extended into the upper range with a volume spike.")
            markers.append(_marker(h.index[-1], "aboveBar", "#fb923c", "arrowDown", "Climax", "volume_profile", 4, last["High"]))
        if ret20 is not None and ret20 < -15 and last["Close"] <= close.tail(40).quantile(0.1):
            observations.append("Selling climax watch: capitulation volume appeared near the lower range.")
            markers.append(_marker(h.index[-1], "belowBar", "#38bdf8", "arrowUp", "Climax", "volume_profile", 4, last["Low"]))

    return _dimension(
        "volume_profile",
        "computed" if profile else "partial",
        observations or ["Insufficient volume history for profile construction."],
        metrics={
            "latest_close": _round(last["Close"], 2),
            "volume": _round(vol.iloc[-1], 0),
            "volume_ma20": _round(vol_ma20.iloc[-1], 0) if len(vol_ma20.dropna()) else None,
            "volume_ratio": _round(vol_ratio, 2),
            "poc": _round(poc, 2),
            "vah": _round(vah, 2),
            "val": _round(val, 2),
            "profile_nodes": profile,
            "hvn_nodes": profile[:3],
            "lvn_nodes": lvn_nodes[:3],
        },
        markers=markers,
        levels=levels,
        data_gaps=["True VPVR requires intrabar price-volume distribution; close allocation is an approximation."],
    )


def _momentum(h: pd.DataFrame, pivots: list[dict]) -> dict:
    close = h["Close"]
    rsi = _rsi(close)
    dif, dea, hist = _macd(close)
    k, d = _stochastic(h)
    observations: list[str] = []
    markers: list[dict] = []
    latest_rsi = _as_float(rsi.iloc[-1]) if rsi.notna().any() else None
    if latest_rsi is not None:
        if latest_rsi >= 70:
            observations.append("RSI is above 70; evaluate for momentum embedding before fading.")
        elif latest_rsi <= 30:
            observations.append("RSI is below 30; oversold condition requires structure confirmation.")
        elif latest_rsi >= 50:
            observations.append("RSI is above 50 mid-line; internal momentum is constructive.")
        else:
            observations.append("RSI is below 50 mid-line; internal momentum is weak.")
        if rsi.tail(8).dropna().ge(70).sum() >= 5:
            observations.append("Momentum embedding: RSI stayed above 70 across most recent bars.")

    highs = [p for p in pivots if p["kind"] == "high"]
    lows = [p for p in pivots if p["kind"] == "low"]
    if len(highs) >= 2 and latest_rsi is not None:
        h1, h2 = highs[-2], highs[-1]
        r1 = _as_float(rsi.iloc[h1["index"]])
        r2 = _as_float(rsi.iloc[h2["index"]])
        if h2["price"] > h1["price"] and r1 is not None and r2 is not None and r2 < r1:
            observations.append("Regular bearish divergence: price higher high with RSI lower high.")
            markers.append(_marker(h2["time"], "aboveBar", "#ef4444", "arrowDown", "RSI Div", "momentum", 5, h2["price"]))
        if h2["price"] < h1["price"] and r1 is not None and r2 is not None and r2 > r1:
            observations.append("Hidden bearish divergence: price lower high with RSI higher high, confirming supply pressure.")
            markers.append(_marker(h2["time"], "aboveBar", "#f97316", "arrowDown", "Hidden Div", "momentum", 4, h2["price"]))
    if len(lows) >= 2 and latest_rsi is not None:
        l1, l2 = lows[-2], lows[-1]
        r1 = _as_float(rsi.iloc[l1["index"]])
        r2 = _as_float(rsi.iloc[l2["index"]])
        if l2["price"] < l1["price"] and r1 is not None and r2 is not None and r2 > r1:
            observations.append("Regular bullish divergence: price lower low with RSI higher low.")
            markers.append(_marker(l2["time"], "belowBar", "#22c55e", "arrowUp", "RSI Div", "momentum", 5, l2["price"]))
        if l2["price"] > l1["price"] and r1 is not None and r2 is not None and r2 < r1:
            observations.append("Hidden bullish divergence: price higher low with RSI lower low, confirming trend reset.")
            markers.append(_marker(l2["time"], "belowBar", "#84cc16", "arrowUp", "Hidden Div", "momentum", 4, l2["price"]))

    if _as_float(hist.iloc[-1]) is not None and _as_float(hist.iloc[-2]) is not None:
        if hist.iloc[-1] > hist.iloc[-2] > 0:
            observations.append("MACD histogram expanding above zero; trend acceleration is positive.")
        elif hist.iloc[-1] < hist.iloc[-2] < 0:
            observations.append("MACD histogram expanding below zero; downside acceleration is active.")
    if dif.dropna().size >= 8:
        recent_dif = dif.dropna().tail(8)
        if recent_dif.min() > 0 and recent_dif.iloc[-1] > recent_dif.iloc[-2] and recent_dif.iloc[-2] <= recent_dif.iloc[:-1].median():
            observations.append("MACD zero-line reject: bullish momentum defended the structural mid-line.")
            markers.append(_marker(h.index[-1], "belowBar", "#22d3ee", "circle", "Zero Reject", "momentum", 4, h["Low"].iloc[-1]))
        if recent_dif.max() < 0 and recent_dif.iloc[-1] < recent_dif.iloc[-2] and recent_dif.iloc[-2] >= recent_dif.iloc[:-1].median():
            observations.append("MACD zero-line reject: bearish momentum rejected a recovery attempt below zero.")
            markers.append(_marker(h.index[-1], "aboveBar", "#fb7185", "circle", "Zero Reject", "momentum", 4, h["High"].iloc[-1]))

    return _dimension(
        "momentum",
        "computed",
        observations or ["Momentum signals are neutral or insufficient for divergence classification."],
        metrics={
            "rsi14": _round(latest_rsi, 1),
            "macd_dif": _round(dif.iloc[-1], 3),
            "macd_dea": _round(dea.iloc[-1], 3),
            "macd_hist": _round(hist.iloc[-1], 3),
            "stochastic_k": _round(k.iloc[-1], 1),
            "stochastic_d": _round(d.iloc[-1], 1),
        },
        markers=markers,
    )


def _collapse_same_price_pivots(seq: list[dict]) -> list[dict]:
    """Collapse consecutive same-price pivots produced by flat plateaus."""
    out: list[dict] = []
    for p in seq:
        if out and abs(out[-1]["price"] - p["price"]) < 1e-6:
            continue
        out.append(p)
    return out


def _detect_head_and_shoulders(highs: list[dict], lows: list[dict]) -> Optional[dict]:
    """Detect classic Head & Shoulders on the latest three swing highs.

    Returns a level dict or None. Inverse H&S uses lows instead.
    """
    highs = _collapse_same_price_pivots(highs)
    lows = _collapse_same_price_pivots(lows)
    if len(highs) < 3:
        return None
    s1, head, s2 = highs[-3], highs[-2], highs[-1]
    if head["price"] <= max(s1["price"], s2["price"]):
        return None
    if abs(s1["price"] / s2["price"] - 1) > 0.05:  # shoulders within 5%
        return None
    if (head["price"] - max(s1["price"], s2["price"])) / head["price"] < 0.015:
        return None  # head not meaningfully higher
    # Neckline: lowest swing-low between shoulders
    neckline_candidates = [l for l in lows if s1["index"] < l["index"] < s2["index"]]
    neckline = min(neckline_candidates, key=lambda l: l["price"]) if neckline_candidates else None
    return {
        "type": "head_and_shoulders",
        "direction": "bearish",
        "shoulders": [_round(s1["price"], 2), _round(s2["price"], 2)],
        "head": _round(head["price"], 2),
        "neckline_price": _round(neckline["price"], 2) if neckline else None,
        "neckline_time": _time(neckline["time"]) if neckline else None,
    }


def _detect_inverse_head_and_shoulders(highs: list[dict], lows: list[dict]) -> Optional[dict]:
    highs = _collapse_same_price_pivots(highs)
    lows = _collapse_same_price_pivots(lows)
    if len(lows) < 3:
        return None
    s1, head, s2 = lows[-3], lows[-2], lows[-1]
    if head["price"] >= min(s1["price"], s2["price"]):
        return None
    if abs(s1["price"] / s2["price"] - 1) > 0.05:
        return None
    if (min(s1["price"], s2["price"]) - head["price"]) / max(head["price"], 1e-9) < 0.015:
        return None
    neckline_candidates = [hi for hi in highs if s1["index"] < hi["index"] < s2["index"]]
    neckline = max(neckline_candidates, key=lambda h: h["price"]) if neckline_candidates else None
    return {
        "type": "inverse_head_and_shoulders",
        "direction": "bullish",
        "shoulders": [_round(s1["price"], 2), _round(s2["price"], 2)],
        "head": _round(head["price"], 2),
        "neckline_price": _round(neckline["price"], 2) if neckline else None,
        "neckline_time": _time(neckline["time"]) if neckline else None,
    }


def _detect_double_top_or_bottom(highs: list[dict], lows: list[dict]) -> Optional[dict]:
    """Detect a double top / double bottom on the latest two same-kind swings."""
    highs = _collapse_same_price_pivots(highs)
    lows = _collapse_same_price_pivots(lows)
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if abs(a["price"] / b["price"] - 1) <= 0.02 and b["index"] - a["index"] >= 5:
            # Neckline = lowest low between the two peaks
            between = [lo for lo in lows if a["index"] < lo["index"] < b["index"]]
            neckline = min(between, key=lambda lo: lo["price"]) if between else None
            return {
                "type": "double_top",
                "direction": "bearish",
                "peaks": [_round(a["price"], 2), _round(b["price"], 2)],
                "neckline_price": _round(neckline["price"], 2) if neckline else None,
            }
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if abs(a["price"] / b["price"] - 1) <= 0.02 and b["index"] - a["index"] >= 5:
            between = [hi for hi in highs if a["index"] < hi["index"] < b["index"]]
            neckline = max(between, key=lambda hi: hi["price"]) if between else None
            return {
                "type": "double_bottom",
                "direction": "bullish",
                "valleys": [_round(a["price"], 2), _round(b["price"], 2)],
                "neckline_price": _round(neckline["price"], 2) if neckline else None,
            }
    return None


def _structure_geometry(h: pd.DataFrame, pivots: list[dict]) -> dict:
    observations: list[str] = []
    markers: list[dict] = []
    levels: list[dict] = []
    close = h["Close"]
    price = float(close.iloc[-1])
    highs = [p for p in pivots if p["kind"] == "high"]
    lows = [p for p in pivots if p["kind"] == "low"]
    for p in highs[-3:]:
        levels.append({"type": "resistance", "price": _round(p["price"], 2), "time": _time(p["time"])})
    for p in lows[-3:]:
        levels.append({"type": "support", "price": _round(p["price"], 2), "time": _time(p["time"])})

    # Distinguish BOS (continuation) from ChoCh (first reversal break) per SMC.
    # Use the slope of the most recent same-kind pivots as the prior trend proxy:
    #   - prior trend up   : last two highs ascending OR last two lows ascending
    #   - prior trend down : last two highs descending OR last two lows descending
    def _trend_proxy() -> str:
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1]["price"] > highs[-2]["price"] and lows[-1]["price"] > lows[-2]["price"]:
                return "up"
            if highs[-1]["price"] < highs[-2]["price"] and lows[-1]["price"] < lows[-2]["price"]:
                return "down"
        if len(highs) >= 2 and highs[-1]["price"] > highs[-2]["price"]:
            return "up"
        if len(lows) >= 2 and lows[-1]["price"] < lows[-2]["price"]:
            return "down"
        return "neutral"

    prior_trend = _trend_proxy()
    if highs:
        prev_high = highs[-1]
        if price > prev_high["price"]:
            if prior_trend == "down":
                observations.append("ChoCh up: first break of a swing high while the prior structure was bearish; trend character may flip.")
                markers.append(_marker(h.index[-1], "belowBar", "#22d3ee", "arrowUp", "ChoCh", "structure_geometry", 5, price))
            else:
                observations.append("BOS above latest swing high: continuation structure break.")
                markers.append(_marker(h.index[-1], "belowBar", "#22c55e", "arrowUp", "BOS", "structure_geometry", 5, price))
    if lows:
        prev_low = lows[-1]
        if price < prev_low["price"]:
            if prior_trend == "up":
                observations.append("ChoCh down: first break of a swing low while the prior structure was bullish; trend character may flip.")
                markers.append(_marker(h.index[-1], "aboveBar", "#fb7185", "arrowDown", "ChoCh", "structure_geometry", 5, price))
            else:
                observations.append("BOS below latest swing low: bearish structure break.")
                markers.append(_marker(h.index[-1], "aboveBar", "#ef4444", "arrowDown", "BOS", "structure_geometry", 5, price))

    if len(highs) >= 2:
        prior_high = highs[-2]
        latest_high = highs[-1]
        if latest_high["index"] < len(h) - 1 and price > prior_high["price"] and abs(price / prior_high["price"] - 1) <= 0.015:
            observations.append("S/R flip watch: old swing resistance is being retested from above.")
            levels.append({"type": "sr_flip_support", "price": _round(prior_high["price"], 2), "time": _time(prior_high["time"])})
    if len(lows) >= 2:
        prior_low = lows[-2]
        latest_low = lows[-1]
        if latest_low["index"] < len(h) - 1 and price < prior_low["price"] and abs(price / prior_low["price"] - 1) <= 0.015:
            observations.append("S/R flip watch: old swing support is being retested from below.")
            levels.append({"type": "sr_flip_resistance", "price": _round(prior_low["price"], 2), "time": _time(prior_low["time"])})

    support_slope = resistance_slope = None
    if len(lows) >= 2:
        p1, p2 = lows[-2], lows[-1]
        dx = max(p2["index"] - p1["index"], 1)
        support_slope = (p2["price"] - p1["price"]) / dx
        projected = p2["price"] + support_slope * (len(h) - 1 - p2["index"])
        levels.append({"type": "trendline", "label": "swing_low_support", "price": _round(projected, 2), "slope": _round(support_slope, 4)})
    if len(highs) >= 2:
        p1, p2 = highs[-2], highs[-1]
        dx = max(p2["index"] - p1["index"], 1)
        resistance_slope = (p2["price"] - p1["price"]) / dx
        projected = p2["price"] + resistance_slope * (len(h) - 1 - p2["index"])
        levels.append({"type": "trendline", "label": "swing_high_resistance", "price": _round(projected, 2), "slope": _round(resistance_slope, 4)})

    # Parallel channel: when both trendlines have a comparable slope, anchor a
    # parallel pair using the AVERAGED slope so the upper/lower lines stay
    # equidistant — that is the actual definition of a channel rather than two
    # independently-projected trendlines.
    if support_slope is not None and resistance_slope is not None:
        avg = (support_slope + resistance_slope) / 2
        if abs(support_slope - resistance_slope) <= max(abs(avg) * 0.6, 0.05):
            anchor_low = lows[-1]
            anchor_high = highs[-1]
            lower_projection = anchor_low["price"] + avg * (len(h) - 1 - anchor_low["index"])
            upper_projection = anchor_high["price"] + avg * (len(h) - 1 - anchor_high["index"])
            levels.append({"type": "channel", "label": "channel_lower", "price": _round(lower_projection, 2), "slope": _round(avg, 4)})
            levels.append({"type": "channel", "label": "channel_upper", "price": _round(upper_projection, 2), "slope": _round(avg, 4)})
            if upper_projection > lower_projection:
                if price >= upper_projection * 0.995:
                    observations.append("Price tagging the upper parallel channel; supply-side defense expected.")
                elif price <= lower_projection * 1.005:
                    observations.append("Price tagging the lower parallel channel; demand-side defense expected.")

    # Named chart patterns: Head & Shoulders, Inverse H&S, Double Top/Bottom
    hs = _detect_head_and_shoulders(highs, lows)
    if hs:
        observations.append(
            f"Head & Shoulders top: head {hs['head']}, shoulders {hs['shoulders']}, neckline {hs['neckline_price']}."
        )
        levels.append(hs)
        markers.append(_marker(h.index[-1], "aboveBar", "#ef4444", "arrowDown", "H&S", "structure_geometry", 4, hs["head"]))
    inv_hs = _detect_inverse_head_and_shoulders(highs, lows)
    if inv_hs:
        observations.append(
            f"Inverse Head & Shoulders: head {inv_hs['head']}, shoulders {inv_hs['shoulders']}, neckline {inv_hs['neckline_price']}."
        )
        levels.append(inv_hs)
        markers.append(_marker(h.index[-1], "belowBar", "#22c55e", "arrowUp", "Inv H&S", "structure_geometry", 4, inv_hs["head"]))
    dtb = _detect_double_top_or_bottom(highs, lows)
    if dtb:
        if dtb["direction"] == "bearish":
            observations.append(f"Double top: peaks {dtb['peaks']}, neckline {dtb['neckline_price']}.")
            markers.append(_marker(h.index[-1], "aboveBar", "#ef4444", "arrowDown", "Double Top", "structure_geometry", 4, max(dtb['peaks'])))
        else:
            observations.append(f"Double bottom: valleys {dtb['valleys']}, neckline {dtb['neckline_price']}.")
            markers.append(_marker(h.index[-1], "belowBar", "#22c55e", "arrowUp", "Double Btm", "structure_geometry", 4, min(dtb['valleys'])))
        levels.append(dtb)

    if highs and lows:
        swing_high = max(highs[-5:], key=lambda x: x["price"])
        swing_low = min(lows[-5:], key=lambda x: x["price"])
        low_price, high_price = swing_low["price"], swing_high["price"]
        if high_price > low_price:
            span = high_price - low_price
            fibs = {
                "0.382": high_price - 0.382 * span,
                "0.500": high_price - 0.500 * span,
                "0.618": high_price - 0.618 * span,
                "0.650": high_price - 0.650 * span,
                "1.618_extension": high_price + 0.618 * span,
                "2.618_extension": high_price + 1.618 * span,
            }
            for label, value in fibs.items():
                levels.append({"type": "fibonacci", "label": label, "price": _round(value, 2)})
            gp_low = min(fibs["0.618"], fibs["0.650"])
            gp_high = max(fibs["0.618"], fibs["0.650"])
            if gp_low <= price <= gp_high:
                observations.append("Price is inside the Fibonacci golden pocket.")
                markers.append(_marker(h.index[-1], "belowBar", "#facc15", "circle", "Golden Pocket", "structure_geometry", 4, price))

    if not observations:
        observations.append("Structure levels are mapped; no latest-bar BOS or golden-pocket trigger.")
    return _dimension("structure_geometry", "computed", observations, levels=levels, markers=markers)


def _volatility_risk(h: pd.DataFrame, *, price_override: Any = None, basis: str = "primary") -> dict:
    atr = _atr(h)
    close = h["Close"]
    latest_atr = _as_float(atr.iloc[-1]) if atr.notna().any() else None
    observations: list[str] = []
    metrics: dict[str, Any] = {"atr14": _round(latest_atr, 2)}
    if latest_atr is None:
        return _dimension("volatility_risk", "partial", ["Insufficient history for ATR."], metrics=metrics)
    price = _as_float(price_override)
    if price is None:
        price = float(close.iloc[-1])
    atr_pct = latest_atr / price * 100 if price else None
    atr_ma20 = atr.rolling(20).mean().iloc[-1]
    atr_percentile = None
    if atr.dropna().size >= 20:
        atr_percentile = float((atr.dropna() <= latest_atr).mean() * 100)
    if _as_float(atr_ma20):
        if latest_atr > atr_ma20 * 1.2:
            observations.append("Volatility expansion: ATR is materially above its 20-period average.")
        elif latest_atr < atr_ma20 * 0.8:
            observations.append("Volatility compression: ATR is materially below its 20-period average.")
        else:
            observations.append("ATR is near its recent volatility baseline.")
    metrics.update({
        "atr_pct": _round(atr_pct, 2),
        "atr_percentile": _round(atr_percentile, 1),
        "stop_2atr_long": _round(price - 2 * latest_atr, 2),
        "stop_3atr_long": _round(price - 3 * latest_atr, 2),
        "stop_2atr_short": _round(price + 2 * latest_atr, 2),
        "stop_3atr_short": _round(price + 3 * latest_atr, 2),
        "analysis_basis": basis,
    })
    levels = [
        {"type": "atr_stop", "label": "2ATR Long Stop", "price": metrics["stop_2atr_long"]},
        {"type": "atr_stop", "label": "3ATR Long Stop", "price": metrics["stop_3atr_long"]},
        {"type": "atr_stop", "label": "2ATR Short Stop", "price": metrics["stop_2atr_short"]},
        {"type": "atr_stop", "label": "3ATR Short Stop", "price": metrics["stop_3atr_short"]},
    ]
    severity_value = 0.0
    if atr_percentile is not None and atr_percentile >= 85:
        severity_value += 1.0
    if atr_pct is not None and atr_pct >= 5:
        severity_value += 1.0
    return _dimension("volatility_risk", "computed", observations, metrics=metrics, levels=levels, severity=_severity_from_value(severity_value))


def _intraday_trend_direction(h: Optional[pd.DataFrame], lookback: int = 24) -> Optional[str]:
    """Return 'up' / 'down' / 'flat' from the last `lookback` intraday closes."""
    if h is None or len(h) < lookback:
        return None
    close = h["Close"].tail(lookback)
    ret = float(close.iloc[-1] / close.iloc[0] - 1)
    if ret > 0.01:
        return "up"
    if ret < -0.01:
        return "down"
    return "flat"


def _mtf_derivatives(
    h: pd.DataFrame,
    derivatives: Optional[dict],
    *,
    intraday_1h: Optional[pd.DataFrame] = None,
    intraday_15m: Optional[pd.DataFrame] = None,
) -> dict:
    observations: list[str] = []
    close = h["Close"]
    daily_ret = _series_return(close, 20)
    weekly = h.resample("W").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    monthly = h.resample("ME").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    weekly_ret = _series_return(weekly["Close"], 10) if len(weekly) else None
    monthly_ret = _series_return(monthly["Close"], 6) if len(monthly) else None

    # Execution timeframes: 1H / 15M trend direction as design demands
    dir_1h = _intraday_trend_direction(intraday_1h, lookback=24)  # ~1 trading day of 1H bars
    dir_15m = _intraday_trend_direction(intraday_15m, lookback=26)  # ~1 trading day of 15M bars
    if dir_1h:
        observations.append(f"1H execution timeframe trend: {dir_1h}.")
    if dir_15m:
        observations.append(f"15M execution timeframe trend: {dir_15m}.")

    alignment = "mixed"
    rets = [x for x in (daily_ret, weekly_ret, monthly_ret) if x is not None]
    if daily_ret is not None and weekly_ret is not None:
        if all(x > 0 for x in rets):
            alignment = "bullish"
            observations.append("Daily, weekly, and available monthly structures are aligned upward.")
        elif all(x < 0 for x in rets):
            alignment = "bearish"
            observations.append("Daily, weekly, and available monthly structures are aligned downward.")
        else:
            observations.append("Daily and weekly structures are not aligned.")
    else:
        observations.append("Multi-timeframe price aggregation is partial due to limited history.")

    # Cross-timeframe synchronicity check (macro daily/weekly vs micro 1H/15M)
    micro_signs = [d for d in (dir_1h, dir_15m) if d in {"up", "down"}]
    if alignment != "mixed" and micro_signs:
        macro_sign = "up" if alignment == "bullish" else "down"
        if all(s == macro_sign for s in micro_signs):
            observations.append("Macro and intraday timeframes synchronized; high-velocity setup window.")
        elif any(s != macro_sign for s in micro_signs):
            observations.append("Macro and intraday timeframes diverge; trade entries should wait for re-alignment.")

    data_gaps = []
    status = "computed"
    derivative_notes = []
    if not derivatives:
        data_gaps.append("Open interest, funding rates, and implied volatility are not configured for this asset.")
        status = "partial"
    else:
        oi_change = _as_float(derivatives.get("open_interest_change_pct"))
        funding = _as_float(derivatives.get("funding_rate"))
        iv_rank = _as_float(derivatives.get("iv_rank"))
        if oi_change is not None and abs(oi_change) >= 10:
            derivative_notes.append(f"Open interest changed {round(oi_change, 2)}%, implying leverage participation.")
        if funding is not None and abs(funding) >= 0.03:
            derivative_notes.append("Funding is stretched; crowded derivatives positioning risk is elevated.")
        if iv_rank is not None and iv_rank >= 80:
            derivative_notes.append("Implied volatility rank is elevated; options market prices a large move.")
        # Short Squeeze: sharp daily up move + stretched negative funding
        recent_ret_3d = _series_return(close, 3)
        if recent_ret_3d is not None and recent_ret_3d >= 6 and funding is not None and funding <= -0.02:
            derivative_notes.append(
                f"Short squeeze warning: 3-day return {round(recent_ret_3d, 2)}% with negative funding {round(funding, 4)}."
            )
        # OI Accumulation: OI rising fast while ATR percentile compressed
        if oi_change is not None and oi_change >= 20 and len(h) >= 20:
            atr = _atr(h)
            if atr.dropna().size >= 20:
                latest_atr = _as_float(atr.iloc[-1])
                pct = float((atr.dropna() <= latest_atr).mean() * 100)
                if pct <= 30:
                    derivative_notes.append(
                        f"OI accumulation: OI up {round(oi_change, 2)}% while ATR sits at the {round(pct, 1)}th percentile."
                    )
        observations.extend(derivative_notes or ["Derivative payload supplied without extreme OI/funding/IV flags."])
    if intraday_1h is None and intraday_15m is None:
        data_gaps.append("Intraday 1H/15M execution-timeframe data unavailable; macro alignment only.")
    return _dimension(
        "mtf_derivatives",
        status,
        observations,
        metrics={
            "daily_20d_return_pct": _round(daily_ret, 2),
            "weekly_10w_return_pct": _round(weekly_ret, 2),
            "monthly_6m_return_pct": _round(monthly_ret, 2),
            "timeframe_alignment": alignment,
            "intraday_1h_direction": dir_1h,
            "intraday_15m_direction": dir_15m,
            "derivatives": derivatives or {},
        },
        data_gaps=data_gaps,
    )


def _cvd_divergence(cvd_series, price_series) -> Optional[str]:
    """Compare last two swings of price vs CVD; return 'bearish' / 'bullish' / None."""
    if cvd_series is None or price_series is None:
        return None
    try:
        cvd = pd.Series([float(v) for _, v in cvd_series])
        price = pd.Series([float(p) for _, p in price_series])
    except Exception:
        return None
    if len(cvd) < 6 or len(price) < 6 or len(cvd) != len(price):
        return None
    # Compare halves: did price make a higher high while CVD didn't? (bearish div)
    p_first_max = price.iloc[: len(price) // 2].max()
    p_second_max = price.iloc[len(price) // 2:].max()
    c_first_max = cvd.iloc[: len(cvd) // 2].max()
    c_second_max = cvd.iloc[len(cvd) // 2:].max()
    if p_second_max > p_first_max and c_second_max <= c_first_max:
        return "bearish"
    p_first_min = price.iloc[: len(price) // 2].min()
    p_second_min = price.iloc[len(price) // 2:].min()
    c_first_min = cvd.iloc[: len(cvd) // 2].min()
    c_second_min = cvd.iloc[len(cvd) // 2:].min()
    if p_second_min < p_first_min and c_second_min >= c_first_min:
        return "bullish"
    return None


def _microstructure(order_flow: Optional[dict]) -> dict:
    if not order_flow:
        return _dimension(
            "microstructure_orderflow",
            "unavailable",
            ["Footprint, CVD, liquidation pools, and aggressive buy/sell imbalance require tick/order-flow data."],
            data_gaps=["tick prints", "aggressive buy/sell classification", "CVD", "liquidation heatmap"],
        )
    observations = ["Order-flow payload supplied."]
    buy_volume = _as_float(order_flow.get("aggressive_buy_volume") or order_flow.get("buy_volume"))
    sell_volume = _as_float(order_flow.get("aggressive_sell_volume") or order_flow.get("sell_volume"))
    cvd_delta = _as_float(order_flow.get("cvd_delta") or order_flow.get("delta"))
    liquidation_distance = _as_float(order_flow.get("nearest_liquidation_distance_pct"))
    imbalance_ratio = None
    if buy_volume is not None and sell_volume is not None and sell_volume:
        imbalance_ratio = buy_volume / sell_volume
        if imbalance_ratio >= 3:
            observations.append("Aggressive buy imbalance exceeds 300%; institutional forcing block is possible.")
        elif imbalance_ratio <= 1 / 3:
            observations.append("Aggressive sell imbalance exceeds 300%; supply forcing block is possible.")
    if cvd_delta is not None:
        observations.append("CVD delta is positive; aggressive buyers dominate." if cvd_delta > 0 else "CVD delta is negative; aggressive sellers dominate.")
    # CVD vs price divergence when caller supplies parallel timeseries
    divergence = _cvd_divergence(order_flow.get("cvd_series"), order_flow.get("price_series"))
    if divergence == "bearish":
        observations.append("CVD divergence: price prints a higher high while CVD fails to confirm — fragile rally.")
    elif divergence == "bullish":
        observations.append("CVD divergence: price prints a lower low while CVD fails to confirm — fading selling pressure.")
    if liquidation_distance is not None and liquidation_distance <= 2:
        observations.append("Nearby liquidation pool is within 2%; price magnet risk is elevated.")
    metrics = {
        **{k: v for k, v in order_flow.items() if k not in {"cvd_series", "price_series"}},
        "computed_imbalance_ratio": _round(imbalance_ratio, 2),
        "cvd_divergence": divergence,
    }
    return _dimension("microstructure_orderflow", "computed", observations, metrics=metrics)


def _intermarket(
    h: pd.DataFrame,
    benchmark_close: Optional[pd.Series],
    *,
    benchmarks: Optional[dict] = None,
) -> dict:
    if benchmark_close is None or len(benchmark_close.dropna()) < 25:
        return _dimension(
            "intermarket_correlation",
            "partial",
            ["Benchmark data unavailable; relative strength and lead/lag signals cannot be fully ranked."],
            data_gaps=["benchmark close series"],
        )
    asset = h["Close"].rename("asset")
    bench = benchmark_close.copy().rename("benchmark")
    bench.index = pd.to_datetime(bench.index).tz_localize(None)
    aligned = pd.concat([asset, bench], axis=1).dropna()
    observations: list[str] = []
    if len(aligned) < 25:
        return _dimension("intermarket_correlation", "partial", ["Insufficient overlapping asset/benchmark history."], data_gaps=["overlapping benchmark dates"])
    asset_ret20 = _series_return(aligned["asset"], 20)
    bench_ret20 = _series_return(aligned["benchmark"], 20)
    alpha20 = asset_ret20 - bench_ret20 if asset_ret20 is not None and bench_ret20 is not None else None
    corr60 = aligned["asset"].pct_change().tail(60).corr(aligned["benchmark"].pct_change().tail(60))
    if alpha20 is not None:
        observations.append("Asset is outperforming benchmark over 20 sessions." if alpha20 > 0 else "Asset is underperforming benchmark over 20 sessions.")

    # Multi-benchmark overlay: design names DXY, US10Y, VIX explicitly so we
    # compute 20-day asset alpha vs each reference that is present.
    cross_alpha: dict[str, Optional[float]] = {}
    levels_list: list[dict] = []
    if benchmarks:
        for label, series in benchmarks.items():
            if series is None or len(series.dropna()) < 25:
                continue
            ref = series.copy()
            ref.index = pd.to_datetime(ref.index).tz_localize(None)
            joined = pd.concat([asset, ref.rename(label)], axis=1).dropna()
            if len(joined) < 25:
                continue
            asset_ret = _series_return(joined["asset"], 20)
            ref_ret = _series_return(joined[label], 20)
            if asset_ret is None or ref_ret is None:
                continue
            cross_alpha[label] = round(asset_ret - ref_ret, 2)
        # Sector ratio for Risk-On/Risk-Off (XLK vs XLV) when present
        xlk = benchmarks.get("xlk")
        xlv = benchmarks.get("xlv")
        if xlk is not None and xlv is not None and len(xlk.dropna()) >= 25 and len(xlv.dropna()) >= 25:
            joined = pd.concat([xlk.rename("xlk"), xlv.rename("xlv")], axis=1).dropna()
            if len(joined) >= 25:
                ratio = (joined["xlk"] / joined["xlv"]).dropna()
                ratio_ma20 = ratio.rolling(20).mean()
                if len(ratio_ma20.dropna()):
                    cur = float(ratio.iloc[-1])
                    avg = float(ratio_ma20.iloc[-1])
                    regime = "risk_on" if cur > avg else "risk_off"
                    cross_alpha["xlk_xlv_ratio"] = round(cur, 4)
                    cross_alpha["xlk_xlv_regime"] = regime
                    observations.append(
                        f"XLK/XLV ratio {round(cur, 3)} vs 20-day mean {round(avg, 3)}: {regime.replace('_', ' ')}."
                    )
        # VIX context
        vix = benchmarks.get("vix")
        if vix is not None and len(vix.dropna()) >= 20:
            vix_latest = _as_float(vix.iloc[-1])
            vix_ma20 = float(vix.tail(20).mean())
            if vix_latest is not None:
                cross_alpha["vix_latest"] = round(vix_latest, 2)
                if vix_latest >= max(vix_ma20 * 1.4, 25):
                    observations.append(f"VIX {round(vix_latest, 2)} elevated vs 20-day mean {round(vix_ma20, 2)}; macro risk-off pressure.")

    return _dimension(
        "intermarket_correlation",
        "computed",
        observations,
        metrics={
            "asset_20d_return_pct": _round(asset_ret20, 2),
            "benchmark_20d_return_pct": _round(bench_ret20, 2),
            "alpha_20d_pct": _round(alpha20, 2),
            "correlation_60d": _round(corr60, 3),
            "cross_benchmarks_20d_alpha": cross_alpha,
        },
        levels=levels_list,
    )


def _breadth(breadth: Optional[dict]) -> dict:
    if not breadth:
        return _dimension(
            "breadth_internals",
            "unavailable",
            ["A/D line and percent-above-MA signals require index constituent breadth data."],
            data_gaps=["advance-decline line", "percent above 50MA", "percent above 200MA"],
        )
    observations = ["Breadth payload supplied."]
    above_50 = _as_float(breadth.get("percent_above_50ma") or breadth.get("above_50ma_pct"))
    above_200 = _as_float(breadth.get("percent_above_200ma") or breadth.get("above_200ma_pct"))
    ad_divergence = breadth.get("ad_line_divergence") or breadth.get("breadth_divergence")
    # Native A/D ratio + cumulative line when caller supplies advancing/declining counts
    up = _as_float(breadth.get("advancing"))
    down = _as_float(breadth.get("declining"))
    ad_ratio = None
    if up is not None and down is not None and down:
        ad_ratio = up / down
        if ad_ratio >= 3:
            observations.append(f"A/D ratio {round(ad_ratio, 2)} — broad-market thrust (advancers >> decliners).")
        elif ad_ratio <= 1 / 3:
            observations.append(f"A/D ratio {round(ad_ratio, 2)} — broad-market capitulation (decliners >> advancers).")
        else:
            observations.append(f"A/D ratio {round(ad_ratio, 2)} — neutral breadth.")
    ad_line_series = breadth.get("ad_line_series") or breadth.get("ad_line")
    ad_line_slope = None
    if isinstance(ad_line_series, list) and len(ad_line_series) >= 5:
        recent = [float(x) for _, x in ad_line_series[-5:]] if isinstance(ad_line_series[0], (list, tuple)) else [float(x) for x in ad_line_series[-5:]]
        if len(recent) >= 2:
            ad_line_slope = recent[-1] - recent[0]
            if ad_line_slope < 0 and (above_50 is None or above_50 > 50):
                observations.append("Cumulative A/D Line is rolling over while index breadth remains elevated — hollow prosperity warning.")
    if above_50 is not None:
        if above_50 >= 85:
            observations.append("Percent above 50MA is above 85%; broad-market overbought risk is active.")
        elif above_50 <= 15:
            observations.append("Percent above 50MA is below 15%; broad-market capitulation watch is active.")
    if above_200 is not None and above_200 <= 25:
        observations.append("Long-term breadth is structurally weak; index rallies may be narrow.")
    if ad_divergence:
        observations.append(f"A/D breadth divergence flagged: {ad_divergence}.")
    metrics = {**breadth, "ad_ratio": _round(ad_ratio, 2), "ad_line_slope": _round(ad_line_slope, 2)}
    return _dimension("breadth_internals", "computed", observations, metrics=metrics)


def _opening_range(intraday_5m: Optional[pd.DataFrame]) -> Optional[dict]:
    """Compute the first 30-minute Opening Range high/low and cleared direction."""
    if intraday_5m is None or len(intraday_5m) < 6:
        return None
    last_date = intraday_5m.index[-1].date()
    today = intraday_5m[intraday_5m.index.date == last_date]
    if len(today) < 6:
        return None
    opening = today.iloc[:6]  # 6 × 5min = 30 minutes
    or_high = float(opening["High"].max())
    or_low = float(opening["Low"].min())
    later = today.iloc[6:]
    cleared = "pending"
    if len(later):
        last_close = float(later["Close"].iloc[-1])
        if last_close > or_high:
            cleared = "up"
        elif last_close < or_low:
            cleared = "down"
        else:
            cleared = "inside"
    return {
        "or_high": round(or_high, 2),
        "or_low": round(or_low, 2),
        "cleared": cleared,
        "date": last_date.isoformat(),
    }


def _time_cyclical(
    h: pd.DataFrame,
    anchors: Optional[list[dict]],
    *,
    intraday_5m: Optional[pd.DataFrame] = None,
) -> dict:
    observations: list[str] = []
    markers: list[dict] = []
    levels: list[dict] = []
    close = h["Close"]
    vol = h["Volume"].fillna(0)
    anchor_points = anchors or []
    if not anchor_points and len(h) >= 20:
        low_idx = h["Low"].idxmin()
        anchor_points = [{"label": "range_low", "time": low_idx}]
    avwap_values = []
    for anchor in anchor_points:
        anchor_ts = pd.Timestamp(anchor.get("time")).tz_localize(None)
        scoped = h[h.index >= anchor_ts]
        if len(scoped) and scoped["Volume"].sum() > 0:
            pv = (scoped["Close"] * scoped["Volume"]).cumsum()
            vv = scoped["Volume"].cumsum().replace(0, np.nan)
            avwap = pv / vv
            latest = _as_float(avwap.iloc[-1])
            avwap_values.append({"label": anchor.get("label", "anchor"), "time": _time(anchor_ts), "avwap": _round(latest, 2)})
            levels.append({"type": "avwap", "label": anchor.get("label", "anchor"), "price": _round(latest, 2), "time": _time(anchor_ts)})
            markers.append(_marker(anchor_ts, "belowBar", "#38bdf8", "circle", "AVWAP", "time_cyclical", 3))
            if latest and close.iloc[-1] >= latest:
                observations.append(f"Price is above anchored VWAP from {anchor.get('label', 'anchor')}.")
            elif latest:
                observations.append(f"Price is below anchored VWAP from {anchor.get('label', 'anchor')}.")
    if not avwap_values:
        observations.append("AVWAP requires volume history and a valid anchor event.")

    opening_range = _opening_range(intraday_5m)
    data_gaps = []
    if opening_range:
        levels.append({"type": "opening_range_high", "label": "OR High", "price": opening_range["or_high"]})
        levels.append({"type": "opening_range_low", "label": "OR Low", "price": opening_range["or_low"]})
        observations.append(
            f"30-min Opening Range {opening_range['or_low']}–{opening_range['or_high']} (cleared {opening_range['cleared']})."
        )
    else:
        data_gaps.append("Opening range requires intraday 5m session candles for the current trading day.")
    return _dimension(
        "time_cyclical",
        "computed" if (avwap_values or opening_range) else "partial",
        observations,
        metrics={"avwap": avwap_values, "opening_range": opening_range},
        markers=markers,
        levels=levels,
        data_gaps=data_gaps,
    )


def _ratio_distance(value: Optional[float], target: float) -> Optional[float]:
    if value is None:
        return None
    return abs(value - target) / max(target, 1e-9)


def _harmonic_candidate(pivots: list[dict], strict_tolerance: float = 0.05) -> Optional[dict]:
    """XABCD harmonic candidate with strict ratio tolerance.

    `strict_tolerance` is the maximum allowed average normalized distance
    between observed ratios and template ratios. Default 0.05 (5%) keeps the
    classifier from labelling random pivots as Bat/Butterfly/Gartley.
    """
    if len(pivots) < 5:
        return None
    points = pivots[-5:]
    prices = [float(p["price"]) for p in points]
    x, a, b, c, d = prices
    xa = abs(a - x)
    ab = abs(b - a)
    bc = abs(c - b)
    cd = abs(d - c)
    ad = abs(d - a)
    if min(xa, ab, bc, cd) <= 0:
        return None
    ratios = {
        "ab_xa": ab / xa,
        "bc_ab": bc / ab,
        "cd_bc": cd / bc,
        "ad_xa": ad / xa,
    }
    templates = {
        "Gartley": {"ab_xa": 0.618, "bc_ab": 0.382, "ad_xa": 0.786, "cd_bc": 1.272},
        "Bat":     {"ab_xa": 0.450, "bc_ab": 0.382, "ad_xa": 0.886, "cd_bc": 1.618},
        "Butterfly": {"ab_xa": 0.786, "bc_ab": 0.382, "ad_xa": 1.272, "cd_bc": 1.618},
    }
    scored = []
    for name, targets in templates.items():
        distances = [_ratio_distance(ratios.get(k), target) for k, target in targets.items()]
        distances = [x for x in distances if x is not None]
        if distances:
            avg_distance = sum(distances) / len(distances)
            scored.append((avg_distance, name))
    if not scored:
        return None
    distance, name = min(scored)
    # Strict gate: only classify when ALL targeted ratios are within tolerance
    if distance > strict_tolerance:
        return None
    direction = "bullish_prz" if d < c else "bearish_prz"
    return {
        "type": "harmonic_candidate",
        "pattern": name,
        "direction": direction,
        "confidence": _round(max(0.0, 1 - distance / strict_tolerance), 2),
        "ratios": {key: _round(value, 3) for key, value in ratios.items()},
        "prz": _round(d, 2),
        "points": [{"label": label, "kind": p["kind"], "price": _round(p["price"], 2), "time": _time(p["time"])} for label, p in zip("XABCD", points)],
    }


def _advanced_geometries(h: pd.DataFrame, pivots: list[dict]) -> dict:
    observations: list[str] = []
    markers: list[dict] = []
    levels: list[dict] = []
    for i in range(1, len(h) - 1):
        prev_bar = h.iloc[i - 1]
        next_bar = h.iloc[i + 1]
        ts = h.index[i]
        if prev_bar["High"] < next_bar["Low"]:
            levels.append({"type": "bullish_fvg", "low": _round(prev_bar["High"], 2), "high": _round(next_bar["Low"], 2), "time": _time(ts)})
        if prev_bar["Low"] > next_bar["High"]:
            levels.append({"type": "bearish_fvg", "low": _round(next_bar["High"], 2), "high": _round(prev_bar["Low"], 2), "time": _time(ts)})
    for zone in levels[-5:]:
        marker_price = zone.get("low")
        markers.append(_marker(pd.to_datetime(zone["time"], unit="s"), "inBar", "#a78bfa", "square", "FVG", "advanced_geometries", 3, marker_price))
    if levels:
        observations.append("Fair Value Gaps mapped as three-candle imbalance zones.")
    # FVG Fill: latest candle re-enters the price range of a previously mapped
    # FVG → high-probability rebalance event per the design ("price has a high
    # probability of returning to fill this imbalance range").
    if len(h) >= 1 and levels:
        last_bar = h.iloc[-1]
        for zone in reversed(levels):
            ztype = zone.get("type", "")
            if not ztype.endswith("fvg"):
                continue
            zlow = _as_float(zone.get("low"))
            zhigh = _as_float(zone.get("high"))
            if zlow is None or zhigh is None:
                continue
            if last_bar["Low"] <= zhigh and last_bar["High"] >= zlow:
                direction = "bullish" if ztype == "bullish_fvg" else "bearish"
                observations.append(
                    f"FVG fill: latest bar re-entered a {direction} FVG zone {round(zlow, 2)}–{round(zhigh, 2)}."
                )
                shape = "arrowUp" if direction == "bullish" else "arrowDown"
                position = "belowBar" if direction == "bullish" else "aboveBar"
                color = "#22c55e" if direction == "bullish" else "#ef4444"
                markers.append(_marker(h.index[-1], position, color, shape, "FVG Fill", "advanced_geometries", 4, (zlow + zhigh) / 2))
                break
    if len(pivots) >= 5:
        harmonic = _harmonic_candidate(pivots)
        if harmonic:
            observations.append(f"{harmonic['pattern']} harmonic candidate mapped with PRZ near {harmonic['prz']}.")
            levels.append(harmonic)
            marker_position = "belowBar" if harmonic["direction"] == "bullish_prz" else "aboveBar"
            marker_shape = "arrowUp" if harmonic["direction"] == "bullish_prz" else "arrowDown"
            marker_color = "#8b5cf6" if harmonic["direction"] == "bullish_prz" else "#c084fc"
            markers.append(_marker(h.index[-1], marker_position, marker_color, marker_shape, harmonic["pattern"], "advanced_geometries", 4, harmonic["prz"]))
        else:
            observations.append("Latest five pivots are available, but harmonic ratios do not meet strict candidate thresholds.")
    else:
        observations.append("Insufficient confirmed pivots for harmonic XABCD screening.")
    return _dimension("advanced_geometries", "computed", observations, markers=markers, levels=levels)


def _options_gex(options_profile: Optional[dict]) -> dict:
    if not options_profile:
        return _dimension(
            "options_gex",
            "unavailable",
            ["Gamma flip, gamma wall, and charm magnet require options chain and market-maker exposure data."],
            data_gaps=["options open interest", "gamma by strike", "expiration calendar"],
        )
    observations = ["Options profile supplied."]
    levels: list[dict] = []
    spot = _as_float(options_profile.get("spot") or options_profile.get("underlying_price"))
    gamma_flip = _as_float(options_profile.get("gamma_flip"))
    gamma_wall = _as_float(options_profile.get("gamma_wall") or options_profile.get("absolute_gamma_wall"))
    max_pain = _as_float(options_profile.get("max_pain"))
    regime = options_profile.get("gamma_regime")
    if gamma_flip is not None:
        levels.append({"type": "gamma_flip", "label": "Gamma Flip", "price": _round(gamma_flip, 2)})
        if spot is not None:
            observations.append("Spot is above gamma flip." if spot > gamma_flip else "Spot is below gamma flip.")
    if gamma_wall is not None:
        levels.append({"type": "gamma_wall", "label": "Gamma Wall", "price": _round(gamma_wall, 2)})
        observations.append("Absolute gamma wall / charm magnet is mapped.")
    if max_pain is not None:
        levels.append({"type": "max_pain", "label": "Max Pain", "price": _round(max_pain, 2)})
    if regime:
        observations.append(f"Gamma regime: {regime}.")
    return _dimension("options_gex", "computed", observations, metrics=options_profile, levels=levels)


def _order_book(order_book: Optional[dict]) -> dict:
    if not order_book:
        return _dimension(
            "order_book",
            "unavailable",
            ["Spoofing, layering, liquidity pockets, and iceberg absorption require Level 2/3 order-book snapshots."],
            data_gaps=["level 2 order book", "historical resting liquidity", "trade-to-book execution data"],
        )
    observations = ["Order-book payload supplied."]
    levels: list[dict] = []
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    bid_depth = sum(_as_float(row.get("size") if isinstance(row, dict) else row[1]) or 0 for row in bids[:10])
    ask_depth = sum(_as_float(row.get("size") if isinstance(row, dict) else row[1]) or 0 for row in asks[:10])
    imbalance = bid_depth / ask_depth if ask_depth else None
    if imbalance is not None:
        if imbalance >= 2:
            observations.append("Visible bid depth exceeds ask depth by 2x; support wall is active.")
        elif imbalance <= 0.5:
            observations.append("Visible ask depth exceeds bid depth by 2x; supply wall is active.")
    for row in bids[:3]:
        price = row.get("price") if isinstance(row, dict) else row[0]
        size = row.get("size") if isinstance(row, dict) else row[1]
        levels.append({"type": "bid_wall", "label": "Bid Wall", "price": _round(price, 2), "size": _round(size, 0)})
    for row in asks[:3]:
        price = row.get("price") if isinstance(row, dict) else row[0]
        size = row.get("size") if isinstance(row, dict) else row[1]
        levels.append({"type": "ask_wall", "label": "Ask Wall", "price": _round(price, 2), "size": _round(size, 0)})
    metrics = {**order_book, "top10_bid_depth": _round(bid_depth, 0), "top10_ask_depth": _round(ask_depth, 0), "book_imbalance": _round(imbalance, 2)}
    return _dimension("order_book", "computed", observations, metrics=metrics, levels=levels)


def _statistical_reversion(h: pd.DataFrame) -> dict:
    close = h["Close"].dropna()
    if len(close) < 30:
        return _dimension("statistical_reversion", "partial", ["Insufficient history for regression channel."], data_gaps=["30+ closing prices"])
    y = close.tail(min(len(close), 120)).to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    residual = y - fitted
    sigma = residual.std(ddof=1) if len(residual) > 1 else 0
    latest_z = residual[-1] / sigma if sigma else 0
    latest_fit = fitted[-1]
    observations = []
    markers = []
    levels = [
        {"type": "regression", "label": "Mean", "price": _round(latest_fit, 2)},
        {"type": "regression", "label": "+1σ", "price": _round(latest_fit + sigma, 2)},
        {"type": "regression", "label": "-1σ", "price": _round(latest_fit - sigma, 2)},
        {"type": "regression", "label": "+2σ", "price": _round(latest_fit + 2 * sigma, 2)},
        {"type": "regression", "label": "-2σ", "price": _round(latest_fit - 2 * sigma, 2)},
        {"type": "regression", "label": "+3σ", "price": _round(latest_fit + 3 * sigma, 2)},
        {"type": "regression", "label": "-3σ", "price": _round(latest_fit - 3 * sigma, 2)},
    ]
    if latest_z >= 3:
        observations.append("Severe statistical overextension above +3 sigma; tail-risk mean reversion is the dominant base case.")
        markers.append(_marker(close.index[-1], "aboveBar", "#dc2626", "arrowDown", "+3σ", "statistical_reversion", 5, close.iloc[-1]))
    elif latest_z >= 2:
        observations.append("Statistical overextension above +2 sigma; tail-risk mean reversion watch.")
        markers.append(_marker(close.index[-1], "aboveBar", "#f97316", "arrowDown", "+2σ", "statistical_reversion", 4, close.iloc[-1]))
    elif latest_z <= -3:
        observations.append("Severe statistical overextension below -3 sigma; capitulation mean reversion is the dominant base case.")
        markers.append(_marker(close.index[-1], "belowBar", "#2563eb", "arrowUp", "-3σ", "statistical_reversion", 5, close.iloc[-1]))
    elif latest_z <= -2:
        observations.append("Statistical overextension below -2 sigma; capitulation mean reversion watch.")
        markers.append(_marker(close.index[-1], "belowBar", "#38bdf8", "arrowUp", "-2σ", "statistical_reversion", 4, close.iloc[-1]))
    else:
        observations.append("Price remains inside the primary regression channel.")
    return _dimension(
        "statistical_reversion",
        "computed",
        observations,
        metrics={"regression_slope": _round(slope, 4), "latest_z_score": _round(latest_z, 2), "sigma": _round(sigma, 2), "regression_mean": _round(latest_fit, 2)},
        markers=markers,
        levels=levels,
    )


def _macro_wave(h: pd.DataFrame, pivots: list[dict]) -> dict:
    close = h["Close"]
    ma60 = close.rolling(60).mean()
    vol = h["Volume"].fillna(0) if "Volume" in h.columns else pd.Series(0, index=h.index)
    observations: list[str] = []
    markers: list[dict] = []
    phase = "unknown"
    if ma60.notna().sum() >= 20:
        slope = ma60.iloc[-1] - ma60.iloc[-20]
        price = close.iloc[-1]
        rel_slope = abs(slope / ma60.iloc[-1]) if ma60.iloc[-1] else 0.0
        # Flat 60MA wins first: a barely-positive slope must not pre-empt the
        # range branch, otherwise accumulation/distribution can never resolve.
        if rel_slope < 0.02:
            # Range: distinguish accumulation vs distribution by where rising-volume
            # candles cluster. Accumulation = effort concentrated at the range low;
            # Distribution = effort concentrated at the range high.
            window = h.tail(80) if len(h) >= 80 else h
            if len(window) >= 20 and float(window["Volume"].sum()) > 0:
                low_band = window["Low"].quantile(0.30)
                high_band = window["High"].quantile(0.70)
                vol_w = window["Volume"].fillna(0)
                close_w = window["Close"]
                vol_at_lows = float(vol_w[close_w <= low_band].sum())
                vol_at_highs = float(vol_w[close_w >= high_band].sum())
                ratio = (vol_at_lows + 1) / (vol_at_highs + 1)
                if ratio >= 1.3:
                    phase = "accumulation"
                    observations.append("Wyckoff heuristic: accumulation phase, range with effort biased to the lows.")
                elif ratio <= 1 / 1.3:
                    phase = "distribution"
                    observations.append("Wyckoff heuristic: distribution phase, range with effort biased to the highs.")
                else:
                    phase = "accumulation_or_distribution"
                    observations.append("Wyckoff heuristic: flat 60MA implies range, accumulation/distribution requires more volume confirmation.")
            else:
                phase = "accumulation_or_distribution"
                observations.append("Wyckoff heuristic: flat 60MA implies range, accumulation/distribution requires volume confirmation.")
        elif price > ma60.iloc[-1] and slope > 0:
            phase = "markup"
            observations.append("Wyckoff heuristic: markup phase, price above rising 60MA.")
        elif price < ma60.iloc[-1] and slope < 0:
            phase = "markdown"
            observations.append("Wyckoff heuristic: markdown phase, price below falling 60MA.")
    wave3_meta = None
    if len(pivots) >= 5:
        observations.append("Five or more pivots available for wave labeling; deterministic Elliott counts should remain advisory.")
        # Wave 3 Extension heuristic: among the latest 5 pivots, find the
        # impulse leg (consecutive pivots) with the largest absolute price
        # excursion. If it is at least 1.6× the next-largest leg AND its
        # direction matches the current Wyckoff phase, flag as Wave 3 candidate.
        recent = pivots[-5:]
        leg_lengths = []
        for i in range(len(recent) - 1):
            leg_lengths.append({
                "start": recent[i],
                "end": recent[i + 1],
                "magnitude": abs(recent[i + 1]["price"] - recent[i]["price"]),
                "direction": "up" if recent[i + 1]["price"] > recent[i]["price"] else "down",
            })
        if leg_lengths:
            leg_lengths.sort(key=lambda x: x["magnitude"], reverse=True)
            biggest = leg_lengths[0]
            second = leg_lengths[1]["magnitude"] if len(leg_lengths) > 1 else 0
            if second > 0 and biggest["magnitude"] / second >= 1.6:
                wave3_dir = biggest["direction"]
                phase_dir = "up" if phase == "markup" else ("down" if phase == "markdown" else None)
                if phase_dir is None or wave3_dir == phase_dir:
                    wave3_meta = {
                        "direction": wave3_dir,
                        "magnitude": _round(biggest["magnitude"], 2),
                        "ratio_vs_next": _round(biggest["magnitude"] / second, 2),
                        "start_time": _time(biggest["start"]["time"]),
                        "end_time": _time(biggest["end"]["time"]),
                    }
                    observations.append(
                        f"Elliott Wave 3 extension candidate: {wave3_dir} impulse {round(biggest['magnitude'], 2)} "
                        f"is {round(biggest['magnitude'] / second, 2)}× the next-largest leg."
                    )
    if len(h) >= 80:
        range_window = h.tail(80)
        range_high = float(range_window["High"].quantile(0.92))
        range_low = float(range_window["Low"].quantile(0.08))
        last = h.iloc[-1]
        if last["Low"] < range_low and last["Close"] > range_low:
            observations.append("Wyckoff Spring heuristic: range low was swept and reclaimed.")
            markers.append(_marker(h.index[-1], "belowBar", "#22c55e", "arrowUp", "Spring", "macro_wave", 5, last["Low"]))
        if last["High"] > range_high and last["Close"] < range_high:
            observations.append("Wyckoff UTAD heuristic: range high was swept and rejected.")
            markers.append(_marker(h.index[-1], "aboveBar", "#ef4444", "arrowDown", "UTAD", "macro_wave", 5, last["High"]))
    if phase == "markup" and len(pivots) >= 5:
        wave_context = "impulse_candidate"
    elif phase in {"markdown", "distribution"}:
        wave_context = "corrective_or_distribution"
    elif phase == "accumulation":
        wave_context = "accumulation_base"
    elif phase == "accumulation_or_distribution":
        wave_context = "range_undefined"
    else:
        wave_context = "unconfirmed"
    return _dimension(
        "macro_wave",
        "computed" if observations else "partial",
        observations or ["Insufficient structure for macro cycle heuristic."],
        metrics={
            "wyckoff_phase": phase,
            "pivot_count": len(pivots),
            "wave_context": wave_context,
            "wave3_candidate": wave3_meta,
        },
        markers=markers,
    )


def _detect_whipsaw(intraday_5m: Optional[pd.DataFrame], daily: Optional[pd.DataFrame]) -> Optional[dict]:
    """Identify an event-day whipsaw: intraday range >> daily baseline + multi-flips."""
    if intraday_5m is None or daily is None or len(intraday_5m) < 10 or len(daily) < 20:
        return None
    last_date = intraday_5m.index[-1].date()
    today = intraday_5m[intraday_5m.index.date == last_date]
    if len(today) < 10:
        return None
    intraday_range = float(today["High"].max() - today["Low"].min())
    avg_daily_range = float((daily["High"] - daily["Low"]).tail(20).mean())
    if avg_daily_range <= 0:
        return None
    range_ratio = intraday_range / avg_daily_range
    # Count direction reversals via 5-bar smoothed sign of close differences
    diffs = today["Close"].diff().dropna()
    if len(diffs) < 6:
        return None
    smoothed_sign = diffs.rolling(3).mean().dropna().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    transitions = int((smoothed_sign.diff().abs() == 2).sum())
    if range_ratio >= 3 and transitions >= 3:
        return {"range_ratio": round(range_ratio, 2), "direction_flips": transitions, "date": last_date.isoformat()}
    return None


def _event_calendar(
    events: Optional[list[dict]],
    latest_ts: Optional[pd.Timestamp] = None,
    h: Optional[pd.DataFrame] = None,
    *,
    intraday_5m: Optional[pd.DataFrame] = None,
) -> dict:
    if not events:
        # Whipsaw can still fire on a day without an explicit catalyst payload.
        whipsaw = _detect_whipsaw(intraday_5m, h)
        if whipsaw:
            return _dimension(
                "event_calendar",
                "partial",
                [
                    f"Data-driven whipsaw flagged for {whipsaw['date']}: intraday range "
                    f"{whipsaw['range_ratio']}× daily baseline with {whipsaw['direction_flips']} direction flips.",
                    "No explicit event calendar payload; treat as untagged catalyst.",
                ],
                metrics={"whipsaw": whipsaw},
                data_gaps=["earnings dates", "CPI/FOMC/NFP timestamps", "company-specific catalysts"],
            )
        return _dimension(
            "event_calendar",
            "unavailable",
            ["Macro and earnings catalyst markers require an event calendar feed."],
            data_gaps=["earnings dates", "CPI/FOMC/NFP timestamps", "company-specific catalysts"],
        )
    markers = []
    observations = ["Event calendar markers supplied."]
    next_event = None
    for event in events:
        try:
            event_ts = pd.Timestamp(event["time"]).tz_localize(None)
            markers.append(_marker(event_ts, "inBar", "#facc15", "circle", event.get("label", "Event"), "event_calendar", 3))
            if latest_ts is not None and event_ts >= latest_ts and (next_event is None or event_ts < next_event):
                next_event = event_ts
        except Exception:
            continue
    days_to_next = None
    if latest_ts is not None and next_event is not None:
        days_to_next = (next_event.date() - latest_ts.date()).days
        if 0 <= days_to_next <= 5:
            observations.append(f"Next catalyst is within {days_to_next} trading/calendar days; pre-event compression risk should be monitored.")
    if h is not None and len(h) >= 10:
        recent_range = ((h["High"] - h["Low"]) / h["Close"].replace(0, np.nan)).tail(5).mean()
        prior_range = ((h["High"] - h["Low"]) / h["Close"].replace(0, np.nan)).tail(20).head(15).mean()
        if _as_float(recent_range) and _as_float(prior_range) and recent_range < prior_range * 0.75:
            observations.append("Recent candle ranges compressed into the event window.")
    whipsaw = _detect_whipsaw(intraday_5m, h)
    if whipsaw:
        observations.append(
            f"Data-driven whipsaw on {whipsaw['date']}: intraday range "
            f"{whipsaw['range_ratio']}× daily baseline with {whipsaw['direction_flips']} direction flips."
        )
    return _dimension(
        "event_calendar",
        "computed",
        observations,
        markers=markers,
        metrics={"event_count": len(events), "days_to_next_event": days_to_next, "whipsaw": whipsaw},
    )


def _confluence_zones(dimensions: list[dict], latest_price: float) -> list[dict]:
    raw_levels: list[dict] = []
    for dim in dimensions:
        for level in dim.get("levels", []):
            price = level.get("price")
            if price is None and level.get("low") is not None and level.get("high") is not None:
                price = (level["low"] + level["high"]) / 2
            price = _as_float(price)
            if price is None:
                continue
            raw_levels.append({"price": price, "dimension": dim["id"], "type": level.get("type", "level"), "label": level.get("label", "")})
    zones: list[dict] = []
    tolerance = max(latest_price * 0.01, 0.01)
    for level in sorted(raw_levels, key=lambda x: x["price"]):
        matched = None
        for zone in zones:
            if abs(zone["center"] - level["price"]) <= tolerance:
                matched = zone
                break
        if not matched:
            matched = {"center": level["price"], "levels": []}
            zones.append(matched)
        matched["levels"].append(level)
        matched["center"] = sum(x["price"] for x in matched["levels"]) / len(matched["levels"])
    result = []
    for zone in zones:
        if len({x["dimension"] for x in zone["levels"]}) >= 2:
            result.append({
                "center": _round(zone["center"], 2),
                "score": len(zone["levels"]),
                "sources": zone["levels"],
            })
    return sorted(result, key=lambda x: x["score"], reverse=True)[:8]


def _add_signal(signals: list[dict], label: str, direction: str, strength: float, evidence: str = "") -> float:
    sign = 1.0 if direction == "bullish" else (-1.0 if direction == "bearish" else 0.0)
    clipped_strength = _clip(strength, 0.0, 2.0)
    signals.append({
        "label": label,
        "direction": direction,
        "strength": _round(clipped_strength, 2),
        "evidence": evidence,
    })
    return sign * clipped_strength


_MARKER_TEXT_BIAS = {
    # bullish-meaning markers (final implication, regardless of source dimension)
    "bull engulf": +1,
    "bear trap": +1,
    "spring": +1,
    "hammer": +1,
    "sweep": +1,  # bull sweep colour is green; bear sweep handled separately below
    # bearish-meaning markers
    "bear engulf": -1,
    "bull trap": -1,
    "utad": -1,
    "pin bar": -1,
    "climax": -1,
}


def _marker_direction_score(markers: list[dict]) -> float:
    """Aggregate marker direction without double-counting position+text.

    Rules:
      - Markers with directional shapes (arrowUp/arrowDown) use the shape as the
        direction signal scaled by weight.
      - Markers with neutral shapes (circle/square) fall back to a small explicit
        text-bias map so we don't infer direction from substring matches that
        accidentally trigger on words like "bear trap".
    """
    score = 0.0
    for marker in markers:
        weight = _clip(marker.get("weight", 1), 1, 5) / 3.0
        shape = marker.get("shape")
        text = (marker.get("text") or "").lower().strip()
        if shape == "arrowUp":
            score += weight
        elif shape == "arrowDown":
            score -= weight
        else:
            bias = _MARKER_TEXT_BIAS.get(text)
            if bias is None:
                # Try whole-word matching on the bias keys (e.g., "sweep" inside
                # "Bear Sweep" would otherwise hijack the bullish bias).
                tokens = set(text.split())
                for key, sign in _MARKER_TEXT_BIAS.items():
                    if key in tokens:
                        bias = sign
                        break
            if bias is not None:
                score += sign_weight(bias, weight)
    return _clip(score, -3.5, 3.5)


def sign_weight(sign: int, weight: float) -> float:
    return weight * (1 if sign > 0 else -1)


def _enrich_dimension(dim: dict) -> dict:
    enriched = dict(dim)
    metrics = dict(enriched.get("metrics") or {})
    signals: list[dict] = list(enriched.get("signals") or [])
    score = _as_float(enriched.get("score")) or 0.0
    severity_value = 0.0
    dim_id = enriched.get("id")
    observations_text = " ".join(enriched.get("observations") or []).lower()

    if dim_id == "price_action":
        marker_score = _marker_direction_score(enriched.get("markers", []))
        if marker_score:
            score += _add_signal(signals, "price_action_marker", "bullish" if marker_score > 0 else "bearish", min(abs(marker_score), 2), "Latest candlestick marker stack.")
        if "inside bar" in observations_text:
            severity_value += 0.4
    elif dim_id == "trend_ma":
        price_vs_ma20 = _as_float(metrics.get("price_vs_ma20_pct"))
        if price_vs_ma20 is not None:
            direction = "bullish" if price_vs_ma20 > 0 else "bearish"
            score += _add_signal(signals, "price_vs_20ma", direction, min(abs(price_vs_ma20) / 4, 1.5), f"{_round(price_vs_ma20, 2)}% from 20MA.")
        ma20, ma60, ma200 = (_as_float(metrics.get("ma20")), _as_float(metrics.get("ma60")), _as_float(metrics.get("ma200")))
        price = _as_float(metrics.get("latest_close")) or None
        if price is None:
            for level in enriched.get("levels", []):
                if level.get("label") == "MA20":
                    price = ma20
                    break
        if ma20 and ma60 and ma200:
            if ma20 > ma60 > ma200:
                score += _add_signal(signals, "ma_stack", "bullish", 1.25, "20/60/200MA stack is rising.")
            elif ma20 < ma60 < ma200:
                score += _add_signal(signals, "ma_stack", "bearish", 1.25, "20/60/200MA stack is falling.")
        if _as_float(metrics.get("bollinger_width_percentile")) is not None and metrics["bollinger_width_percentile"] <= 15:
            severity_value += 0.6
    elif dim_id == "volume_profile":
        vol_ratio = _as_float(metrics.get("volume_ratio"))
        if vol_ratio is not None and vol_ratio >= 2:
            severity_value += 1.0
            _add_signal(signals, "institutional_effort", "neutral", min(vol_ratio / 2, 2), "Volume spike versus 20-day average.")
        if "absorption" in observations_text or "climax" in observations_text:
            severity_value += 1.0
        latest = _as_float(metrics.get("latest_close"))
        poc = _as_float(metrics.get("poc"))
        if latest and poc:
            score += _add_signal(signals, "poc_location", "bullish" if latest >= poc else "bearish", 0.5, "Spot relative to volume POC.")
    elif dim_id == "momentum":
        rsi = _as_float(metrics.get("rsi14"))
        hist = _as_float(metrics.get("macd_hist"))
        if rsi is not None:
            if rsi >= 70:
                score += _add_signal(signals, "rsi_embedding_or_overheat", "bullish", 0.8, f"RSI {round(rsi, 1)} above 70.")
                severity_value += 0.5
            elif rsi <= 30:
                score += _add_signal(signals, "rsi_oversold", "bearish", 0.5, f"RSI {round(rsi, 1)} below 30.")
                severity_value += 0.6
            elif rsi >= 50:
                score += _add_signal(signals, "rsi_midline", "bullish", 0.45, "RSI above 50.")
            else:
                score += _add_signal(signals, "rsi_midline", "bearish", 0.45, "RSI below 50.")
        if hist is not None:
            score += _add_signal(signals, "macd_histogram", "bullish" if hist > 0 else "bearish", min(abs(hist), 1.0), "MACD histogram sign.")
        score += _marker_direction_score(enriched.get("markers", []))
    elif dim_id == "structure_geometry":
        score += _marker_direction_score(enriched.get("markers", []))
        if "golden pocket" in observations_text:
            severity_value += 0.5
    elif dim_id == "volatility_risk":
        atr_pct = _as_float(metrics.get("atr_pct"))
        atr_percentile = _as_float(metrics.get("atr_percentile"))
        if atr_pct is not None and atr_pct >= 5:
            severity_value += 1.0
        if atr_percentile is not None and atr_percentile >= 85:
            severity_value += 1.0
        if "compression" in observations_text:
            severity_value += 0.5
    elif dim_id == "mtf_derivatives":
        alignment = metrics.get("timeframe_alignment")
        if alignment == "bullish":
            score += _add_signal(signals, "timeframe_alignment", "bullish", 1.4, "Daily/weekly/monthly are aligned upward.")
        elif alignment == "bearish":
            score += _add_signal(signals, "timeframe_alignment", "bearish", 1.4, "Daily/weekly/monthly are aligned downward.")
        elif alignment == "mixed":
            severity_value += 0.5
    elif dim_id == "microstructure_orderflow":
        imbalance = _as_float(metrics.get("computed_imbalance_ratio"))
        cvd_delta = _as_float(metrics.get("cvd_delta") or metrics.get("delta"))
        if imbalance is not None:
            if imbalance >= 3:
                score += _add_signal(signals, "orderflow_imbalance", "bullish", 1.2, "Aggressive buy imbalance above 300%.")
            elif imbalance <= 1 / 3:
                score += _add_signal(signals, "orderflow_imbalance", "bearish", 1.2, "Aggressive sell imbalance above 300%.")
        if cvd_delta is not None:
            score += _add_signal(signals, "cvd_delta", "bullish" if cvd_delta > 0 else "bearish", 0.8, "CVD delta sign.")
    elif dim_id == "intermarket_correlation":
        alpha = _as_float(metrics.get("alpha_20d_pct"))
        corr = _as_float(metrics.get("correlation_60d"))
        if alpha is not None:
            score += _add_signal(signals, "relative_strength", "bullish" if alpha > 0 else "bearish", min(abs(alpha) / 5, 1.4), f"20D alpha {round(alpha, 2)}%.")
        if corr is not None and corr < 0.2:
            severity_value += 0.4
    elif dim_id == "breadth_internals":
        above_50 = _as_float(metrics.get("percent_above_50ma") or metrics.get("above_50ma_pct"))
        if above_50 is not None:
            if above_50 >= 85:
                severity_value += 1.0
                score += _add_signal(signals, "breadth_overbought", "bearish", 0.6, "Breadth is stretched above 85%.")
            elif above_50 <= 15:
                severity_value += 1.0
                score += _add_signal(signals, "breadth_capitulation", "bullish", 0.6, "Breadth capitulation below 15%.")
    elif dim_id == "time_cyclical":
        if "above anchored vwap" in observations_text:
            score += _add_signal(signals, "avwap_location", "bullish", 0.7, "Spot is above anchored VWAP.")
        elif "below anchored vwap" in observations_text:
            score += _add_signal(signals, "avwap_location", "bearish", 0.7, "Spot is below anchored VWAP.")
    elif dim_id == "advanced_geometries":
        for level in enriched.get("levels", []):
            if level.get("type") == "harmonic_candidate":
                direction = "bullish" if level.get("direction") == "bullish_prz" else "bearish"
                score += _add_signal(signals, "harmonic_prz", direction, _as_float(level.get("confidence")) or 0.5, f"{level.get('pattern')} PRZ.")
        if any(level.get("type", "").endswith("fvg") for level in enriched.get("levels", [])):
            severity_value += 0.4
    elif dim_id == "options_gex":
        regime = str(metrics.get("gamma_regime") or "").lower()
        if "negative" in regime:
            severity_value += 1.2
        elif "positive" in regime:
            severity_value += 0.3
    elif dim_id == "order_book":
        imbalance = _as_float(metrics.get("book_imbalance"))
        if imbalance is not None:
            if imbalance >= 2:
                score += _add_signal(signals, "book_imbalance", "bullish", 1.0, "Visible bid wall dominates.")
            elif imbalance <= 0.5:
                score += _add_signal(signals, "book_imbalance", "bearish", 1.0, "Visible ask wall dominates.")
    elif dim_id == "statistical_reversion":
        zscore = _as_float(metrics.get("latest_z_score"))
        if zscore is not None:
            if zscore >= 2:
                score += _add_signal(signals, "tail_reversion", "bearish", min(abs(zscore) / 2, 1.6), "Price is above +2 sigma.")
                severity_value += 1.0
            elif zscore <= -2:
                score += _add_signal(signals, "tail_reversion", "bullish", min(abs(zscore) / 2, 1.6), "Price is below -2 sigma.")
                severity_value += 1.0
    elif dim_id == "macro_wave":
        phase = metrics.get("wyckoff_phase")
        if phase == "markup":
            score += _add_signal(signals, "wyckoff_phase", "bullish", 1.4, "Markup phase heuristic.")
        elif phase == "markdown":
            score += _add_signal(signals, "wyckoff_phase", "bearish", 1.4, "Markdown phase heuristic.")
        elif phase == "accumulation":
            score += _add_signal(signals, "wyckoff_phase", "bullish", 0.7, "Range with effort biased to the lows (accumulation).")
        elif phase == "distribution":
            score += _add_signal(signals, "wyckoff_phase", "bearish", 0.7, "Range with effort biased to the highs (distribution).")
        wave3 = metrics.get("wave3_candidate")
        if wave3 and wave3.get("direction") in {"up", "down"}:
            score += _add_signal(
                signals,
                "wave3_extension",
                "bullish" if wave3["direction"] == "up" else "bearish",
                min(1.6, (_as_float(wave3.get("ratio_vs_next")) or 1.6) / 2),
                "Largest impulse leg dominates the recent pivot stack.",
            )
        score += _marker_direction_score(enriched.get("markers", []))
    elif dim_id == "event_calendar":
        days_to_next = _as_float(metrics.get("days_to_next_event"))
        if days_to_next is not None and days_to_next <= 5:
            severity_value += 1.2

    score = _clip(score, -4.0, 4.0)
    enriched["score"] = _round(score, 2)
    enriched["bias"] = _bias_from_score(score)
    enriched["severity"] = _severity_from_value(severity_value)
    confidence = _confidence_for_status(
        enriched.get("status", ""),
        bool(enriched.get("metrics")),
        bool(enriched.get("data_gaps")),
    )
    if signals:
        confidence += min(len(signals) * 0.03, 0.12)
    enriched["confidence"] = _round(_clip(confidence, 0.05, 0.95), 2)
    enriched["signals"] = signals[:8]
    return enriched


def _level_price(level: dict) -> Optional[float]:
    price = _as_float(level.get("price"))
    if price is not None:
        return price
    low = _as_float(level.get("low"))
    high = _as_float(level.get("high"))
    if low is not None and high is not None:
        return (low + high) / 2
    prz = _as_float(level.get("prz"))
    return prz


def _collect_levels(dimensions: list[dict]) -> list[dict]:
    levels: list[dict] = []
    for dim in dimensions:
        for level in dim.get("levels", []):
            price = _level_price(level)
            if price is None:
                continue
            levels.append({
                "price": _round(price, 2),
                "dimension": dim["id"],
                "type": level.get("type", "level"),
                "label": level.get("label") or level.get("pattern") or level.get("type", "level"),
                "source": dim.get("name", dim["id"]),
            })
    return levels


def _scan_history_markers(h: pd.DataFrame, pivots: list[dict]) -> list[dict]:
    """Backfill high-value markers by walking every bar.

    Scope is intentionally conservative: only patterns where seeing repeated
    historical occurrences adds context to chart reading. Each bar is judged
    in isolation against pre-computed series — the same condition rules used
    on the latest bar elsewhere, only re-applied across history.
    """
    out: list[dict] = []
    if len(h) < 5:
        return out
    close = h["Close"]
    vol = h["Volume"].fillna(0) if "Volume" in h.columns else pd.Series(0, index=h.index)
    vol_ma20 = vol.rolling(20).mean()
    rsi = _rsi(close)
    dif, dea, hist = _macd(close)

    # Per-bar candlestick / volume / sweep / trap
    for i in range(2, len(h)):
        last = h.iloc[i]
        prev = h.iloc[i - 1]
        ts = h.index[i]
        rng = max(last["High"] - last["Low"], 1e-9)
        body = abs(last["Close"] - last["Open"])
        body_r = body / rng
        upper_r = (last["High"] - max(last["Open"], last["Close"])) / rng
        lower_r = (min(last["Open"], last["Close"]) - last["Low"]) / rng

        if (prev["Close"] < prev["Open"] and last["Close"] > last["Open"]
                and last["Close"] > prev["Open"] and last["Open"] < prev["Close"]):
            out.append(_marker(ts, "belowBar", "#22c55e", "arrowUp", "Bull Engulf", "price_action", 3, last["Low"]))
        if (prev["Close"] > prev["Open"] and last["Close"] < last["Open"]
                and last["Close"] < prev["Open"] and last["Open"] > prev["Close"]):
            out.append(_marker(ts, "aboveBar", "#ef4444", "arrowDown", "Bear Engulf", "price_action", 3, last["High"]))

        if lower_r >= 0.6 and body_r <= 0.3 and upper_r <= 0.2:
            out.append(_marker(ts, "belowBar", "#34d399", "arrowUp", "Hammer", "price_action", 3, last["Low"]))
        if upper_r >= 0.6 and body_r <= 0.3 and lower_r <= 0.2:
            out.append(_marker(ts, "aboveBar", "#f87171", "arrowDown", "Pin Bar", "price_action", 3, last["High"]))

        if i >= 22:
            prior_low = h["Low"].iloc[i - 22:i].min()
            prior_high = h["High"].iloc[i - 22:i].max()
            if last["Low"] < prior_low and last["Close"] > prior_low:
                out.append(_marker(ts, "belowBar", "#a3e635", "arrowUp", "Sweep", "price_action", 4, last["Low"]))
            if last["High"] > prior_high and last["Close"] < prior_high:
                out.append(_marker(ts, "aboveBar", "#fb7185", "arrowDown", "Sweep", "price_action", 4, last["High"]))

        if i >= 24:
            prev_range = h.iloc[i - 23:i - 3]
            range_high = prev_range["High"].max()
            range_low = prev_range["Low"].min()
            prior = h.iloc[i - 1]
            if prior["Close"] > range_high and last["Close"] < range_high:
                out.append(_marker(ts, "aboveBar", "#ef4444", "arrowDown", "Bull Trap", "price_action", 4, last["High"]))
            if prior["Close"] < range_low and last["Close"] > range_low:
                out.append(_marker(ts, "belowBar", "#22c55e", "arrowUp", "Bear Trap", "price_action", 4, last["Low"]))

        vma = _as_float(vol_ma20.iloc[i])
        if vma and vma > 0:
            vr = float(vol.iloc[i]) / vma
            if vr >= 2 and body_r <= 0.25:
                out.append(_marker(ts, "aboveBar", "#f43f5e", "square", "Absorb", "volume_profile", 4, last["High"]))
            elif vr >= 2:
                out.append(_marker(ts, "aboveBar", "#f59e0b", "circle", "Vol Spike", "volume_profile", 3, last["High"]))

    # Pivot-based BOS / ChoCh: walk each high/low pivot and check break of prior
    highs = [p for p in pivots if p["kind"] == "high"]
    lows = [p for p in pivots if p["kind"] == "low"]
    for j, pivot in enumerate(highs):
        if j == 0:
            continue
        prev_high = highs[j - 1]
        if pivot["price"] <= prev_high["price"]:
            continue
        # Determine prior trend from pivots earlier than prev_high
        prior_trend = "neutral"
        if len(lows) >= 2 and len(highs) >= 2:
            earlier_lows = [l for l in lows if l["index"] < pivot["index"]]
            if len(earlier_lows) >= 2 and earlier_lows[-1]["price"] < earlier_lows[-2]["price"]:
                prior_trend = "down"
        ts = pivot["time"]
        if prior_trend == "down":
            out.append(_marker(ts, "belowBar", "#22d3ee", "arrowUp", "ChoCh", "structure_geometry", 4, pivot["price"]))
        else:
            out.append(_marker(ts, "belowBar", "#22c55e", "arrowUp", "BOS", "structure_geometry", 3, pivot["price"]))
    for j, pivot in enumerate(lows):
        if j == 0:
            continue
        prev_low = lows[j - 1]
        if pivot["price"] >= prev_low["price"]:
            continue
        prior_trend = "neutral"
        if len(highs) >= 2:
            earlier_highs = [hi for hi in highs if hi["index"] < pivot["index"]]
            if len(earlier_highs) >= 2 and earlier_highs[-1]["price"] > earlier_highs[-2]["price"]:
                prior_trend = "up"
        ts = pivot["time"]
        if prior_trend == "up":
            out.append(_marker(ts, "aboveBar", "#fb7185", "arrowDown", "ChoCh", "structure_geometry", 4, pivot["price"]))
        else:
            out.append(_marker(ts, "aboveBar", "#ef4444", "arrowDown", "BOS", "structure_geometry", 3, pivot["price"]))

    # Statistical ±3σ across history
    closes_arr = close.to_numpy(dtype=float)
    if len(closes_arr) >= 30:
        window = closes_arr[-min(len(closes_arr), 250):]
        x_arr = np.arange(len(window), dtype=float)
        slope, intercept = np.polyfit(x_arr, window, 1)
        fitted = slope * x_arr + intercept
        residual = window - fitted
        sigma = residual.std(ddof=1) if len(residual) > 1 else 0
        if sigma:
            base_ts = close.tail(len(window)).index
            for k, r in enumerate(residual):
                z = r / sigma
                if z >= 3:
                    out.append(_marker(base_ts[k], "aboveBar", "#dc2626", "arrowDown", "+3σ", "statistical_reversion", 4, fitted[k] + r))
                elif z <= -3:
                    out.append(_marker(base_ts[k], "belowBar", "#2563eb", "arrowUp", "-3σ", "statistical_reversion", 4, fitted[k] + r))
                elif z >= 2.2 and (k == len(residual) - 1 or residual[k] > residual[k - 1]):
                    # Only the local peak above +2σ to avoid clutter
                    if k > 0 and k < len(residual) - 1 and residual[k] >= residual[k - 1] and residual[k] >= residual[k + 1]:
                        out.append(_marker(base_ts[k], "aboveBar", "#f97316", "arrowDown", "+2σ", "statistical_reversion", 3, fitted[k] + r))
                elif z <= -2.2:
                    if k > 0 and k < len(residual) - 1 and residual[k] <= residual[k - 1] and residual[k] <= residual[k + 1]:
                        out.append(_marker(base_ts[k], "belowBar", "#38bdf8", "arrowUp", "-2σ", "statistical_reversion", 3, fitted[k] + r))

    # Regular RSI divergence across all consecutive same-kind pivot pairs
    if len(highs) >= 2:
        for j in range(1, len(highs)):
            h1, h2 = highs[j - 1], highs[j]
            r1 = _as_float(rsi.iloc[h1["index"]]) if h1["index"] < len(rsi) else None
            r2 = _as_float(rsi.iloc[h2["index"]]) if h2["index"] < len(rsi) else None
            if r1 is None or r2 is None:
                continue
            if h2["price"] > h1["price"] and r2 < r1:
                out.append(_marker(h2["time"], "aboveBar", "#ef4444", "arrowDown", "RSI Div", "momentum", 4, h2["price"]))
    if len(lows) >= 2:
        for j in range(1, len(lows)):
            l1, l2 = lows[j - 1], lows[j]
            r1 = _as_float(rsi.iloc[l1["index"]]) if l1["index"] < len(rsi) else None
            r2 = _as_float(rsi.iloc[l2["index"]]) if l2["index"] < len(rsi) else None
            if r1 is None or r2 is None:
                continue
            if l2["price"] < l1["price"] and r2 > r1:
                out.append(_marker(l2["time"], "belowBar", "#22c55e", "arrowUp", "RSI Div", "momentum", 4, l2["price"]))

    return out


def _marker_summary(markers: list[dict]) -> dict:
    by_dimension: dict[str, int] = {}
    by_weight: dict[str, int] = {}
    for marker in markers:
        by_dimension[marker.get("dimension", "unknown")] = by_dimension.get(marker.get("dimension", "unknown"), 0) + 1
        weight_key = str(marker.get("weight", 1))
        by_weight[weight_key] = by_weight.get(weight_key, 0) + 1
    return {
        "total": len(markers),
        "by_dimension": dict(sorted(by_dimension.items())),
        "by_weight": dict(sorted(by_weight.items())),
        "high_conviction": [m for m in markers if _as_float(m.get("weight")) is not None and m["weight"] >= 4][-12:],
    }


def _matrix_summary(dimensions: list[dict], latest_price: float) -> dict:
    computed = sum(1 for dim in dimensions if dim.get("status") == "computed")
    partial = sum(1 for dim in dimensions if dim.get("status") == "partial")
    unavailable = sum(1 for dim in dimensions if dim.get("status") == "unavailable")
    weighted_score = 0.0
    weight_sum = 0.0
    risk_score = 0
    for dim in dimensions:
        confidence = _as_float(dim.get("confidence")) or 0.0
        if dim.get("status") != "unavailable":
            weighted_score += (_as_float(dim.get("score")) or 0.0) * confidence
            weight_sum += confidence
        risk_score += {"low": 0, "medium": 1, "high": 2}.get(dim.get("severity"), 0)
    net_score = weighted_score / weight_sum if weight_sum else 0.0
    confidence = (sum((_as_float(dim.get("confidence")) or 0.0) for dim in dimensions) / max(len(dimensions), 1))
    return {
        "bias": _bias_from_score(net_score),
        "net_score": _round(net_score, 2),
        "risk_score": risk_score,
        "risk_level": "high" if risk_score >= 8 else ("medium" if risk_score >= 4 else "low"),
        "confidence": _round(confidence, 2),
        "computed_count": computed,
        "partial_count": partial,
        "unavailable_count": unavailable,
        "dimension_count": len(dimensions),
        "latest_price": _round(latest_price, 2),
    }


def _interaction_layer(dimensions: list[dict], confluence_zones: list[dict]) -> list[dict]:
    by_id = {dim["id"]: dim for dim in dimensions}

    def bias(dim_id: str) -> str:
        return by_id.get(dim_id, {}).get("bias", "neutral")

    def score(dim_id: str) -> float:
        return _as_float(by_id.get(dim_id, {}).get("score")) or 0.0

    trend_momentum = "aligned" if score("trend_ma") * score("momentum") > 0 else ("conflict" if score("trend_ma") * score("momentum") < 0 else "neutral")
    macro_exec = "aligned" if score("macro_wave") * score("mtf_derivatives") > 0 else ("conflict" if score("macro_wave") * score("mtf_derivatives") < 0 else "neutral")
    data_gaps = sorted({gap for dim in dimensions for gap in dim.get("data_gaps", [])})

    return [
        {
            "name": "Confluence Zone",
            "status": "active" if confluence_zones else "inactive",
            "logic": "Structure, Fibonacci, FVG, VPVR, AVWAP, MA, GEX, order-book, and statistical levels cluster within 1% of price.",
            "evidence": confluence_zones[:3],
        },
        {
            "name": "Trend-Momentum Confirmation",
            "status": trend_momentum,
            "logic": "MA alignment is validated by RSI/MACD before chart markers are promoted to continuation signals.",
            "evidence": {"trend_bias": bias("trend_ma"), "momentum_bias": bias("momentum")},
        },
        {
            "name": "Effort-Result Reversal Risk",
            "status": "active" if by_id.get("volume_profile", {}).get("severity") in {"medium", "high"} else "inactive",
            "logic": "Volume spike plus small candle body can invalidate price-only breakout interpretations.",
            "evidence": by_id.get("volume_profile", {}).get("signals", []),
        },
        {
            "name": "Macro-to-Execution Alignment",
            "status": macro_exec,
            "logic": "Macro wave phase and multi-timeframe direction gate short-timeframe execution triggers.",
            "evidence": {"macro_bias": bias("macro_wave"), "mtf_bias": bias("mtf_derivatives")},
        },
        {
            "name": "Data Availability Guardrail",
            "status": "gaps_present" if data_gaps else "complete",
            "logic": "Unavailable order-flow, options, breadth, order-book, and event layers stay explicit instead of being silently approximated.",
            "evidence": data_gaps[:12],
        },
    ]


def _execution_plan(dimensions: list[dict], summary: dict, confluence_zones: list[dict], latest_price: float) -> dict:
    levels = _collect_levels(dimensions)
    below = sorted([level for level in levels if level["price"] < latest_price], key=lambda x: x["price"], reverse=True)
    above = sorted([level for level in levels if level["price"] > latest_price], key=lambda x: x["price"])
    by_id = {dim["id"]: dim for dim in dimensions}
    volatility = by_id.get("volatility_risk", {}).get("metrics", {})
    bias = summary.get("bias", "neutral")

    entries: list[dict] = []
    stops: list[dict] = []
    targets: list[dict] = []
    risk_notes: list[str] = []

    pullback_zone = next((zone for zone in confluence_zones if _as_float(zone.get("center")) and zone["center"] < latest_price), None)
    breakout_level = above[0] if above else None
    support_level = below[0] if below else None

    if bias in {"bullish", "strong_bullish"}:
        if pullback_zone:
            entries.append({"type": "pullback_to_confluence", "price": pullback_zone["center"], "logic": "Bullish matrix with nearest confluence support."})
        elif support_level:
            entries.append({"type": "pullback_to_support", "price": support_level["price"], "logic": f"{support_level['label']} from {support_level['dimension']}."})
        if breakout_level:
            entries.append({"type": "breakout_confirmation", "price": breakout_level["price"], "logic": f"Clear {breakout_level['label']} with volume confirmation."})
        for key in ("stop_2atr_long", "stop_3atr_long"):
            if volatility.get(key) is not None:
                stops.append({"type": key, "price": volatility[key], "logic": "ATR-adaptive long invalidation."})
        targets.extend({"type": "resistance_or_extension", "price": level["price"], "logic": f"{level['label']} from {level['dimension']}."} for level in above[:3])
    elif bias in {"bearish", "strong_bearish"}:
        if breakout_level:
            entries.append({"type": "relief_to_resistance", "price": breakout_level["price"], "logic": f"Bearish matrix into {breakout_level['label']}."})
        if support_level:
            entries.append({"type": "breakdown_confirmation", "price": support_level["price"], "logic": f"Lose {support_level['label']} with follow-through."})
        for key in ("stop_2atr_short", "stop_3atr_short"):
            if volatility.get(key) is not None:
                stops.append({"type": key, "price": volatility[key], "logic": "ATR-adaptive short invalidation."})
        targets.extend({"type": "support_or_reversion", "price": level["price"], "logic": f"{level['label']} from {level['dimension']}."} for level in below[:3])
    else:
        if support_level:
            entries.append({"type": "range_low_response", "price": support_level["price"], "logic": "Neutral matrix: wait for support reaction."})
        if breakout_level:
            entries.append({"type": "range_high_response", "price": breakout_level["price"], "logic": "Neutral matrix: wait for resistance reaction."})
        risk_notes.append("Matrix is neutral; wait for trend-momentum or confluence confirmation before sizing.")

    if summary.get("risk_level") == "high":
        risk_notes.append("Risk level is high; reduce size or require confirmation from volume/order-flow.")
    if summary.get("unavailable_count", 0) >= 4:
        risk_notes.append("Several institutional data layers are unavailable; avoid treating OHLC-only confirmation as full institutional confirmation.")

    return {
        "bias": bias,
        "latest_price": _round(latest_price, 2),
        "entries": entries[:4],
        "stops": stops[:4],
        "targets": targets[:4],
        "nearest_supports": below[:5],
        "nearest_resistances": above[:5],
        "risk_notes": risk_notes,
    }


def build_technical_matrix(
    symbol: str,
    history: pd.DataFrame,
    *,
    context_history: Optional[pd.DataFrame] = None,
    benchmark_close: Optional[pd.Series] = None,
    benchmarks: Optional[dict] = None,
    intraday_1h: Optional[pd.DataFrame] = None,
    intraday_15m: Optional[pd.DataFrame] = None,
    intraday_5m: Optional[pd.DataFrame] = None,
    events: Optional[list[dict]] = None,
    derivatives: Optional[dict] = None,
    order_flow: Optional[dict] = None,
    order_book: Optional[dict] = None,
    breadth: Optional[dict] = None,
    options_profile: Optional[dict] = None,
    anchors: Optional[list[dict]] = None,
    include_history_markers: bool = False,
    source: str = "",
) -> dict:
    """Build the full 17-dimensional matrix and chart marker payload."""
    execution_h = _clean_history(history)
    if len(execution_h) < 2:
        raise ValueError("not enough OHLC history")
    context_h = (
        _clean_history(context_history)
        if context_history is not None and len(context_history)
        else execution_h
    )
    use_context_for_macro = _is_intraday_history(execution_h) or len(execution_h) < 60
    macro_h = context_h if use_context_for_macro and len(context_h) >= 20 else execution_h
    trend_h = context_h if use_context_for_macro and len(context_h) >= 20 else execution_h
    volatility_h = context_h if use_context_for_macro and len(context_h) >= 20 else execution_h
    macro_basis = "context_daily" if macro_h is context_h and macro_h is not execution_h else "primary"
    trend_basis = "context_daily" if trend_h is context_h and trend_h is not execution_h else "primary"
    volatility_basis = "context_daily" if volatility_h is context_h and volatility_h is not execution_h else "primary"

    pivots = _find_pivots(execution_h)
    macro_pivots = _find_pivots(macro_h)
    intraday_1h = _clean_history(intraday_1h) if intraday_1h is not None and len(intraday_1h) else None
    intraday_15m = _clean_history(intraday_15m) if intraday_15m is not None and len(intraday_15m) else None
    intraday_5m = _clean_history(intraday_5m) if intraday_5m is not None and len(intraday_5m) else None
    dimensions = [
        _price_action(execution_h, pivots),
        _trend_ma(trend_h, price_override=execution_h["Close"].iloc[-1], basis=trend_basis),
        _volume_profile(execution_h),
        _momentum(execution_h, pivots),
        _structure_geometry(execution_h, pivots),
        _volatility_risk(volatility_h, price_override=execution_h["Close"].iloc[-1], basis=volatility_basis),
        _mtf_derivatives(macro_h, derivatives, intraday_1h=intraday_1h, intraday_15m=intraday_15m),
        _microstructure(order_flow),
        _intermarket(macro_h, benchmark_close, benchmarks=benchmarks),
        _breadth(breadth),
        _time_cyclical(execution_h, anchors, intraday_5m=intraday_5m),
        _advanced_geometries(execution_h, pivots),
        _options_gex(options_profile),
        _order_book(order_book),
        _statistical_reversion(execution_h),
        _macro_wave(macro_h, macro_pivots),
        _event_calendar(events, execution_h.index[-1], macro_h, intraday_5m=intraday_5m),
    ]
    dimensions = [_enrich_dimension(dim) for dim in dimensions]
    markers = [marker for dim in dimensions for marker in dim.get("markers", [])]
    history_marker_count = 0
    if include_history_markers:
        # Dedupe: existing markers' (time, dimension, text) keys take precedence
        # over re-scanned ones at the same coordinate to avoid double-stamping.
        seen_keys = {(m["time"], m.get("dimension"), m.get("text")) for m in markers}
        for hist_marker in _scan_history_markers(execution_h, pivots):
            key = (hist_marker["time"], hist_marker.get("dimension"), hist_marker.get("text"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            markers.append(hist_marker)
            history_marker_count += 1
    markers = sorted(markers, key=lambda m: (m["time"], m.get("weight", 1)))
    latest_price = float(execution_h["Close"].iloc[-1])
    confluence = _confluence_zones(dimensions, latest_price)
    summary = _matrix_summary(dimensions, latest_price)
    return {
        "symbol": symbol,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source or execution_h.attrs.get("source", ""),
        "history": {
            "first": execution_h.index[0].date().isoformat(),
            "last": execution_h.index[-1].date().isoformat(),
            "bars": len(execution_h),
            "latest_close": _round(latest_price, 2),
            "granularity": "intraday" if _is_intraday_history(execution_h) else "daily",
        },
        "analysis_context": {
            "execution_bars": len(execution_h),
            "context_bars": len(context_h),
            "execution_granularity": "intraday" if _is_intraday_history(execution_h) else "daily",
            "macro_basis": macro_basis,
            "trend_basis": trend_basis,
            "volatility_basis": volatility_basis,
        },
        "workflow": [dimension_id for dimension_id, _ in DIMENSION_DEFS],
        "dimensions": dimensions,
        "dimension_status": {dim["id"]: dim["status"] for dim in dimensions},
        "summary": summary,
        "interactions": _interaction_layer(dimensions, confluence),
        "confluence_zones": confluence,
        "execution_plan": _execution_plan(dimensions, summary, confluence, latest_price),
        "markers": markers,
        "marker_summary": {
            **_marker_summary(markers),
            "history_backfill": history_marker_count,
            "include_history_markers": include_history_markers,
        },
        "data_gaps": sorted({gap for dim in dimensions for gap in dim.get("data_gaps", [])}),
        # Preserve dimension attribution so the UI can surface which dimension a
        # missing feed belongs to instead of showing a flat deduped list.
        "data_gaps_by_dimension": {
            dim["id"]: list(dim.get("data_gaps", []))
            for dim in dimensions
            if dim.get("data_gaps")
        },
    }
