"""Feature importance analysis for SMC confluences.

Ranks confluence factors by their impact on trade outcomes (win/loss or R-multiple).
Falls back to pure-numpy correlation if scikit-learn is not installed.
"""

from __future__ import annotations

import pandas as pd


def calculate_feature_importance(df: pd.DataFrame) -> dict:
    """Calculate the relative importance of each feature for predicting win or R-multiple.
    
    Args:
        df: Flattened trade records DataFrame.
        
    Returns:
        Dict containing importance ranks:
          - 'importances': list of dicts with 'feature', 'importance', and 'direction'
          - 'method': 'logistic_regression' or 'statistical_correlation'
    """
    candidate_features = [
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
    
    # Filter columns that are present in the dataframe
    feature_cols = [c for c in candidate_features if c in df.columns]
    
    if len(df) < 5 or not feature_cols:
        return {"importances": [], "method": "insufficient_data"}
        
    # Standardize types and fill NaNs
    X = df[feature_cols].fillna(False).astype(int)
    
    # We can try regression on r_multiple or classification on win. Let's use win.
    y_class = df["win"].fillna(0).astype(int)
    y_reg = df["r_multiple"].fillna(0.0).astype(float)
    
    try:
        from sklearn.linear_model import LogisticRegression
        import numpy as np
        
        # Check variance of features
        valid_cols = [col for col in feature_cols if X[col].nunique() > 1]
        
        if len(valid_cols) > 0 and len(np.unique(y_class)) > 1:
            X_model = X[valid_cols]
            model = LogisticRegression(penalty="l2", C=1.0, random_state=42)
            model.fit(X_model, y_class)
            
            coefs = model.coef_[0]
            importances = []
            for col, coef in zip(valid_cols, coefs):
                importances.append({
                    "feature": col,
                    "importance": round(float(abs(coef)), 4),
                    "direction": 1 if coef >= 0 else -1,
                })
            # Add back constant variance columns as zero importance
            for col in feature_cols:
                if col not in valid_cols:
                    importances.append({"feature": col, "importance": 0.0, "direction": 1})
                    
            importances.sort(key=lambda x: x["importance"], reverse=True)
            return {"importances": importances, "method": "logistic_regression"}
            
    except ImportError:
        pass
        
    # Statistical fallback using Pearson correlation of each feature with the R-multiple
    importances = []
    for col in feature_cols:
        if X[col].nunique() <= 1:
            corr = 0.0
        else:
            # Simple numpy correlation
            corr = float(X[col].corr(y_reg))
            if pd.isna(corr):
                corr = 0.0
                
        importances.append({
            "feature": col,
            "importance": round(abs(corr), 4),
            "direction": 1 if corr >= 0 else -1,
        })
        
    importances.sort(key=lambda x: x["importance"], reverse=True)
    return {"importances": importances, "method": "statistical_correlation"}
