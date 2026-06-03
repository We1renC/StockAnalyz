"""CVD Divergence and Spot vs Perp Order Flow confirmation.

Identifies buying/selling exhaustion through Cumulative Volume Delta (CVD) divergences,
and classifies moves as spot-driven (genuine) or perp-driven (speculative).
"""

from __future__ import annotations

import pandas as pd


def detect_cvd_divergence(
    prices: pd.Series,
    cvd: pd.Series,
    lookback: int = 15,
) -> dict:
    """Detect bullish and bearish CVD divergences.
    
    Args:
        prices: Series of prices (usually close or high/low).
        cvd: Cumulative Volume Delta series.
        lookback: Period to search for peaks/troughs.
        
    Returns:
        Dict containing:
          - 'bearish_cvd_divergence': True if price made a higher high but CVD made a lower high.
          - 'bullish_cvd_divergence': True if price made a lower low but CVD made a higher low.
    """
    if len(prices) < lookback + 5 or len(cvd) < lookback + 5:
        return {"bearish_cvd_divergence": False, "bullish_cvd_divergence": False}
        
    p_last = float(prices.iloc[-1])
    c_last = float(cvd.iloc[-1])
    
    # Find historical peaks/troughs in the lookback window (excluding the last few bars to find clear turning points)
    window_prices = prices.iloc[-lookback-5:-2]
    window_cvd = cvd.iloc[-lookback-5:-2]
    
    p_max = float(window_prices.max())
    p_min = float(window_prices.min())
    
    c_max = float(window_cvd.max())
    c_min = float(window_cvd.min())
    
    bearish_cvd_divergence = False
    bullish_cvd_divergence = False
    
    # Bearish Divergence: Price higher high, CVD lower high
    if p_last > p_max and c_last < c_max:
        bearish_cvd_divergence = True
        
    # Bullish Divergence: Price lower low, CVD higher low
    if p_last < p_min and c_last > c_min:
        bullish_cvd_divergence = True
        
    return {
        "bearish_cvd_divergence": bearish_cvd_divergence,
        "bullish_cvd_divergence": bullish_cvd_divergence,
    }


def classify_order_flow(
    spot_cvd_change: float,
    perp_cvd_change: float,
) -> dict:
    """Classify order flow as spot-driven or perp-driven.
    
    Args:
        spot_cvd_change: Change in Spot CVD over the window.
        perp_cvd_change: Change in Perp CVD over the window.
        
    Returns:
        Dict containing:
          - 'spot_driven': True if the move is dominated by spot buying/selling.
          - 'perp_driven': True if the move is dominated by leveraged perp buying/selling.
          - 'source_type': 'spot_genuine', 'perp_speculative', or 'balanced'
    """
    abs_spot = abs(spot_cvd_change)
    abs_perp = abs(perp_cvd_change)
    
    total = abs_spot + abs_perp
    if total <= 0.0001:
        return {"spot_driven": False, "perp_driven": False, "source_type": "balanced"}
        
    spot_pct = abs_spot / total
    
    if spot_pct >= 0.60:
        return {"spot_driven": True, "perp_driven": False, "source_type": "spot_genuine"}
    elif spot_pct <= 0.40:
        return {"spot_driven": False, "perp_driven": True, "source_type": "perp_speculative"}
    else:
        return {"spot_driven": False, "perp_driven": False, "source_type": "balanced"}
