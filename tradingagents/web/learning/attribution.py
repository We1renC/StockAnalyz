"""Win/Loss attribution and edge discovery engine.

Computes expected values, factor lift, R-multiple distributions,
and MAE/MFE statistics to optimize stop-loss and take-profit parameters.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_expectancy(win_rate: float, avg_win_r: float, avg_loss_r: float) -> float:
    """Calculate the expectancy of a strategy in R-multiples.
    
    Expectancy = (Win Rate * Avg Win R) - ((1 - Win Rate) * Avg Loss R)
    Here Avg Loss R is positive.
    """
    loss_rate = 1.0 - win_rate
    return (win_rate * avg_win_r) - (loss_rate * abs(avg_loss_r))


def generate_attribution_report(df: pd.DataFrame) -> dict:
    """Analyze trade records to discover edges, factor lifts, and MAE/MFE optimizations.
    
    Args:
        df: Flattened trade records DataFrame.
        
    Returns:
        Dict containing attribution analysis reports.
    """
    if df.empty or len(df) < 5:
        return {
            "total_trades": len(df),
            "overall": {},
            "factors": {},
            "models": {},
            "mae_mfe_recommendations": {},
        }
        
    # 1. Overall Metrics
    total_trades = len(df)
    wins_df = df[df["win"] == 1]
    losses_df = df[df["win"] == 0]
    
    win_rate = len(wins_df) / total_trades
    avg_r = float(df["r_multiple"].mean())
    
    avg_win_r = float(wins_df["r_multiple"].mean()) if not wins_df.empty else 0.0
    avg_loss_r = float(losses_df["r_multiple"].mean()) if not losses_df.empty else 0.0
    
    gross_profit = float(wins_df["pnl"].sum())
    gross_loss = float(losses_df["pnl"].abs().sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    
    overall_expectancy = calculate_expectancy(win_rate, avg_win_r, avg_loss_r)
    
    report = {
        "total_trades": total_trades,
        "overall": {
            "win_rate": round(win_rate, 4),
            "expected_r": round(avg_r, 4),
            "avg_win_r": round(avg_win_r, 4),
            "avg_loss_r": round(avg_loss_r, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else 99.0,
            "expectancy_r": round(overall_expectancy, 4),
        },
        "factors": {},
        "models": {},
        "mae_mfe_recommendations": {},
    }
    
    # 2. Single-Factor expected value / lift slicing
    # We identify boolean factor columns
    candidate_factors = [
        "htf_bias_alignment",
        "premium_discount_alignment",
        "unmitigated_ob",
        "unfilled_fvg",
        "liquidity_sweep",
        "ltf_choch",
        "ote_zone",
        "killzone",
        "displacement",
        "unicorn_pattern",
        "smt_divergence_pattern",
        "silver_bullet_pattern",
        "power_of_three_pattern",
    ]
    
    factor_cols = [c for c in candidate_factors if c in df.columns]
    
    for col in factor_cols:
        # Filter rows where factor is True vs False
        df_true = df[df[col] == True]
        df_false = df[df[col] == False]
        
        if len(df_true) < 2:
            continue
            
        t_wins = df_true[df_true["win"] == 1]
        t_losses = df_true[df_true["win"] == 0]
        
        t_wr = len(t_wins) / len(df_true)
        t_avg_r = float(df_true["r_multiple"].mean())
        t_avg_win = float(t_wins["r_multiple"].mean()) if not t_wins.empty else 0.0
        t_avg_loss = float(t_losses["r_multiple"].mean()) if not t_losses.empty else 0.0
        
        # Lift relative to overall expected R (or difference if overall expected R is near zero)
        lift = t_avg_r / avg_r if abs(avg_r) > 0.01 else 1.0
        diff_expectancy = t_avg_r - avg_r
        
        report["factors"][col] = {
            "count": len(df_true),
            "win_rate": round(t_wr, 4),
            "expected_r": round(t_avg_r, 4),
            "avg_win_r": round(t_avg_win, 4),
            "avg_loss_r": round(t_avg_loss, 4),
            "lift": round(lift, 4),
            "diff_expectancy": round(diff_expectancy, 4),
        }
        
    # 3. Entry Model attribution
    if "model" in df.columns:
        models = df["model"].unique()
        for m in models:
            df_m = df[df["model"] == m]
            if len(df_m) < 2:
                continue
            m_wins = df_m[df_m["win"] == 1]
            m_losses = df_m[df_m["win"] == 0]
            
            m_wr = len(m_wins) / len(df_m)
            m_avg_r = float(df_m["r_multiple"].mean())
            m_avg_win = float(m_wins["r_multiple"].mean()) if not m_wins.empty else 0.0
            m_avg_loss = float(m_losses["r_multiple"].mean()) if not m_losses.empty else 0.0
            
            report["models"][m] = {
                "count": len(df_m),
                "win_rate": round(m_wr, 4),
                "expected_r": round(m_avg_r, 4),
                "avg_win_r": round(m_avg_win, 4),
                "avg_loss_r": round(m_avg_loss, 4),
            }
            
    # 4. MAE / MFE Analysis
    # Let's check if we have entry_price, stop_price, mae, and mfe columns
    req_mae_mfe = {"entry_price", "stop_price", "mae", "mfe"}
    if req_mae_mfe.issubset(df.columns):
        numeric_cols = ["entry_price", "stop_price", "mae", "mfe", "win"]
        df_num = df.copy()
        for col in numeric_cols:
            df_num[col] = pd.to_numeric(df_num[col], errors="coerce")

        # Calculate stop loss distance for each trade
        stop_dist = (df_num["entry_price"] - df_num["stop_price"]).abs()
        
        # Valid trades where stop distance is positive
        valid_idx = (
            stop_dist > 0.0001
        ) & df_num["mae"].notna() & df_num["mfe"].notna()
        
        if valid_idx.sum() >= 3:
            df_v = df_num[valid_idx].copy()
            df_v["stop_dist"] = stop_dist[valid_idx]
            
            # MAE is stored as negative for long drawdown, let's normalize MAE to positive distance
            # mae_ratio = abs(mae) / stop_dist
            df_v["mae_ratio"] = df_v["mae"].abs() / df_v["stop_dist"]
            df_v["mfe_ratio"] = df_v["mfe"].abs() / df_v["stop_dist"]
            
            v_wins = df_v[df_v["win"] == 1]
            v_losses = df_v[df_v["win"] == 0]
            
            # Analyze Winners' MAE
            if not v_wins.empty:
                # Quantile of winner MAE ratio
                mae_90 = float(v_wins["mae_ratio"].quantile(0.90))
                mae_70 = float(v_wins["mae_ratio"].quantile(0.70))
                mae_50 = float(v_wins["mae_ratio"].quantile(0.50))
                
                # Check if >= 30% of winners have MAE ratio >= 0.7
                heavy_drawdown_winners = (v_wins["mae_ratio"] >= 0.7).sum() / len(v_wins)
                
                stop_loss_rec = "keep_current"
                if heavy_drawdown_winners >= 0.30:
                    stop_loss_rec = "widen_stop_loss_atr"
                elif mae_90 < 0.4:
                    stop_loss_rec = "tighten_stop_loss_atr"
                    
                report["mae_mfe_recommendations"]["stop_loss"] = {
                    "winner_mae_ratio_50pct": round(mae_50, 4),
                    "winner_mae_ratio_70pct": round(mae_70, 4),
                    "winner_mae_ratio_90pct": round(mae_90, 4),
                    "heavy_drawdown_winners_pct": round(heavy_drawdown_winners * 100, 2),
                    "recommendation": stop_loss_rec,
                }
                
            # Analyze Losers' MFE (Did they go into significant profit before getting stopped?)
            if not v_losses.empty:
                # Percentage of losers that reached at least 1.0 R-multiple equivalent in profit
                losers_reached_1r = (v_losses["mfe_ratio"] >= 1.0).sum() / len(v_losses)
                losers_reached_05r = (v_losses["mfe_ratio"] >= 0.5).sum() / len(v_losses)
                
                profit_taking_rec = "keep_current"
                if losers_reached_1r >= 0.25:
                    profit_taking_rec = "implement_breakeven_trailing_at_1r"
                elif losers_reached_05r >= 0.40:
                    profit_taking_rec = "tighten_tp1_or_trail_stop"
                    
                report["mae_mfe_recommendations"]["profit_taking"] = {
                    "losers_reached_0_5r_pct": round(losers_reached_05r * 100, 2),
                    "losers_reached_1_0r_pct": round(losers_reached_1r * 100, 2),
                    "recommendation": profit_taking_rec,
                }
                
    return report
