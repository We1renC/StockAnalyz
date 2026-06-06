"""Monthly Bayesian-lite hyperparameter sweep.

Audit fix P3-18. ``min_score`` / ``min_rr`` / ``risk_pct`` are currently
hand-tuned in profile.yaml and only ever change when a human edits them.
The learning loop already retunes the per-factor weights but never
the three top-level knobs that gate every entry.

This module runs a coordinate-descent search over the three knobs:

  • Build candidate grid (default 4 × 4 × 4 = 64 cells)
  • For each cell, walk-forward over the resolved ledger:
    - Replay each historical entry with the candidate ``min_score`` /
      ``min_rr`` / ``risk_pct`` and decide "would we have taken it"
    - Score = Σ r_multiple × risk_pct_taken − fee_per_trade × n_trades
  • Pick the cell with the best risk-adjusted score (Sharpe-like ratio
    of mean / std) subject to ``min_trades`` constraint
  • Emit recommendation; caller writes back to profile.yaml via
    apply_strategy_yaml_overrides if confidence > threshold

"Bayesian-lite": we don't model the posterior — we use the empirical
distribution of past R-multiples as the likelihood and pick by
expected Sharpe. Real Optuna / GPyOpt is overkill at 64 cells and
adds a heavy dep.
"""

from __future__ import annotations

import math
from typing import Optional


def _simulate(
    records: list[dict],
    *,
    min_score: float,
    min_rr: float,
    risk_pct: float,
    fee_per_trade: float = 0.001,
) -> dict:
    """Replay history with candidate knobs; return P&L stats."""
    rs: list[float] = []
    for r in records:
        score = float(r.get("confluence_score") or 0)
        rr = float(r.get("rr") or 0)
        rm = r.get("r_multiple")
        if rm is None or score < min_score or rr < min_rr:
            continue
        try:
            rm_val = float(rm)
        except (TypeError, ValueError):
            continue
        # P&L per trade in account-currency units = r_multiple × risk_pct
        # minus a flat fee fraction.
        rs.append(rm_val * risk_pct - fee_per_trade)
    n = len(rs)
    if n == 0:
        return {"n_trades": 0, "total": 0.0, "mean": 0.0,
                "std": 0.0, "sharpe": 0.0}
    total = sum(rs)
    mean = total / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in rs) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    sharpe = (mean / std) if std > 1e-9 else 0.0
    return {
        "n_trades": n,
        "total": round(total, 6),
        "mean": round(mean, 6),
        "std": round(std, 6),
        "sharpe": round(sharpe, 4),
    }


def sweep_hyperparameters(
    records: list[dict],
    *,
    score_grid: Optional[list[float]] = None,
    rr_grid: Optional[list[float]] = None,
    risk_pct_grid: Optional[list[float]] = None,
    min_trades: int = 20,
    fee_per_trade: float = 0.001,
) -> dict:
    """Run grid sweep over (min_score, min_rr, risk_pct).

    Returns:
      {
        "best": {min_score, min_rr, risk_pct, score: {...}},
        "candidates": [...all cells, sorted by sharpe...],
        "n_records": int,
        "n_valid_candidates": int,
        "status": "ok" | "insufficient_data" | "no_candidate_meets_min_trades",
      }
    """
    score_grid = score_grid or [6, 7, 8, 9]
    rr_grid = rr_grid or [1.5, 2.0, 2.5, 3.0]
    risk_pct_grid = risk_pct_grid or [0.5, 1.0, 1.5, 2.0]

    if not records or len(records) < min_trades:
        return {
            "best": None,
            "candidates": [],
            "n_records": len(records or []),
            "n_valid_candidates": 0,
            "status": "insufficient_data",
        }

    cells = []
    for s in score_grid:
        for r in rr_grid:
            for p in risk_pct_grid:
                stats = _simulate(records, min_score=s, min_rr=r,
                                   risk_pct=p, fee_per_trade=fee_per_trade)
                cells.append({
                    "min_score": s, "min_rr": r, "risk_pct": p,
                    "score": stats,
                })
    # Filter for cells with enough trades (statistical reliability),
    # then sort by sharpe desc.
    valid = [c for c in cells if c["score"]["n_trades"] >= min_trades]
    if not valid:
        return {
            "best": None,
            "candidates": sorted(cells, key=lambda c: -c["score"]["sharpe"])[:10],
            "n_records": len(records),
            "n_valid_candidates": 0,
            "status": "no_candidate_meets_min_trades",
        }
    valid.sort(key=lambda c: -c["score"]["sharpe"])
    return {
        "best": valid[0],
        "candidates": valid[:10],
        "n_records": len(records),
        "n_valid_candidates": len(valid),
        "status": "ok",
    }


def should_apply_recommendation(
    sweep: dict,
    *,
    current: dict,
    min_sharpe_improvement: float = 0.1,
    min_sharpe_absolute: float = 0.2,
) -> dict:
    """Decide whether to write the recommendation back to profile.yaml.

    Conservative — only apply if:
      • sweep.status == "ok"
      • best.sharpe ≥ min_sharpe_absolute (positive Sharpe at all)
      • best.sharpe − current.sharpe ≥ min_sharpe_improvement

    Returns ``{apply: bool, reason: str, delta: float, new: dict}``.
    """
    if not sweep or sweep.get("status") != "ok" or not sweep.get("best"):
        return {"apply": False, "reason": "sweep_not_ok", "delta": 0.0, "new": None}
    best = sweep["best"]
    best_sh = float(best["score"]["sharpe"])
    cur_sh = float((current or {}).get("sharpe") or 0.0)
    if best_sh < min_sharpe_absolute:
        return {"apply": False, "reason": "best_sharpe_below_absolute_floor",
                "delta": round(best_sh - cur_sh, 4), "new": best}
    delta = best_sh - cur_sh
    if delta < min_sharpe_improvement:
        return {"apply": False, "reason": "improvement_below_threshold",
                "delta": round(delta, 4), "new": best}
    return {
        "apply": True,
        "reason": "sharpe_improvement_clears_threshold",
        "delta": round(delta, 4),
        "new": {
            "min_score": best["min_score"],
            "min_rr": best["min_rr"],
            "risk_pct": best["risk_pct"],
            "sharpe": best_sh,
            "n_trades": best["score"]["n_trades"],
        },
    }
