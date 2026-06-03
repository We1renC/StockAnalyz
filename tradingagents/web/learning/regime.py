"""Market regime classifier based on moving average alignment and volatility.

Provides classification of trending vs ranging status and volatility levels.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def classify_market_regime(df: pd.DataFrame, window: int = 20) -> dict:
    """Classify the market regime for the last bar in the DataFrame.
    
    Args:
        df: Normalized OHLCV DataFrame.
        window: Volatility ranking lookback window.
        
    Returns:
        Dict containing regime metrics for the final bar:
          - 'regime_trend': 'trending_bullish', 'trending_bearish', or 'ranging'
          - 'regime_volatility': 'high', 'normal', or 'low'
          - 'atr_pct': ATR as a percentage of close price
          - 'is_trending': True if trending, False if ranging
    """
    if df is None or len(df) < 60:  # Need sufficient bars for SMA60
        return {
            "regime_trend": "ranging",
            "regime_volatility": "normal",
            "atr_pct": 0.02,
            "is_trending": False,
        }
        
    close = df["close"]
    high = df["high"]
    low = df["low"]
    
    # Trend detection via MA alignment
    sma20 = close.rolling(20).mean()
    sma60 = close.rolling(60).mean()
    
    last_close = float(close.iloc[-1])
    last_sma20 = float(sma20.iloc[-1])
    last_sma60 = float(sma60.iloc[-1])
    
    if last_close > last_sma20 and last_sma20 > last_sma60:
        regime_trend = "trending_bullish"
        is_trending = True
    elif last_close < last_sma20 and last_sma20 < last_sma60:
        regime_trend = "trending_bearish"
        is_trending = True
    else:
        regime_trend = "ranging"
        is_trending = False
        
    # Volatility detection via ATR percentage relative to close
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()
    atr_pct = atr / close
    
    # Rolling percentile rank of ATR % to determine relative volatility state
    last_atr_pct = float(atr_pct.iloc[-1]) if not pd.isna(atr_pct.iloc[-1]) else 0.02
    
    # Calculate historical ATR% percentile rank
    hist_atr_pct = atr_pct.dropna().tail(250)  # Look back at last 250 bars
    if len(hist_atr_pct) >= window:
        q25 = np.percentile(hist_atr_pct, 25)
        q75 = np.percentile(hist_atr_pct, 75)
        
        if last_atr_pct >= q75:
            regime_volatility = "high"
        elif last_atr_pct <= q25:
            regime_volatility = "low"
        else:
            regime_volatility = "normal"
    else:
        regime_volatility = "normal"
        
    return {
        "regime_trend": regime_trend,
        "regime_volatility": regime_volatility,
        "atr_pct": round(last_atr_pct, 5),
        "is_trending": is_trending,
    }
