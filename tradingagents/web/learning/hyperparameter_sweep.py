"""Monthly Bayesian-lite hyperparameter sweep.

Audit fix P3-18. ``min_score`` / ``min_rr`` / ``risk_pct`` are currently
hand-tuned in profile.yaml and only ever change when a human edits them.
The learning loop already retunes the per-factor weights but never
the three top-level knobs that gate every entry.

This module ships two sweep modes:

  1. ``sweep_hyperparameters`` — legacy in-sample grid evaluation.
  2. ``sweep_walk_forward`` — purged expanding-window walk-forward
     that aggregates out-of-sample performance per candidate cell.

Callers that actually want to write recommendations back to live config
should prefer the walk-forward result, because it picks by aggregated
OOS Sharpe instead of one-pass in-sample fit.
"""

from __future__ import annotations

import math
from typing import Optional


def _rr_value(record: dict) -> float:
    """Support both legacy ``rr`` and canonical ``rr_planned`` fields."""
    for key in ("rr", "rr_planned"):
        try:
            value = record.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _selected_returns(
    records: list[dict],
    *,
    min_score: float,
    min_rr: float,
    risk_pct: float,
    fee_per_trade: float = 0.001,
) -> list[float]:
    """Return per-trade realized returns for rows that pass the candidate gate."""
    rs: list[float] = []
    for r in records:
        score = float(r.get("confluence_score") or 0)
        rr = _rr_value(r)
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
    return rs


def _stats_from_returns(rs: list[float]) -> dict:
    """Summarize a vector of per-trade returns with a Sharpe-like ratio."""
    n = len(rs)
    if n == 0:
        return {"n_trades": 0, "total": 0.0, "mean": 0.0, "std": 0.0, "sharpe": 0.0}
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


def _simulate(
    records: list[dict],
    *,
    min_score: float,
    min_rr: float,
    risk_pct: float,
    fee_per_trade: float = 0.001,
) -> dict:
    """Replay history with candidate knobs; return P&L stats."""
    rs = _selected_returns(
        records,
        min_score=min_score,
        min_rr=min_rr,
        risk_pct=risk_pct,
        fee_per_trade=fee_per_trade,
    )
    return _stats_from_returns(rs)


def _candidate_grid(
    *,
    score_grid: Optional[list[float]] = None,
    rr_grid: Optional[list[float]] = None,
    risk_pct_grid: Optional[list[float]] = None,
) -> list[tuple[float, float, float]]:
    score_grid = score_grid or [6, 7, 8, 9]
    rr_grid = rr_grid or [1.5, 2.0, 2.5, 3.0]
    risk_pct_grid = risk_pct_grid or [0.5, 1.0, 1.5, 2.0]
    return [
        (float(s), float(r), float(p))
        for s in score_grid
        for r in rr_grid
        for p in risk_pct_grid
    ]


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
    grid = _candidate_grid(
        score_grid=score_grid,
        rr_grid=rr_grid,
        risk_pct_grid=risk_pct_grid,
    )
    if not records or len(records) < min_trades:
        return {
            "best": None,
            "candidates": [],
            "n_records": len(records or []),
            "n_valid_candidates": 0,
            "status": "insufficient_data",
        }

    cells = []
    for s, r, p in grid:
        stats = _simulate(
            records,
            min_score=s,
            min_rr=r,
            risk_pct=p,
            fee_per_trade=fee_per_trade,
        )
        cells.append(
            {
                "min_score": s,
                "min_rr": r,
                "risk_pct": p,
                "score": stats,
            }
        )
    valid = [c for c in cells if c["score"]["n_trades"] >= min_trades]
    if not valid:
        return {
            "best": None,
            "candidates": sorted(cells, key=lambda c: -c["score"]["sharpe"])[:10],
            "n_records": len(records),
            "n_valid_candidates": 0,
            "status": "no_candidate_meets_min_trades",
        }
    valid.sort(
        key=lambda c: (
            -float(c["score"]["sharpe"]),
            -float(c["score"]["total"]),
            -int(c["score"]["n_trades"]),
        )
    )
    return {
        "best": valid[0],
        "candidates": valid[:10],
        "n_records": len(records),
        "n_valid_candidates": len(valid),
        "status": "ok",
    }


def _chronological_records(records: list[dict]) -> list[dict]:
    """Sort resolved records by entry_time (ISO string sort works for UTC)."""
    out = [
        r
        for r in (records or [])
        if r.get("outcome") not in (None, "pending")
        and r.get("r_multiple") is not None
    ]
    out.sort(key=lambda r: str(r.get("entry_time") or ""))
    return out


def sweep_walk_forward(
    records: list[dict],
    *,
    n_folds: int = 4,
    purge_size: int = 5,
    score_grid: Optional[list[float]] = None,
    rr_grid: Optional[list[float]] = None,
    risk_pct_grid: Optional[list[float]] = None,
    min_trades_per_fold: int = 10,
    fee_per_trade: float = 0.001,
) -> dict:
    """Purged expanding-window walk-forward over the ledger.

    Unlike ``sweep_hyperparameters``, this function does not choose by
    whole-ledger in-sample fit. It builds consecutive OOS folds and:

      1. runs an in-sample sweep on each fold's expanding train window
         so callers can inspect per-fold local winners;
      2. evaluates *every* candidate cell on every OOS fold;
      3. aggregates OOS trade returns per cell and ranks candidates by
         aggregate OOS Sharpe.

    The returned ``best`` cell is therefore a directly-actionable OOS
    recommendation compatible with ``should_apply_recommendation``.
    """
    chrono = _chronological_records(records)
    if n_folds <= 0:
        return {
            "n_folds": int(n_folds),
            "oos_sharpes": [],
            "mean_oos_sharpe": 0.0,
            "std_oos_sharpe": 0.0,
            "per_fold": [],
            "candidates": [],
            "best": None,
            "n_records": len(chrono),
            "status": "invalid_n_folds",
        }

    min_required_records = (n_folds + 1) * min_trades_per_fold
    if not chrono or len(chrono) < min_required_records:
        return {
            "n_folds": n_folds,
            "oos_sharpes": [],
            "mean_oos_sharpe": 0.0,
            "std_oos_sharpe": 0.0,
            "per_fold": [],
            "candidates": [],
            "best": None,
            "n_records": len(chrono),
            "status": "insufficient_data",
        }

    grid = _candidate_grid(
        score_grid=score_grid,
        rr_grid=rr_grid,
        risk_pct_grid=risk_pct_grid,
    )
    available_for_oos = len(chrono) - min_trades_per_fold
    fold_size = max(min_trades_per_fold, available_for_oos // n_folds)
    initial_train = len(chrono) - fold_size * n_folds
    if initial_train < min_trades_per_fold:
        return {
            "n_folds": n_folds,
            "oos_sharpes": [],
            "mean_oos_sharpe": 0.0,
            "std_oos_sharpe": 0.0,
            "per_fold": [],
            "candidates": [],
            "best": None,
            "n_records": len(chrono),
            "status": "insufficient_initial_train",
        }

    per_fold: list[dict] = []
    train_selected_oos_sharpes: list[float] = []
    aggregate: dict[tuple[float, float, float], dict] = {
        cell: {
            "min_score": cell[0],
            "min_rr": cell[1],
            "risk_pct": cell[2],
            "oos_returns": [],
            "fold_scores": [],
            "folds_with_trades": 0,
        }
        for cell in grid
    }

    for k in range(n_folds):
        oos_start = initial_train + k * fold_size
        oos_end = initial_train + (k + 1) * fold_size if k < n_folds - 1 else len(chrono)
        oos = chrono[oos_start:oos_end]
        train_end = max(min_trades_per_fold, oos_start - purge_size)
        train = chrono[:train_end]
        if len(train) < min_trades_per_fold or not oos:
            per_fold.append(
                {
                    "fold": k,
                    "train_n": len(train),
                    "oos_n": len(oos),
                    "picked": None,
                    "oos_sharpe": None,
                    "reason": "insufficient_train_or_oos",
                }
            )
            continue

        train_sweep = sweep_hyperparameters(
            train,
            score_grid=score_grid,
            rr_grid=rr_grid,
            risk_pct_grid=risk_pct_grid,
            min_trades=min_trades_per_fold,
            fee_per_trade=fee_per_trade,
        )
        if train_sweep.get("status") != "ok":
            per_fold.append(
                {
                    "fold": k,
                    "train_n": len(train),
                    "oos_n": len(oos),
                    "picked": None,
                    "oos_sharpe": None,
                    "reason": f"train_{train_sweep.get('status')}",
                }
            )
            continue

        picked = train_sweep["best"]
        picked_oos_score = _simulate(
            oos,
            min_score=picked["min_score"],
            min_rr=picked["min_rr"],
            risk_pct=picked["risk_pct"],
            fee_per_trade=fee_per_trade,
        )
        if picked_oos_score["n_trades"] >= 1:
            train_selected_oos_sharpes.append(float(picked_oos_score["sharpe"]))

        per_fold.append(
            {
                "fold": k,
                "train_n": len(train),
                "oos_n": len(oos),
                "picked": {
                    "min_score": picked["min_score"],
                    "min_rr": picked["min_rr"],
                    "risk_pct": picked["risk_pct"],
                },
                "oos_score": picked_oos_score,
                "oos_sharpe": picked_oos_score["sharpe"],
            }
        )

        for s, r, p in grid:
            oos_returns = _selected_returns(
                oos,
                min_score=s,
                min_rr=r,
                risk_pct=p,
                fee_per_trade=fee_per_trade,
            )
            fold_score = _stats_from_returns(oos_returns)
            cell = aggregate[(s, r, p)]
            cell["fold_scores"].append(
                {
                    "fold": k,
                    "train_n": len(train),
                    "oos_n": len(oos),
                    "score": fold_score,
                }
            )
            if oos_returns:
                cell["oos_returns"].extend(oos_returns)
                cell["folds_with_trades"] += 1

    if not train_selected_oos_sharpes:
        return {
            "n_folds": n_folds,
            "oos_sharpes": [],
            "mean_oos_sharpe": 0.0,
            "std_oos_sharpe": 0.0,
            "per_fold": per_fold,
            "candidates": [],
            "best": None,
            "n_records": len(chrono),
            "status": "no_fold_produced_oos_trades",
        }

    candidate_rows: list[dict] = []
    for s, r, p in grid:
        cell = aggregate[(s, r, p)]
        score = _stats_from_returns(cell["oos_returns"])
        if score["n_trades"] < min_trades_per_fold or cell["folds_with_trades"] == 0:
            continue
        candidate_rows.append(
            {
                "min_score": s,
                "min_rr": r,
                "risk_pct": p,
                "score": score,
                "walk_forward": {
                    "folds_with_trades": cell["folds_with_trades"],
                    "fold_scores": cell["fold_scores"],
                },
            }
        )

    mean_sh = sum(train_selected_oos_sharpes) / len(train_selected_oos_sharpes)
    std_sh = (
        math.sqrt(
            sum((x - mean_sh) ** 2 for x in train_selected_oos_sharpes)
            / (len(train_selected_oos_sharpes) - 1)
        )
        if len(train_selected_oos_sharpes) > 1
        else 0.0
    )

    if not candidate_rows:
        return {
            "n_folds": n_folds,
            "oos_sharpes": [round(x, 4) for x in train_selected_oos_sharpes],
            "mean_oos_sharpe": round(mean_sh, 4),
            "std_oos_sharpe": round(std_sh, 4),
            "per_fold": per_fold,
            "candidates": [],
            "best": None,
            "n_records": len(chrono),
            "status": "no_candidate_meets_min_oos_trades",
        }

    candidate_rows.sort(
        key=lambda c: (
            -float((c.get("score") or {}).get("sharpe") or 0.0),
            -float((c.get("score") or {}).get("total") or 0.0),
            -int((c.get("score") or {}).get("n_trades") or 0),
        )
    )

    return {
        "n_folds": n_folds,
        "oos_sharpes": [round(x, 4) for x in train_selected_oos_sharpes],
        "mean_oos_sharpe": round(mean_sh, 4),
        "std_oos_sharpe": round(std_sh, 4),
        "per_fold": per_fold,
        "candidates": candidate_rows[:10],
        "best": candidate_rows[0],
        "n_records": len(chrono),
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
        return {
            "apply": False,
            "reason": "best_sharpe_below_absolute_floor",
            "delta": round(best_sh - cur_sh, 4),
            "new": best,
        }
    delta = best_sh - cur_sh
    if delta < min_sharpe_improvement:
        return {
            "apply": False,
            "reason": "improvement_below_threshold",
            "delta": round(delta, 4),
            "new": best,
        }
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
