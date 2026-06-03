"""Storage and formatting utilities for closed-loop trade records.

Enables reading trade records from SQLite, flattening JSON features,
and exporting them to CSV/Parquet formats.
"""

from __future__ import annotations

import json
from pathlib import Path
import pandas as pd


def load_trades_from_db(conn, symbol: str | None = None, include_journal: bool = False) -> pd.DataFrame:
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
    if include_journal:
        journal_query = """
            SELECT
                NULL AS id,
                NULL AS run_id,
                symbol,
                market,
                timeframe,
                journal_key AS trade_id,
                direction,
                model,
                entry_time,
                exit_time,
                entry_price,
                exit_price,
                stop_price,
                tp1_price,
                qty,
                pnl,
                r_multiple,
                confluence_score AS score,
                NULL AS threshold,
                feature_vector,
                dol_target,
                status AS exit_reason,
                NULL AS holding_bars,
                CASE
                    WHEN pnl IS NOT NULL AND pnl > 0 THEN 1
                    WHEN pnl IS NULL AND r_multiple IS NOT NULL AND r_multiple > 0 THEN 1
                    ELSE 0
                END AS win,
                NULL AS mae,
                NULL AS mfe,
                environment,
                emotion,
                'journal' AS sample_source
            FROM smc_trade_journal
            WHERE status = 'closed'
        """
        journal_params = []
        if symbol:
            journal_query += " AND symbol = ?"
            journal_params.append(symbol.upper())
        journal_df = pd.read_sql_query(journal_query, conn, params=journal_params)
        if not journal_df.empty:
            if df.empty:
                df = journal_df
            else:
                if "sample_source" not in df.columns:
                    df["sample_source"] = "backtest"
                for col in journal_df.columns:
                    if col not in df.columns:
                        df[col] = None
                for col in df.columns:
                    if col not in journal_df.columns:
                        journal_df[col] = None
                df = pd.concat([df[df.columns], journal_df[df.columns]], ignore_index=True)
    if df.empty:
        return pd.DataFrame()
    if "sample_source" not in df.columns:
        df["sample_source"] = "backtest"
        
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
    drop_cols = ["feature_vector"] if "feature_vector" in df.columns else []
    flat_df = pd.concat([df.drop(columns=drop_cols), features_df], axis=1)
    
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
