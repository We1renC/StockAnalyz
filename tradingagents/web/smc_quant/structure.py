from __future__ import annotations
import numpy as np
import pandas as pd
from .config import SMCConfig
from .utils import _record_time, _ts_value, atr

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
                    "time_unix": _ts_value(idx[i]),
                    "confirm_index": i + swing_length,
                    "confirm_time": _record_time(idx[i + swing_length]),
                    "confirm_time_unix": _ts_value(idx[i + swing_length]),
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
                    "time_unix": _ts_value(idx[i]),
                    "confirm_index": i + swing_length,
                    "confirm_time": _record_time(idx[i + swing_length]),
                    "confirm_time_unix": _ts_value(idx[i + swing_length]),
                    "type": "low",
                    "direction": 1,
                    "level": round(float(lows[i]), 4),
                    "scope": label,
                    "lookahead_safe": True,
                }
            )
    return sorted(swings, key=lambda x: (x["index"], x["type"]))


def detect_swings_lookahead_safe(df: pd.DataFrame, as_of_bar: int, swing_length: int = 5, label: str = "swing") -> list[dict]:
    """只返回在 as_of_bar 前已完全確認的 swings（防前視偏誤）"""
    all_swings = detect_swings(df, swing_length, label)
    return [s for s in all_swings if s["confirm_index"] <= as_of_bar]


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
            atr_mult = (body / atr_v) if atr_v > 0 else 0.0
            if atr_mult >= 2.5:
                strength = "extreme"
            elif atr_mult >= 1.8:
                strength = "strong"
            elif atr_mult >= cfg.displacement_atr_mult:
                strength = "normal"
            else:
                strength = "body_only"
            out.append(
                {
                    "index": i,
                    "time": _record_time(ts),
                    "direction": direction,
                    "body": round(body, 4),
                    "range": round(rng, 4),
                    "atr": round(atr_v, 4),
                    "body_ratio": round(body_ratio, 3),
                    "atr_multiple": round(atr_mult, 3),
                    "strength": strength,
                }
            )
    return out
