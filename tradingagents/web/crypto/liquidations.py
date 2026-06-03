"""Liquidation cluster detection and Open Interest (OI) sweep validation.

Quantifies real liquidity zones using modeled liquidation clusters and verifies
reversals by matching price sweeps with Open Interest drop signatures.
"""

from __future__ import annotations

import pandas as pd


def detect_liquidation_clusters(
    df: pd.DataFrame,
    swings: list[dict],
    atr_mult: float = 0.5,
) -> list[dict]:
    """Identify potential liquidation clusters near swing levels.
    
    Models stop-loss clusters based on the time a swing level has survived
    without being broken. Longer survival leads to denser clusters.
    
    Args:
        df: Normalized OHLCV DataFrame.
        swings: List of swing high/low dictionaries.
        atr_mult: Distances to place the cluster from the swing level in ATRs.
        
    Returns:
        List of liquidation cluster dicts:
          - 'level': Price level of the cluster.
          - 'type': 'BSL_LIQ' (above high) or 'SSL_LIQ' (below low).
          - 'strength': Modeled density (1 to 5).
          - 'swing_index': Original swing bar index.
    """
    clusters = []
    if not swings or len(df) < 15:
        return clusters
        
    # Calculate ATR for scaling
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    
    # Analyze confirmed swings
    for s in swings:
        idx = s["index"]
        level = s["level"]
        kind = s["type"]
        
        # Check how long this swing has survived (unbroken up to end of df)
        survived_bars = len(df) - idx
        
        # We only care about relatively recent unbroken swings
        is_broken = False
        for j in range(idx + 1, len(df)):
            if kind == "high" and df["high"].iloc[j] > level:
                is_broken = True
                break
            if kind == "low" and df["low"].iloc[j] < level:
                is_broken = True
                break
                
        if is_broken:
            continue
            
        # Strength scales with survival duration (max 5)
        strength = min(5, max(1, int(survived_bars / 10)))
        
        # Place liquidation clusters slightly offset from swing levels
        # Short liquidations (BSL_LIQ) are placed slightly above swing highs
        # Long liquidations (SSL_LIQ) are placed slightly below swing lows
        offset = atr * atr_mult
        
        if kind == "high":
            clusters.append({
                "level": round(level + offset, 4),
                "type": "BSL_LIQ",
                "strength": strength,
                "swing_index": idx,
            })
        else:
            clusters.append({
                "level": round(level - offset, 4),
                "type": "SSL_LIQ",
                "strength": strength,
                "swing_index": idx,
            })
            
    return clusters


def confirm_liquidation_sweep(
    price: float,
    swing_level: float,
    direction: int,  # 1 for bullish (sweeping low), -1 for bearish (sweeping high)
    oi_change_pct: float,
) -> tuple[bool, str]:
    """Validate if a sweep is confirmed by an Open Interest squeeze drop.
    
    Args:
        price: Current close or high/low price.
        swing_level: The price level being swept.
        direction: 1 (sweeping low, long squeeze), -1 (sweeping high, short squeeze).
        oi_change_pct: Percentage change in Open Interest over the sweep window (e.g. -0.03 for -3%).
        
    Returns:
        Tuple of (is_confirmed, reason).
    """
    # Check if price actually swept/pierced the level
    is_sweep = False
    if direction == 1 and price < swing_level:
        is_sweep = True
    elif direction == -1 and price > swing_level:
        is_sweep = True
        
    if not is_sweep:
        return False, "level not swept"
        
    # An Open Interest drop indicates that leveraged positions were closed (squeezed/liquidated)
    # Threshold for confirmation is a drop of 1.5% or more
    if oi_change_pct <= -0.015:
        reason = f"confirmed by OI drop of {oi_change_pct*100:.2f}% (squeeze reversal)"
        return True, reason
        
    return False, f"insufficient OI drop ({oi_change_pct*100:.2f}%)"
