"""Purged cross-validation and time-series validation tools.

Prevents information leakage between overlapping trade labels during validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_sharpe_ratio(r_multiples: pd.Series) -> float:
    """Calculate the Sharpe Ratio of trade R-multiples.
    
    Args:
        r_multiples: Series of trade R-multiples.
        
    Returns:
        Sharpe Ratio (unannualized, based on trade frequency).
    """
    if len(r_multiples) < 2:
        return 0.0
    mean_r = r_multiples.mean()
    std_r = r_multiples.std(ddof=1)
    if std_r <= 0.0001:
        return 0.0
    return float(mean_r / std_r)


def purged_train_test_split(
    df: pd.DataFrame,
    train_pct: float = 0.7,
    purge_hours: float = 48.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Perform a purged train/test split on time-ordered trade records.
    
    Removes trades from the test set that overlap in time with trades in
    the train set (based on exit_time and entry_time), preventing label leakage.
    
    Args:
        df: Trade records DataFrame.
        train_pct: Percentage of data to use for training.
        purge_hours: Hours of safety margin to purge after the train set ends.
        
    Returns:
        Tuple of (train_df, test_df).
    """
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
        
    # Ensure sorted by entry_time
    if "entry_time" in df.columns:
        df_sorted = df.sort_values("entry_time").copy()
    else:
        df_sorted = df.copy()
        
    n = len(df_sorted)
    split_idx = int(n * train_pct)
    if split_idx == 0 or split_idx >= n:
        return df_sorted, pd.DataFrame()
        
    train_df = df_sorted.iloc[:split_idx]
    test_candidate_df = df_sorted.iloc[split_idx:]
    
    # Purging: Find the max exit time in training set
    if "exit_time" in df_sorted.columns:
        train_exits = pd.to_datetime(train_df["exit_time"], errors="coerce")
        max_train_exit = train_exits.max()
        
        if pd.isna(max_train_exit):
            # Fallback if timestamps are missing
            return train_df, test_candidate_df
            
        # Purge threshold: max_train_exit + purge_hours
        purge_threshold = max_train_exit + pd.Timedelta(hours=purge_hours)
        
        test_entries = pd.to_datetime(test_candidate_df["entry_time"], errors="coerce")
        # Keep only test trades that start after the purge threshold
        test_df = test_candidate_df[test_entries >= purge_threshold]
    else:
        # Fallback to no-purge time series split
        test_df = test_candidate_df
        
    return train_df, test_df


def estimate_backtest_overfitting(
    train_r: pd.Series,
    test_r: pd.Series,
) -> dict:
    """Compare train vs out-of-sample performance to estimate overfitting risk.
    
    Args:
        train_r: R-multiples from train set.
        test_r: R-multiples from test set.
        
    Returns:
        Dict with overfitting metrics and warnings.
    """
    train_sharpe = calculate_sharpe_ratio(train_r)
    test_sharpe = calculate_sharpe_ratio(test_r)
    
    # Sharpe decay: ratio of OOS to In-Sample Sharpe
    decay = 1.0 - (test_sharpe / train_sharpe) if train_sharpe > 0.001 else 0.0
    
    # Overfitting risk classification
    if train_sharpe > 0.5 and test_sharpe <= 0.0:
        risk = "critical_overfit"
    elif decay > 0.5:
        risk = "high"
    elif decay > 0.2:
        risk = "moderate"
    else:
        risk = "low"
        
    return {
        "train_sharpe": round(train_sharpe, 4),
        "test_sharpe": round(test_sharpe, 4),
        "sharpe_decay": round(decay, 4),
        "overfitting_probability_approx": round(max(0.0, min(1.0, decay)), 4) if train_sharpe > 0 else 0.5,
        "overfitting_risk_level": risk,
    }
