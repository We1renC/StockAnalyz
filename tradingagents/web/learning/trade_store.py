"""Storage and formatting utilities for closed-loop trade records.

Enables reading trade records from SQLite, flattening JSON features,
and exporting them to CSV/Parquet formats.
"""

from __future__ import annotations

import json
from pathlib import Path
import pandas as pd


def load_trades_from_db(conn, symbol: str | None = None) -> pd.DataFrame:
    """Load trades from the SQLite database and flatten the features.
    
    Args:
        conn: SQLite connection.
        symbol: Optional filter by symbol.
        
    Returns:
        DataFrame containing flat trade records with feature columns expanded.
    """
    query = "SELECT * FROM smc_backtest_trades"
    params = []
    if symbol:
        query += " WHERE symbol = ?"
        params.append(symbol.upper())
        
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return pd.DataFrame()
        
    # Unpack JSON columns
    feature_list = []
    dol_list = []
    
    for _, row in df.iterrows():
        # Feature vector
        fv = {}
        fv_str = row.get("feature_vector")
        if fv_str:
            try:
                fv = json.loads(fv_str)
            except Exception:
                pass
        feature_list.append(fv)
        
        # DOL target
        dol = {}
        dol_str = row.get("dol_target")
        if dol_str:
            try:
                dol = json.loads(dol_str)
            except Exception:
                pass
        dol_list.append(dol)
        
    features_df = pd.DataFrame(feature_list, index=df.index)
    
    # Ensure there are no name collisions between original columns and features
    clashing = set(df.columns).intersection(set(features_df.columns))
    if clashing:
        features_df = features_df.drop(columns=list(clashing))
        
    # Standardize types and fill missing values for boolean/numeric features
    for col in features_df.columns:
        # Check if the column is boolean-like
        if features_df[col].dropna().apply(lambda x: isinstance(x, bool) or x in (0, 1, 0.0, 1.0)).all():
            features_df[col] = features_df[col].fillna(False).astype(bool)
            
    # Combine back into a single flat DataFrame
    flat_df = pd.concat([df.drop(columns=["feature_vector"]), features_df], axis=1)
    
    # Add flattened DOL columns for easier analysis
    flat_df["dol_type"] = [d.get("type") for d in dol_list]
    flat_df["dol_level"] = [d.get("level") for d in dol_list]
    flat_df["dol_distance_pct"] = [d.get("distance_pct") for d in dol_list]
    
    return flat_df


def export_trades_to_csv(df: pd.DataFrame, filepath: str | Path) -> None:
    """Export the flattened trade DataFrame to CSV.
    
    Args:
        df: Flattened trade records.
        filepath: Path to save the CSV.
    """
    if df.empty:
        # Save empty CSV with headers if possible, or just touch it
        pd.DataFrame().to_csv(filepath, index=False)
        return
    df.to_csv(filepath, index=False)
