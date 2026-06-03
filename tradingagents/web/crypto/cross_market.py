"""Cross-market indicators and multi-exchange SMT divergence.

Implements Coinbase premium, weekend CME跳空缺口 (skip gaps) detection,
cross-exchange SMT divergence, and BTC Dominance/Altcoin alignment rules.
"""

from __future__ import annotations

import pandas as pd


def calculate_coinbase_premium(
    coinbase_close: pd.Series,
    binance_close: pd.Series,
) -> pd.Series:
    """Calculate the Coinbase Premium Index.
    
    Premium % = ((Coinbase Close - Binance Close) / Binance Close) * 100
    """
    # Align the indices
    aligned = pd.concat([coinbase_close.rename("cb"), binance_close.rename("bn")], axis=1).dropna()
    if aligned.empty:
        return pd.Series(dtype=float)
    return (aligned["cb"] - aligned["bn"]) / aligned["bn"] * 100


def detect_cme_gaps(
    df: pd.DataFrame,
    threshold_pct: float = 0.005,
) -> list[dict]:
    """Detect skips or gaps generated over weekend closures.
    
    Since crypto trades 24/7 but institutions close over weekends,
    we look for skips between Friday 17:00 EST and Sunday 18:00 EST close/open levels.
    
    Args:
        df: 24/7 OHLCV DataFrame (ideally BTC).
        threshold_pct: Minimum gap size to register (e.g. 0.5%).
        
    Returns:
        List of CME gap dicts:
          - 'top': High boundary of the gap.
          - 'bottom': Low boundary of the gap.
          - 'filled': True if subsequent price has filled the gap.
          - 'time': Timestamp of the gap formation.
    """
    gaps = []
    if df is None or len(df) < 2:
        return gaps
        
    # Standardize index to DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df = df.copy()
            df.index = pd.to_datetime(df.index)
        except Exception:
            return gaps
            
    # Look for Friday close (around 16:00-17:00 New York) vs Sunday open (around 18:00 New York)
    # In a simplified version on a 24/7 daily chart, the gap occurs between Friday Close and Sunday Open
    # Let's inspect the daily jump between Friday Close and Saturday Open (since Saturday/Sunday represent TradFi skip)
    for i in range(1, len(df)):
        prev_idx = df.index[i - 1]
        curr_idx = df.index[i]
        
        # Check if prev_idx is Friday (dayofweek = 4) and curr_idx is Saturday (dayofweek = 5)
        # or gap across weekends
        if prev_idx.dayofweek == 4 and curr_idx.dayofweek == 5:
            prev_close = float(df["close"].iloc[i - 1])
            curr_open = float(df["open"].iloc[i])
            
            diff = abs(curr_open - prev_close) / prev_close
            if diff >= threshold_pct:
                top = max(prev_close, curr_open)
                bottom = min(prev_close, curr_open)
                
                # Check if it has been filled by subsequent price action
                filled = False
                for j in range(i, len(df)):
                    low_j = float(df["low"].iloc[j])
                    high_j = float(df["high"].iloc[j])
                    if low_j <= bottom and high_j >= top:
                        filled = True
                        break
                        
                gaps.append({
                    "top": round(top, 4),
                    "bottom": round(bottom, 4),
                    "filled": filled,
                    "time": curr_idx.isoformat(),
                })
                
    return gaps


def detect_cross_exchange_smt(
    binance_df: pd.DataFrame,
    coinbase_df: pd.DataFrame,
    lookback_bars: int = 15,
) -> dict:
    """Detect cross-exchange SMT divergence for the same asset.
    
    Institutional demand often manifests on Coinbase. If Binance sweeps a low
    but Coinbase holds above its low, it confirms strong institutional buying.
    
    Args:
        binance_df: Binance OHLCV DataFrame.
        coinbase_df: Coinbase OHLCV DataFrame.
        lookback_bars: Window to look back for swing points.
        
    Returns:
        Dict containing:
          - 'bullish_exchange_smt': True if Binance made a lower low but Coinbase held higher low.
          - 'bearish_exchange_smt': True if Binance made a higher high but Coinbase held lower high.
    """
    if len(binance_df) < lookback_bars or len(coinbase_df) < lookback_bars or lookback_bars < 2:
        return {"bullish_exchange_smt": False, "bearish_exchange_smt": False}
        
    # Align the data
    b_lows = binance_df["low"].tail(lookback_bars)
    c_lows = coinbase_df["low"].tail(lookback_bars)
    
    b_highs = binance_df["high"].tail(lookback_bars)
    c_highs = coinbase_df["high"].tail(lookback_bars)
    
    # Check last bar low vs minimum of the previous window
    b_last_low = float(b_lows.iloc[-1])
    b_prev_min = float(b_lows.iloc[:-1].min())
    
    c_last_low = float(c_lows.iloc[-1])
    c_prev_min = float(c_lows.iloc[:-1].min())
    
    bullish_smt = False
    if b_last_low < b_prev_min and c_last_low > c_prev_min:
        bullish_smt = True
        
    # Check last bar high vs maximum of the previous window
    b_last_high = float(b_highs.iloc[-1])
    b_prev_max = float(b_highs.iloc[:-1].max())
    
    c_last_high = float(c_highs.iloc[-1])
    c_prev_max = float(c_highs.iloc[:-1].max())
    
    bearish_smt = False
    if b_last_high > b_prev_max and c_last_high < c_prev_max:
        bearish_smt = True
        
    return {
        "bullish_exchange_smt": bullish_smt,
        "bearish_exchange_smt": bearish_smt,
    }


def align_altcoin_with_btc_bias(
    symbol: str,
    btc_bias: str,
    direction: int,  # 1 for long, -1 for short
) -> bool:
    """Validate if an Altcoin trade aligns with the Bitcoin macro trend.
    
    Args:
        symbol: Ticker of the asset (e.g. SOL/USDT).
        btc_bias: Bitcoin's HTF bias ('bullish', 'strong_bullish', 'bearish', etc.).
        direction: 1 (long), -1 (short).
        
    Returns:
        True if aligned, False if blocked.
    """
    upper_sym = symbol.upper()
    
    # If the symbol itself is BTC, it's always aligned with itself
    if "BTC" in upper_sym and not any(x in upper_sym for x in ("ETH", "SOL", "DOGE", "ADA", "XRP")):
        return True
        
    # Altcoin long trades require bullish BTC bias
    if direction == 1:
        return btc_bias in ("bullish", "strong_bullish", "neutral")
    # Altcoin short trades require bearish BTC bias
    else:
        return btc_bias in ("bearish", "strong_bearish", "neutral")
