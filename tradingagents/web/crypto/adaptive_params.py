"""Adaptive parameter optimization based on asset price volatility.

Adjusts SMC parameters like liquidity range percentage and stop ATR multipliers
dynamically based on the current ATR level and volatility regime.
"""

from __future__ import annotations

import pandas as pd


def get_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Calculate the Average True Range (ATR) indicator."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def calculate_adaptive_params(
    df: pd.DataFrame,
    symbol: str,
    base_k: float = 0.5,
) -> dict:
    """Calculate dynamic parameters for SMC detection based on ATR.
    
    Args:
        df: Normalized OHLCV DataFrame.
        symbol: Ticker symbol.
        base_k: Scaling factor for dynamic range percent.
        
    Returns:
        Dict containing:
          - 'range_percent_dyn': Dynamic liquidity range percentage.
          - 'stop_atr_mult': Volatility-adjusted ATR stop multiplier.
          - 'asset_class': 'major' for BTC/ETH, 'altcoin' for others.
    """
    upper_sym = symbol.upper()
    is_major = any(x in upper_sym for x in ("BTC", "ETH")) and "/" in upper_sym or "BTC-" in upper_sym or "ETH-" in upper_sym or upper_sym in ("BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT")
    
    # Symmetrical fallback if it doesn't match standard patterns but starts with BTC/ETH
    if not is_major:
        is_major = upper_sym.startswith(("BTC", "ETH")) and not upper_sym.startswith(("BTCD", "BTC.D"))
        
    asset_class = "major" if is_major else "altcoin"
    
    if df is None or len(df) < 15:
        # Default fallback parameters
        return {
            "range_percent_dyn": 0.01,
            "stop_atr_mult": 1.5 if is_major else 2.0,
            "asset_class": asset_class,
        }
        
    atr = get_atr(df)
    last_atr = float(atr.iloc[-1])
    last_close = float(df["close"].iloc[-1])
    
    # Calculate ATR% of close price
    atr_pct = last_atr / last_close if last_close > 0 else 0.02
    
    # Dynamic range percent: k * ATR%
    # Clamped to reasonable values [0.002, 0.03] to prevent extreme values
    range_percent_dyn = max(0.002, min(0.03, base_k * atr_pct))
    
    # Dynamic stop-loss ATR multiplier
    # Altcoins require wider stops to absorb spikes/volatility
    if asset_class == "major":
        stop_atr_mult = 1.5
    else:
        # Altcoin: scale up based on ATR%
        if atr_pct > 0.05:  # extremely high volatility altcoin
            stop_atr_mult = 2.5
        else:
            stop_atr_mult = 2.0
            
    return {
        "range_percent_dyn": round(range_percent_dyn, 5),
        "stop_atr_mult": stop_atr_mult,
        "asset_class": asset_class,
    }
