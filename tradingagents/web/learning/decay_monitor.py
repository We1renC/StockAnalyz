"""Performance decay monitoring engine.

Detects deterioration of strategic edge by comparing recent trading results
with historical baseline performance.
"""

from __future__ import annotations

import pandas as pd


def detect_edge_decay(df: pd.DataFrame, window_size: int = 20) -> dict:
    """Analyze sliding window performance to detect strategy edge decay.
    
    Args:
        df: Flattened trade records DataFrame.
        window_size: Lookback count for "recent" trades.
        
    Returns:
        Dict containing decay diagnostics:
          - 'is_decaying': True if recent performance has decayed significantly.
          - 'overall_expectancy': Mean R-multiple across all trades.
          - 'recent_expectancy': Mean R-multiple of the most recent N trades.
          - 'overall_win_rate': Overall win rate.
          - 'recent_win_rate': Win rate of the most recent N trades.
          - 'warning_message': Text explanation of the alert if active.
    """
    if df.empty or len(df) < window_size:
        return {
            "is_decaying": False,
            "overall_expectancy": 0.0,
            "recent_expectancy": 0.0,
            "overall_win_rate": 0.0,
            "recent_win_rate": 0.0,
            "warning_message": "Insufficient trades to monitor decay (need at least 20)",
        }
        
    df_sorted = df.sort_values("entry_time").copy()
    
    overall_expectancy = float(df_sorted["r_multiple"].mean())
    overall_win_rate = float(df_sorted["win"].mean())
    
    recent_trades = df_sorted.tail(window_size)
    recent_expectancy = float(recent_trades["r_multiple"].mean())
    recent_win_rate = float(recent_trades["win"].mean())
    
    is_decaying = False
    warning_message = None
    
    # Trigger conditions:
    # 1. Recent expectancy goes negative while overall was positive.
    # 2. Recent win rate drops below 35% and is at least 15% lower than overall.
    if overall_expectancy > 0.05 and recent_expectancy <= 0.0:
        is_decaying = True
        warning_message = (
            f"Edge decay detected! Recent {window_size} trades expectancy "
            f"dropped to {recent_expectancy:.3f}R (Historical baseline: {overall_expectancy:.3f}R)"
        )
    elif overall_win_rate - recent_win_rate >= 0.15 and recent_win_rate < 0.35:
        is_decaying = True
        warning_message = (
            f"Edge decay detected! Recent {window_size} trades win rate "
            f"dropped to {recent_win_rate * 100:.1f}% (Historical baseline: {overall_win_rate * 100:.1f}%)"
        )
        
    return {
        "is_decaying": is_decaying,
        "overall_expectancy": round(overall_expectancy, 4),
        "recent_expectancy": round(recent_expectancy, 4),
        "overall_win_rate": round(overall_win_rate, 4),
        "recent_win_rate": round(recent_win_rate, 4),
        "warning_message": warning_message,
    }
