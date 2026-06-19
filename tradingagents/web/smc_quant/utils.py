from __future__ import annotations
import math
from typing import Any, Optional
import numpy as np
import pandas as pd

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
