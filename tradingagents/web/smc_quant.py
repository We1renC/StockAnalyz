"""Deterministic Smart Money Concept (SMC) analysis engine.

The engine intentionally keeps every detector pure and pandas-based so the
same outputs can be reused by API views, chart markers, and future backtests.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SMCConfig:
    swing_length: int = 5
    internal_swing_length: int = 3
    close_break: bool = True
    liquidity_range_percent: float = 0.01
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.7
    min_rr: float = 1.5
    entry_threshold: int = 8


DEFAULT_CONFLUENCE_WEIGHTS = {
    "htf_bias_alignment": 2,
    "premium_discount_alignment": 2,
    "unmitigated_ob": 2,
    "unfilled_fvg": 1,
    "liquidity_sweep": 2,
    "ltf_choch": 2,
    "ote_zone": 1,
    "killzone": 1,
    "displacement": 1,
}


MARKET_CONFIGS = {
    "tw": {
        "timezone": "Asia/Taipei",
        "session": "09:00-13:30",
        "primary_killzone": "09:00-10:00",
        "tick_size": 0.01,
        "daily_price_limit_pct": 10,
        "commission_pct": 0.001425,
        "transaction_tax_pct": 0.003,
        "default_timeframes": {"htf": "1y", "mtf": "6mo", "ltf": "1mo"},
    },
    "us": {
        "timezone": "America/New_York",
        "session": "09:30-16:00",
        "primary_killzone": "09:30-10:00",
        "tick_size": 0.01,
        "daily_price_limit_pct": None,
        "commission_pct": 0.0,
        "transaction_tax_pct": 0.0,
        "default_timeframes": {"htf": "1y", "mtf": "6mo", "ltf": "1mo"},
    },
    "crypto": {
        "timezone": "UTC",
        "session": "24/7",
        "primary_killzone": "London/NY",
        "tick_size": 0.01,
        "daily_price_limit_pct": None,
        "commission_pct": 0.0006,
        "funding_sensitive": True,
        "default_timeframes": {"htf": "1d", "mtf": "4h", "ltf": "15m"},
        "max_leverage": 3,
    },
}


def infer_market(symbol: str) -> str:
    upper = (symbol or "").upper()
    if upper.endswith((".TW", ".TWO")):
        return "tw"
    if any(x in upper for x in ("BTC", "ETH", "USDT", "USD-")) or "/" in upper:
        return "crypto"
    return "us"


def market_config(symbol: str) -> dict:
    return deepcopy(MARKET_CONFIGS[infer_market(symbol)])


def confluence_weights(overrides: Optional[dict[str, int]] = None) -> dict[str, int]:
    weights = dict(DEFAULT_CONFLUENCE_WEIGHTS)
    for key, value in (overrides or {}).items():
        if key in weights:
            weights[key] = int(value)
    return weights


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _ts_value(index_value) -> int:
    return int(pd.Timestamp(index_value).timestamp())


def _record_time(index_value) -> str:
    return pd.Timestamp(index_value).isoformat()


def _round_tick(value: float, tick_size: float = 0.01) -> float:
    if tick_size <= 0:
        return round(float(value), 4)
    return round(round(float(value) / tick_size) * tick_size, 4)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    rename = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    out = out.rename(columns=rename)
    required = ["open", "high", "low", "close"]
    for col in required:
        if col not in out:
            return pd.DataFrame()
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "volume" not in out:
        out["volume"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out = out.dropna(subset=required)
    out.index = pd.to_datetime(out.index).tz_localize(None)
    return out.sort_index()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def detect_swings(df: pd.DataFrame, swing_length: int = 5, label: str = "swing") -> list[dict]:
    swings: list[dict] = []
    if len(df) < swing_length * 2 + 1:
        return swings
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    idx = list(df.index)
    for i in range(swing_length, len(df) - swing_length):
        high_window = highs[i - swing_length : i + swing_length + 1]
        low_window = lows[i - swing_length : i + swing_length + 1]
        if highs[i] == np.nanmax(high_window):
            swings.append(
                {
                    "index": i,
                    "time": _record_time(idx[i]),
                    "confirm_index": i + swing_length,
                    "confirm_time": _record_time(idx[i + swing_length]),
                    "type": "high",
                    "direction": -1,
                    "level": round(float(highs[i]), 4),
                    "scope": label,
                    "lookahead_safe": True,
                }
            )
        if lows[i] == np.nanmin(low_window):
            swings.append(
                {
                    "index": i,
                    "time": _record_time(idx[i]),
                    "confirm_index": i + swing_length,
                    "confirm_time": _record_time(idx[i + swing_length]),
                    "type": "low",
                    "direction": 1,
                    "level": round(float(lows[i]), 4),
                    "scope": label,
                    "lookahead_safe": True,
                }
            )
    return sorted(swings, key=lambda x: (x["index"], x["type"]))


def detect_displacement(df: pd.DataFrame, cfg: SMCConfig) -> list[dict]:
    if len(df) == 0:
        return []
    a = atr(df)
    out: list[dict] = []
    for i, (ts, row) in enumerate(df.iterrows()):
        rng = float(row["high"] - row["low"])
        body = abs(float(row["close"] - row["open"]))
        atr_v = float(a.iloc[i]) if i < len(a) else 0.0
        body_ratio = body / rng if rng > 0 else 0
        if (atr_v > 0 and body >= cfg.displacement_atr_mult * atr_v) or body_ratio >= cfg.displacement_body_ratio:
            direction = 1 if row["close"] >= row["open"] else -1
            out.append(
                {
                    "index": i,
                    "time": _record_time(ts),
                    "direction": direction,
                    "body": round(body, 4),
                    "range": round(rng, 4),
                    "atr": round(atr_v, 4),
                    "body_ratio": round(body_ratio, 3),
                }
            )
    return out


def detect_structure(df: pd.DataFrame, swings: list[dict], cfg: SMCConfig) -> list[dict]:
    if not swings:
        return []
    confirmed = [s for s in swings if s["confirm_index"] < len(df)]
    confirmed.sort(key=lambda s: s["confirm_index"])
    last_high = None
    last_low = None
    trend = 0
    events: list[dict] = []
    broken: set[tuple[str, int]] = set()
    ptr = 0
    for i, (ts, row) in enumerate(df.iterrows()):
        while ptr < len(confirmed) and confirmed[ptr]["confirm_index"] <= i:
            s = confirmed[ptr]
            if s["type"] == "high":
                last_high = s
            else:
                last_low = s
            ptr += 1
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        break_high = last_high and ((close > last_high["level"]) if cfg.close_break else (high > last_high["level"]))
        break_low = last_low and ((close < last_low["level"]) if cfg.close_break else (low < last_low["level"]))
        if break_high and ("high", last_high["index"]) not in broken:
            event_type = "CHOCH" if trend < 0 else "BOS"
            trend = 1
            broken.add(("high", last_high["index"]))
            events.append(
                {
                    "index": i,
                    "time": _record_time(ts),
                    "type": event_type,
                    "direction": 1,
                    "level": last_high["level"],
                    "swing_index": last_high["index"],
                    "broken_index": i,
                }
            )
        if break_low and ("low", last_low["index"]) not in broken:
            event_type = "CHOCH" if trend > 0 else "BOS"
            trend = -1
            broken.add(("low", last_low["index"]))
            events.append(
                {
                    "index": i,
                    "time": _record_time(ts),
                    "type": event_type,
                    "direction": -1,
                    "level": last_low["level"],
                    "swing_index": last_low["index"],
                    "broken_index": i,
                }
            )
    return events


def detect_fvgs(df: pd.DataFrame, displacements: list[dict]) -> list[dict]:
    disp_indexes = {d["index"] for d in displacements}
    out: list[dict] = []
    if len(df) < 3:
        return out
    idx = list(df.index)
    for i in range(1, len(df) - 1):
        prev = df.iloc[i - 1]
        mid = df.iloc[i]
        nxt = df.iloc[i + 1]
        direction = 0
        top = bottom = None
        if float(nxt["low"]) > float(prev["high"]):
            direction = 1
            bottom = float(prev["high"])
            top = float(nxt["low"])
        elif float(nxt["high"]) < float(prev["low"]):
            direction = -1
            bottom = float(nxt["high"])
            top = float(prev["low"])
        if not direction:
            continue
        mitigated = None
        inverse = False
        for j in range(i + 2, len(df)):
            row = df.iloc[j]
            if direction == 1 and float(row["low"]) <= bottom:
                mitigated = j
                inverse = float(row["close"]) < bottom
                break
            if direction == -1 and float(row["high"]) >= top:
                mitigated = j
                inverse = float(row["close"]) > top
                break
        out.append(
            {
                "index": i,
                "time": _record_time(idx[i]),
                "direction": direction,
                "top": round(top, 4),
                "bottom": round(bottom, 4),
                "mid": round((top + bottom) / 2, 4),
                "mitigated_index": mitigated,
                "mitigated": mitigated is not None,
                "inverse": inverse,
                "displacement_confirmed": i in disp_indexes,
                "middle_body": round(abs(float(mid["close"] - mid["open"])), 4),
            }
        )
    return out


def detect_liquidity(df: pd.DataFrame, swings: list[dict], cfg: SMCConfig) -> list[dict]:
    out: list[dict] = []
    confirmed = [s for s in swings if s["confirm_index"] < len(df)]
    for kind, direction in (("high", -1), ("low", 1)):
        same = [s for s in confirmed if s["type"] == kind]
        for i in range(len(same) - 1):
            a = same[i]
            cluster = [a]
            for b in same[i + 1 :]:
                ref = max(abs(a["level"]), 1e-9)
                if abs(b["level"] - a["level"]) / ref <= cfg.liquidity_range_percent:
                    cluster.append(b)
            if len(cluster) < 2:
                continue
            level = float(np.mean([x["level"] for x in cluster]))
            end_index = max(x["index"] for x in cluster)
            swept = None
            for j in range(end_index + 1, len(df)):
                row = df.iloc[j]
                if kind == "high" and float(row["high"]) > level and float(row["close"]) < level:
                    swept = j
                    break
                if kind == "low" and float(row["low"]) < level and float(row["close"]) > level:
                    swept = j
                    break
            out.append(
                {
                    "type": "BSL" if kind == "high" else "SSL",
                    "direction": direction,
                    "level": round(level, 4),
                    "start_index": min(x["index"] for x in cluster),
                    "end_index": end_index,
                    "touches": len(cluster),
                    "swept_index": swept,
                    "swept": swept is not None,
                    "time": _record_time(df.index[end_index]),
                }
            )
    return out


def detect_order_blocks(
    df: pd.DataFrame,
    structure: list[dict],
    displacements: list[dict],
    liquidity: list[dict],
) -> list[dict]:
    disp_indexes = {d["index"] for d in displacements}
    recent_sweeps = [l for l in liquidity if l.get("swept_index") is not None]
    out: list[dict] = []
    for event in structure:
        direction = int(event["direction"])
        break_idx = int(event["broken_index"])
        start = max(0, break_idx - 12)
        candidate = None
        for j in range(break_idx - 1, start - 1, -1):
            row = df.iloc[j]
            bearish_candle = float(row["close"]) < float(row["open"])
            bullish_candle = float(row["close"]) > float(row["open"])
            if direction == 1 and bearish_candle:
                candidate = j
                break
            if direction == -1 and bullish_candle:
                candidate = j
                break
        if candidate is None:
            continue
        c = df.iloc[candidate]
        top = float(c["high"])
        bottom = float(c["low"])
        mitigated = None
        breaker = False
        for k in range(break_idx + 1, len(df)):
            row = df.iloc[k]
            if direction == 1:
                if float(row["low"]) <= top and float(row["high"]) >= bottom:
                    mitigated = k
                if float(row["close"]) < bottom:
                    breaker = True
                    break
            else:
                if float(row["high"]) >= bottom and float(row["low"]) <= top:
                    mitigated = k
                if float(row["close"]) > top:
                    breaker = True
                    break
        swept_before = any(
            s.get("swept_index") is not None and candidate - 5 <= int(s["swept_index"]) <= break_idx
            for s in recent_sweeps
        )
        displacement = break_idx in disp_indexes or any(abs(d["index"] - break_idx) <= 1 for d in displacements)
        unmitigated = mitigated is None
        grade = "A" if swept_before and displacement and unmitigated else ("B" if displacement and unmitigated else "C")
        out.append(
            {
                "index": candidate,
                "time": _record_time(df.index[candidate]),
                "direction": direction,
                "top": round(top, 4),
                "bottom": round(bottom, 4),
                "mid": round((top + bottom) / 2, 4),
                "event_index": break_idx,
                "event_type": event["type"],
                "mitigated_index": mitigated,
                "mitigated": mitigated is not None,
                "unmitigated": unmitigated,
                "breaker": breaker,
                "swept_before": swept_before,
                "displacement_confirmed": displacement,
                "grade": grade,
            }
        )
    return out


def premium_discount(df: pd.DataFrame, swings: list[dict]) -> dict:
    if not swings or len(df) == 0:
        return {}
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if not highs or not lows:
        return {}
    hi = max(highs[-5:], key=lambda s: s["level"])
    lo = min(lows[-5:], key=lambda s: s["level"])
    high = float(hi["level"])
    low = float(lo["level"])
    if high <= low:
        return {}
    eq = (high + low) / 2
    close = float(df["close"].iloc[-1])
    zone = "discount" if close < eq else ("premium" if close > eq else "equilibrium")
    return {
        "range_high": round(high, 4),
        "range_low": round(low, 4),
        "equilibrium": round(eq, 4),
        "zone": zone,
        "fib_0_62": round(low + (high - low) * 0.62, 4),
        "fib_0_705": round(low + (high - low) * 0.705, 4),
        "fib_0_79": round(low + (high - low) * 0.79, 4),
    }


def ote_zone(swings: list[dict], bias: str) -> dict:
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if not highs or not lows:
        return {}
    if bias in ("bullish", "strong_bullish"):
        lo = lows[-1]
        hi_candidates = [h for h in highs if h["index"] > lo["index"]]
        hi = hi_candidates[-1] if hi_candidates else highs[-1]
        start, end = lo["level"], hi["level"]
        if end <= start:
            return {}
        return {
            "direction": 1,
            "top": round(end - (end - start) * 0.62, 4),
            "bottom": round(end - (end - start) * 0.79, 4),
            "entry_0705": round(end - (end - start) * 0.705, 4),
            "stop_ref": round(start, 4),
            "tp1": round(end + (end - start) * 0.27, 4),
            "tp2": round(end + (end - start) * 0.62, 4),
        }
    hi = highs[-1]
    lo_candidates = [l for l in lows if l["index"] > hi["index"]]
    lo = lo_candidates[-1] if lo_candidates else lows[-1]
    start, end = hi["level"], lo["level"]
    if start <= end:
        return {}
    return {
        "direction": -1,
        "top": round(end + (start - end) * 0.79, 4),
        "bottom": round(end + (start - end) * 0.62, 4),
        "entry_0705": round(end + (start - end) * 0.705, 4),
        "stop_ref": round(start, 4),
        "tp1": round(end - (start - end) * 0.27, 4),
        "tp2": round(end - (start - end) * 0.62, 4),
    }


def previous_levels(df: pd.DataFrame) -> dict:
    if len(df) < 2:
        return {}
    prev = df.iloc[-2]
    close = float(df["close"].iloc[-1])
    return {
        "previous_high": round(float(prev["high"]), 4),
        "previous_low": round(float(prev["low"]), 4),
        "broken_high": close > float(prev["high"]),
        "broken_low": close < float(prev["low"]),
    }


def session_state(df: pd.DataFrame, symbol: str) -> dict:
    if len(df) == 0:
        return {}
    ts = pd.Timestamp(df.index[-1])
    minute = ts.hour * 60 + ts.minute
    market = "tw" if symbol.endswith((".TW", ".TWO")) else ("crypto" if any(x in symbol.upper() for x in ("BTC", "ETH", "USDT", "USD-")) else "us")
    if market == "tw":
        active = 9 * 60 <= minute <= 13 * 60 + 30
        killzone = 9 * 60 <= minute <= 10 * 60
        name = "Taiwan Open" if killzone else ("Taiwan Session" if active else "Closed")
    elif market == "us":
        active = 9 * 60 + 30 <= minute <= 16 * 60
        killzone = 9 * 60 + 30 <= minute <= 10 * 60
        name = "US Open" if killzone else ("US Session" if active else "Closed")
    else:
        london = 7 * 60 <= minute <= 10 * 60
        ny = 13 * 60 <= minute <= 16 * 60
        active = True
        killzone = london or ny
        name = "London Killzone" if london else ("NY Killzone" if ny else "Crypto 24/7")
    return {"market": market, "active": active, "killzone": killzone, "name": name}


def retracement_state(df: pd.DataFrame, swings: list[dict]) -> dict:
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if not highs or not lows or len(df) == 0:
        return {}
    close = float(df["close"].iloc[-1])
    last_high = highs[-1]
    last_low = lows[-1]
    if last_high["index"] > last_low["index"]:
        leg = last_high["level"] - last_low["level"]
        retr = (last_high["level"] - close) / leg * 100 if leg else None
        direction = 1
    else:
        leg = last_high["level"] - last_low["level"]
        retr = (close - last_low["level"]) / leg * 100 if leg else None
        direction = -1
    return {
        "direction": direction,
        "current_retracement_pct": round(retr, 2) if retr is not None else None,
    }


def _latest_bias(structure: list[dict]) -> str:
    if not structure:
        return "neutral"
    recent = structure[-3:]
    score = sum(e["direction"] * (2 if e["type"] == "CHOCH" else 1) for e in recent)
    if score >= 3:
        return "strong_bullish"
    if score > 0:
        return "bullish"
    if score <= -3:
        return "strong_bearish"
    if score < 0:
        return "bearish"
    return "neutral"


def _price_in_zone(price: float, zone: dict) -> bool:
    top = _safe_float(zone.get("top"))
    bottom = _safe_float(zone.get("bottom"))
    if top is None or bottom is None:
        return False
    return min(top, bottom) <= price <= max(top, bottom)


def _nearest_liquidity(price: float, liquidity: list[dict], direction: int, prev: dict) -> Optional[dict]:
    candidates = []
    for item in liquidity:
        level = _safe_float(item.get("level"))
        if level is None:
            continue
        if direction == 1 and level > price:
            candidates.append({"type": item["type"], "level": level, "source": "liquidity"})
        if direction == -1 and level < price:
            candidates.append({"type": item["type"], "level": level, "source": "liquidity"})
    if direction == 1 and prev.get("previous_high") and prev["previous_high"] > price:
        candidates.append({"type": "PDH", "level": prev["previous_high"], "source": "previous_high"})
    if direction == -1 and prev.get("previous_low") and prev["previous_low"] < price:
        candidates.append({"type": "PDL", "level": prev["previous_low"], "source": "previous_low"})
    if not candidates:
        return None
    return min(candidates, key=lambda x: abs(x["level"] - price))


def build_signals(
    df: pd.DataFrame,
    bias: str,
    order_blocks: list[dict],
    fvgs: list[dict],
    liquidity: list[dict],
    pd: dict,
    ote: dict,
    structure: list[dict],
    displacements: list[dict],
    session: dict,
    prev: dict,
    cfg: SMCConfig,
    weights: Optional[dict[str, int]] = None,
) -> list[dict]:
    if len(df) == 0:
        return []
    price = float(df["close"].iloc[-1])
    direction = 1 if bias in ("bullish", "strong_bullish") else (-1 if bias in ("bearish", "strong_bearish") else 0)
    if direction == 0:
        recent_sweep = next((l for l in reversed(liquidity) if l.get("swept")), None)
        direction = 1 if recent_sweep and recent_sweep["type"] == "SSL" else (-1 if recent_sweep and recent_sweep["type"] == "BSL" else 0)
    if direction == 0:
        return []

    recent_choch = any(e["type"] == "CHOCH" and e["direction"] == direction and e["index"] >= len(df) - 20 for e in structure)
    recent_sweep = any(
        l.get("swept_index") is not None
        and int(l["swept_index"]) >= len(df) - 20
        and ((direction == 1 and l["type"] == "SSL") or (direction == -1 and l["type"] == "BSL"))
        for l in liquidity
    )
    active_ob = [o for o in order_blocks if o["direction"] == direction and o["unmitigated"]]
    active_fvg = [f for f in fvgs if f["direction"] == direction and not f["mitigated"] and f["displacement_confirmed"]]
    in_pd = (direction == 1 and pd.get("zone") == "discount") or (direction == -1 and pd.get("zone") == "premium")
    in_ote = bool(ote) and ote.get("direction") == direction and _price_in_zone(price, ote)
    displacement_recent = any(d["direction"] == direction and d["index"] >= len(df) - 10 for d in displacements)

    factors = []
    score = 0
    w = confluence_weights(weights)
    checks = [
        ("htf_bias_alignment", bias, w["htf_bias_alignment"], direction != 0),
        ("premium_discount_alignment", pd.get("zone"), w["premium_discount_alignment"], in_pd),
        ("unmitigated_ob", len(active_ob), w["unmitigated_ob"], bool(active_ob)),
        ("unfilled_fvg", len(active_fvg), w["unfilled_fvg"], bool(active_fvg)),
        ("liquidity_sweep", recent_sweep, w["liquidity_sweep"], recent_sweep),
        ("ltf_choch", recent_choch, w["ltf_choch"], recent_choch),
        ("ote_zone", ote.get("entry_0705") if ote else None, w["ote_zone"], in_ote),
        ("killzone", session.get("name"), w["killzone"], bool(session.get("killzone"))),
        ("displacement", displacement_recent, w["displacement"], displacement_recent),
    ]
    for key, value, weight, active in checks:
        if active:
            score += weight
        factors.append({"id": key, "value": value, "weight": weight, "active": bool(active)})

    entry_model = "OB/FVG Continuation"
    if recent_sweep and recent_choch:
        entry_model = "Sweep + CHoCH"
    elif in_ote:
        entry_model = "OTE Retracement"
    elif active_ob and active_fvg and any(o.get("breaker") for o in active_ob):
        entry_model = "Unicorn"

    entry_candidates = []
    if active_ob:
        entry_candidates.append({"source": "OB 50%", "price": active_ob[-1]["mid"]})
    if active_fvg:
        entry_candidates.append({"source": "FVG mid", "price": active_fvg[-1]["mid"]})
    if ote:
        entry_candidates.append({"source": "OTE 0.705", "price": ote.get("entry_0705")})
    entry = next((x for x in entry_candidates if x.get("price") is not None), {"source": "market", "price": round(price, 4)})
    entry_price = float(entry["price"])
    if active_ob:
        stop = active_ob[-1]["bottom"] if direction == 1 else active_ob[-1]["top"]
    elif ote:
        stop = ote["stop_ref"]
    else:
        stop = price * (0.97 if direction == 1 else 1.03)
    dol = _nearest_liquidity(entry_price, liquidity, direction, prev)
    target = dol["level"] if dol else (ote.get("tp1") if ote else entry_price + direction * abs(entry_price - stop) * 2)
    risk = abs(entry_price - stop)
    reward = abs(target - entry_price)
    rr = reward / risk if risk > 0 else None
    return [
        {
            "model": entry_model,
            "direction": "long" if direction == 1 else "short",
            "score": score,
            "threshold": cfg.entry_threshold,
            "qualified": score >= cfg.entry_threshold and rr is not None and rr >= cfg.min_rr,
            "entry": round(entry_price, 4),
            "entry_source": entry["source"],
            "stop": round(float(stop), 4),
            "tp1": round(float(target), 4),
            "tp2": round(float(ote["tp2"]), 4) if ote and ote.get("tp2") is not None else None,
            "rr": round(rr, 2) if rr is not None else None,
            "dol_target": dol,
            "factors": factors,
            "risk": {
                "min_rr": cfg.min_rr,
                "structural_invalidation": round(float(stop), 4),
                "position_size_formula": "qty = account_risk / abs(entry - stop)",
            },
        }
    ]


def standardize_signal(
    signal: dict,
    symbol: str,
    timeframe: str,
    market: str,
    generated_at: str,
    account_equity: Optional[float] = None,
    risk_pct: float = 0.01,
) -> dict:
    out = dict(signal)
    out["signal_id"] = f"{symbol}:{timeframe}:{generated_at}:{signal.get('model')}:{signal.get('direction')}"
    out["symbol"] = symbol
    out["timeframe"] = timeframe
    out["market"] = market
    out["generated_at"] = generated_at
    out["status"] = "qualified" if signal.get("qualified") else "watch"
    out["feature_vector"] = {f["id"]: bool(f.get("active")) for f in signal.get("factors", [])}
    out["risk"] = dict(signal.get("risk") or {})
    out["risk"]["position_sizing"] = calculate_position_size(
        out,
        account_equity=account_equity or 0,
        risk_pct=risk_pct,
        market=market,
    )
    return out


def calculate_position_size(
    signal: dict,
    account_equity: float,
    risk_pct: float = 0.01,
    market: Optional[str] = None,
    max_single_loss_pct: float = 0.05,
    max_units: Optional[int] = None,
) -> dict:
    entry = _safe_float(signal.get("entry"))
    stop = _safe_float(signal.get("stop"))
    if entry is None or stop is None or account_equity <= 0:
        return {
            "qty": 0,
            "risk_amount": 0,
            "stop_distance": None,
            "blocked": True,
            "reason": "missing_entry_stop_or_equity",
        }
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return {
            "qty": 0,
            "risk_amount": 0,
            "stop_distance": 0,
            "blocked": True,
            "reason": "zero_stop_distance",
        }
    capped_risk_pct = min(max(risk_pct, 0), max_single_loss_pct)
    risk_amount = account_equity * capped_risk_pct
    raw_qty = math.floor(risk_amount / stop_distance)
    if max_units is not None:
        raw_qty = min(raw_qty, int(max_units))
    cfg = MARKET_CONFIGS.get(market or "", {})
    tick = cfg.get("tick_size", 0.01)
    rounded_entry = _round_tick(entry, tick)
    rounded_stop = _round_tick(stop, tick)
    return {
        "qty": max(raw_qty, 0),
        "risk_amount": round(risk_amount, 2),
        "risk_pct": capped_risk_pct,
        "stop_distance": round(stop_distance, 4),
        "entry_rounded": rounded_entry,
        "stop_rounded": rounded_stop,
        "blocked": raw_qty <= 0,
        "reason": "ok" if raw_qty > 0 else "risk_budget_too_small",
    }


def rule_enforcement_snapshot(
    account_equity: float,
    daily_realized_pnl: float = 0,
    max_drawdown: float = 0,
    active_days_traded: int = 0,
    daily_loss_limit: float = 50_000,
    max_drawdown_limit: float = 50_000,
) -> dict:
    daily_buffer = daily_loss_limit + daily_realized_pnl
    drawdown_buffer = max_drawdown_limit - abs(max_drawdown)
    locked = account_equity <= 0 or daily_buffer <= 0 or drawdown_buffer <= 0
    return {
        "account_equity": round(float(account_equity), 2),
        "daily_loss_limit_buffer": round(float(daily_buffer), 2),
        "max_drawdown_limit_buffer": round(float(drawdown_buffer), 2),
        "active_days_traded": int(active_days_traded),
        "locked": locked,
        "lock_reason": "risk_limit_breached" if locked else "ok",
    }


def build_markers(df: pd.DataFrame, swings, structure, fvgs, obs, liquidity) -> list[dict]:
    markers: list[dict] = []
    for s in swings[-12:]:
        markers.append(
            {
                "time": _ts_value(df.index[s["index"]]),
                "position": "aboveBar" if s["type"] == "high" else "belowBar",
                "shape": "circle",
                "color": "#94a3b8",
                "text": "SMC SH" if s["type"] == "high" else "SMC SL",
                "dimension": "smc",
            }
        )
    for e in structure[-8:]:
        markers.append(
            {
                "time": _ts_value(df.index[e["index"]]),
                "position": "aboveBar" if e["direction"] < 0 else "belowBar",
                "shape": "arrowDown" if e["direction"] < 0 else "arrowUp",
                "color": "#38bdf8" if e["type"] == "BOS" else "#f59e0b",
                "text": f"SMC {e['type']}",
                "dimension": "smc",
            }
        )
    for f in fvgs[-8:]:
        markers.append(
            {
                "time": _ts_value(df.index[f["index"]]),
                "position": "belowBar" if f["direction"] > 0 else "aboveBar",
                "shape": "square",
                "color": "#a855f7",
                "text": "SMC FVG" if not f["inverse"] else "SMC IFVG",
                "dimension": "smc",
            }
        )
    for o in obs[-6:]:
        markers.append(
            {
                "time": _ts_value(df.index[o["index"]]),
                "position": "belowBar" if o["direction"] > 0 else "aboveBar",
                "shape": "square",
                "color": "#22c55e" if o["direction"] > 0 else "#ef4444",
                "text": f"SMC {o['grade']} OB",
                "dimension": "smc",
            }
        )
    for liq in liquidity[-8:]:
        if liq.get("swept_index") is not None:
            markers.append(
                {
                    "time": _ts_value(df.index[int(liq["swept_index"])]),
                    "position": "aboveBar" if liq["type"] == "BSL" else "belowBar",
                    "shape": "circle",
                    "color": "#facc15",
                    "text": f"SMC {liq['type']} Sweep",
                    "dimension": "smc",
                }
            )
    return sorted(markers, key=lambda m: m["time"])


def build_smc_analysis(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str = "1d",
    config: Optional[SMCConfig] = None,
    correlated: Optional[dict[str, pd.DataFrame]] = None,
    weights: Optional[dict[str, int]] = None,
    account_equity: Optional[float] = None,
) -> dict:
    cfg = config or SMCConfig()
    market = infer_market(symbol)
    h = normalize_ohlcv(df)
    if len(h) < max(20, cfg.swing_length * 2 + 5):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "error": "insufficient_history",
            "required_bars": max(20, cfg.swing_length * 2 + 5),
            "bars": len(h),
        }

    swings = detect_swings(h, cfg.swing_length, "swing")
    internal_swings = detect_swings(h, cfg.internal_swing_length, "internal")
    displacements = detect_displacement(h, cfg)
    structure = detect_structure(h, swings, cfg)
    internal_structure = detect_structure(h, internal_swings, cfg)
    fvgs = detect_fvgs(h, displacements)
    liquidity = detect_liquidity(h, swings, cfg)
    obs = detect_order_blocks(h, structure, displacements, liquidity)
    pd_zone = premium_discount(h, swings)
    bias = _latest_bias(structure)
    ote = ote_zone(swings, bias)
    prev = previous_levels(h)
    session = session_state(h, symbol)
    retracement = retracement_state(h, swings)
    generated_at = datetime.now().isoformat(timespec="seconds")
    signals = build_signals(h, bias, obs, fvgs, liquidity, pd_zone, ote, structure, displacements, session, prev, cfg, weights)
    signals = [
        standardize_signal(
            s,
            symbol=symbol,
            timeframe=timeframe,
            market=market,
            generated_at=generated_at,
            account_equity=account_equity,
        )
        for s in signals
    ]
    markers = build_markers(h, swings, structure, fvgs, obs, liquidity)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "generated_at": generated_at,
        "market": market,
        "bars": len(h),
        "summary": {
            "bias": bias,
            "latest_close": round(float(h["close"].iloc[-1]), 4),
            "confluence_score": signals[0]["score"] if signals else 0,
            "qualified_signals": sum(1 for s in signals if s.get("qualified")),
            "risk_reward_min": cfg.min_rr,
            "entry_threshold": cfg.entry_threshold,
            "session": session.get("name"),
            "premium_discount": pd_zone.get("zone"),
            "lookahead_policy": "swing pivots expose confirm_index; signals use confirmed structures only",
        },
        "market_config": market_config(symbol),
        "concepts": {
            "swings": swings[-30:],
            "internal_swings": internal_swings[-30:],
            "structure": structure[-20:],
            "internal_structure": internal_structure[-20:],
            "order_blocks": obs[-20:],
            "fvgs": fvgs[-30:],
            "liquidity": liquidity[-30:],
            "premium_discount": pd_zone,
            "ote": ote,
            "previous_levels": prev,
            "sessions": session,
            "retracements": retracement,
            "displacement": displacements[-20:],
            "judas": {
                "active": bool(session.get("killzone"))
                and any(l.get("swept_index") is not None and int(l["swept_index"]) >= len(h) - 12 for l in liquidity),
                "note": "session fakeout requires sweep plus later CHoCH confirmation",
            },
            "smt": {
                "status": "not_configured" if not correlated else "provided",
                "pairs": list((correlated or {}).keys()),
            },
            "crypto_derivatives": {
                "status": "extension_point",
                "fields": ["liquidation_clusters", "open_interest", "funding_rate", "cvd", "coinbase_premium"],
            },
        },
        "signals": signals,
        "markers": markers,
        "visualization": {
            "enabled_charts": ["structure_map", "order_block_map", "fvg_map", "liquidity_map", "premium_discount_map", "ote_map"],
            "future_charts": ["session_judas_map", "sweep_confirmation_map", "mtf_composite", "trade_setup_map", "crypto_liquidation_overlay"],
        },
        "config": cfg.__dict__,
        "confluence_weights": confluence_weights(weights),
    }


def build_mtf_analysis(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    config: Optional[SMCConfig] = None,
    weights: Optional[dict[str, int]] = None,
    account_equity: Optional[float] = None,
) -> dict:
    """Build HTF -> MTF -> LTF SMC alignment from already fetched OHLCV frames."""
    ordered = [("htf", "HTF"), ("mtf", "MTF"), ("ltf", "LTF")]
    analyses: dict[str, dict] = {}
    for key, label in ordered:
        df = frames.get(key)
        if df is None:
            continue
        analyses[key] = build_smc_analysis(
            df,
            symbol=symbol,
            timeframe=label,
            config=config,
            weights=weights,
            account_equity=account_equity,
        )

    biases = {k: (v.get("summary") or {}).get("bias") for k, v in analyses.items()}
    htf_bias = biases.get("htf")
    ltf_bias = biases.get("ltf")
    alignment = bool(htf_bias and ltf_bias and _bias_direction(htf_bias) == _bias_direction(ltf_bias) and _bias_direction(htf_bias) != 0)
    poi = _rank_poi(analyses.get("mtf") or analyses.get("htf") or {})
    ltf_signals = list((analyses.get("ltf") or {}).get("signals") or [])
    selected_signal = ltf_signals[0] if ltf_signals else None
    if selected_signal:
        selected_signal = dict(selected_signal)
        selected_signal["mtf_alignment"] = alignment
        selected_signal["htf_bias"] = htf_bias
        selected_signal["poi"] = poi[0] if poi else None
        selected_signal["qualified"] = bool(selected_signal.get("qualified") and alignment)
        selected_signal["status"] = "qualified" if selected_signal["qualified"] else "watch"

    return {
        "symbol": symbol,
        "market": infer_market(symbol),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layers": analyses,
        "top_down": {
            "htf_bias": htf_bias,
            "mtf_bias": biases.get("mtf"),
            "ltf_bias": ltf_bias,
            "aligned": alignment,
            "poi_count": len(poi),
            "process": [
                "HTF bias",
                "HTF/MTF POI",
                "LTF sweep or CHoCH trigger",
                "SMC signal and risk gate",
            ],
        },
        "poi": poi,
        "selected_signal": selected_signal,
    }


def _bias_direction(bias: Optional[str]) -> int:
    if bias in ("bullish", "strong_bullish"):
        return 1
    if bias in ("bearish", "strong_bearish"):
        return -1
    return 0


def _rank_poi(analysis: dict) -> list[dict]:
    concepts = analysis.get("concepts") or {}
    obs = concepts.get("order_blocks") or []
    fvgs = concepts.get("fvgs") or []
    liquidity = concepts.get("liquidity") or []
    poi: list[dict] = []
    for ob in obs:
        score = 3 if ob.get("grade") == "A" else (2 if ob.get("grade") == "B" else 1)
        if ob.get("unmitigated"):
            score += 2
        poi.append(
            {
                "type": "order_block",
                "direction": ob.get("direction"),
                "top": ob.get("top"),
                "bottom": ob.get("bottom"),
                "entry": ob.get("mid"),
                "score": score,
                "source_index": ob.get("index"),
            }
        )
    for fvg in fvgs:
        score = 2 if fvg.get("displacement_confirmed") else 1
        if not fvg.get("mitigated"):
            score += 1
        poi.append(
            {
                "type": "fvg",
                "direction": fvg.get("direction"),
                "top": fvg.get("top"),
                "bottom": fvg.get("bottom"),
                "entry": fvg.get("mid"),
                "score": score,
                "source_index": fvg.get("index"),
            }
        )
    for liq in liquidity:
        poi.append(
            {
                "type": "liquidity",
                "direction": liq.get("direction"),
                "level": liq.get("level"),
                "score": 2 + int(liq.get("touches") or 0),
                "swept": liq.get("swept"),
                "source_index": liq.get("end_index"),
            }
        )
    return sorted(poi, key=lambda x: (x.get("score") or 0, x.get("source_index") or 0), reverse=True)[:20]
