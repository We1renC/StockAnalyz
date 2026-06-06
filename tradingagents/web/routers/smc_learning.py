"""SMC-crypto learning endpoints (audit fix F1).

Extracted verbatim from app.py to start decomposing the 12k-line
monolith (S5). These 7 endpoints are fully self-contained — they depend
only on LedgerPaths + the learning.* package + smc_adaptive_store, with
no app-internal helpers (get_db / _crypto_api_client), so they extract
without any circular import.

Mounted via app.include_router(smc_learning.router).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from smc_quant import LedgerPaths

router = APIRouter()


@router.post("/api/smc-crypto/rotate-ledger")
def api_smc_crypto_rotate_ledger(keep_per_symbol: int = 500):
    """Audit fix G3: trim training ledger to a rolling window per symbol,
    gzip-archiving the overflow (automation of the manual Plan B trim)."""
    try:
        from learning.ledger_rotation import rotate_ledger
        return rotate_ledger(LedgerPaths.training_ledger(),
                              keep_per_symbol=int(keep_per_symbol))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rotate-ledger failed: {e}")


@router.get("/api/smc-crypto/ops-metrics")
def api_smc_crypto_ops_metrics():
    """Audit fix G2: single ops surface — autolearn scheduler state,
    ledger-cache hit rate, swallowed-error counts, ledger file sizes."""
    import os as _os
    out: dict = {}
    try:
        from learning.autolearn_scheduler import scheduler_state
        out["autolearn"] = scheduler_state()
    except Exception as e:
        out["autolearn"] = {"error": str(e)}
    try:
        from learning.ledger_cache import cache_stats
        out["ledger_cache"] = cache_stats()
    except Exception as e:
        out["ledger_cache"] = {"error": str(e)}
    try:
        from learning.obs_log import swallow_counts
        out["swallowed_errors"] = swallow_counts()
    except Exception as e:
        out["swallowed_errors"] = {"error": str(e)}
    try:
        sizes = {}
        for label, path in [
            ("training_ledger", LedgerPaths.training_ledger()),
            ("paper_journal", LedgerPaths.paper_journal()),
            ("paper_trades", LedgerPaths.paper_trades()),
        ]:
            try:
                sizes[label] = {
                    "bytes": _os.path.getsize(path),
                    "lines": sum(1 for _ in open(path, "rb")),
                } if _os.path.exists(path) else {"bytes": 0, "lines": 0}
            except Exception:
                sizes[label] = {"error": "stat_failed"}
        out["ledger_files"] = sizes
    except Exception as e:
        out["ledger_files"] = {"error": str(e)}
    return out


@router.post("/api/smc-crypto/decommission-sweep")
def api_smc_crypto_decommission_sweep(symbol: Optional[str] = None,
                                        window_size: int = 50,
                                        min_total_R: float = -5.0,
                                        revive_total_R: float = 1.0,
                                        cooldown_days: int = 7,
                                        commit: bool = True):
    """Audit fix D3: scan ledger, decommission per-(model, symbol, interval)
    when trailing window goes too far underwater; revive after cooldown
    once it recovers.

    POST so cron-friendly (idempotent if state already up-to-date).
    Set commit=False for a dry-run.
    """
    try:
        from learning.model_decommission import (
            compute_per_model_health, decide_decommission,
            load_state, save_state,
        )
        from learning.ledger_cache import cached_load_trade_records as load_trade_records
        import os as _os
        records = load_trade_records(LedgerPaths.training_ledger())
        if symbol:
            records = [r for r in records if r.get("symbol") == symbol]
        decom_path = _os.path.join(
            _os.path.dirname(LedgerPaths.training_ledger()),
            "decommissioned.json",
        )
        prev = load_state(decom_path)
        health = compute_per_model_health(records, window_size=int(window_size))
        out = decide_decommission(
            health, prev,
            min_total_R=float(min_total_R),
            revive_total_R=float(revive_total_R),
            cooldown_days=int(cooldown_days),
        )
        if commit and out.get("actions"):
            save_state(decom_path, out["new_state"])
        return {
            "actions": out["actions"],
            "n_models_active": sum(1 for v in out["new_state"].values()
                                     if v.get("status") == "active"),
            "n_models_decommissioned": sum(1 for v in out["new_state"].values()
                                              if v.get("status") == "decommissioned"),
            "committed": bool(commit and out.get("actions")),
            "state_path": decom_path,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"decommission-sweep failed: {e}")


@router.get("/api/smc-crypto/learning-health")
def api_smc_crypto_learning_health(symbol: Optional[str] = None,
                                     target_sample_size: int = 30):
    """Audit fix C1: aggregated 0-100 health score across all 4 panels."""
    try:
        from learning.learning_health import compute_learning_health
        from smc_quant import read_trade_ledger
        records = read_trade_ledger(LedgerPaths.training_ledger(), symbol=symbol)
        # Kill-switch state is in the adaptive sqlite; pull lazily.
        kill_state = "READY"
        try:
            from smc_adaptive_store import get_kill_switch_state, open_adaptive_db
            conn = open_adaptive_db()
            s = get_kill_switch_state(conn, symbol=symbol or "_global")
            kill_state = (s or {}).get("state") or "READY"
        except Exception:
            pass
        return compute_learning_health(
            records=records, kill_switch_state=kill_state,
            target_sample_size=int(target_sample_size),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"learning-health failed: {e}")


@router.get("/api/smc-crypto/cluster-ensemble")
def api_smc_crypto_cluster_ensemble(symbol: Optional[str] = None,
                                      min_samples: int = 10):
    """Audit fix P3-19: per-(model, symbol, interval, regime) ensemble.

    Returns each cluster's per-factor lift (E[R|active]−E[R|inactive])
    so the tuner can spot "this factor is great for BTC 1h trending
    but poison for ETH 15m ranging" — invisible to the global average.
    """
    try:
        from learning.cluster_ensemble import (
            build_cluster_weight_table, cluster_summary,
        )
        from smc_quant import CONFLUENCE_WEIGHTS_DEFAULT, read_trade_ledger
        records = read_trade_ledger(LedgerPaths.training_ledger(), symbol=symbol)
        factors = list(CONFLUENCE_WEIGHTS_DEFAULT.keys())
        table = build_cluster_weight_table(records, factors=factors,
                                              min_samples=int(min_samples))
        return {
            "n_clusters": len(table),
            "clusters": cluster_summary(table),
            "factors_tracked": factors,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cluster-ensemble failed: {e}")


@router.get("/api/smc-crypto/hyperparameter-sweep")
def api_smc_crypto_hyperparameter_sweep(symbol: Optional[str] = None,
                                          min_trades: int = 20,
                                          min_trades_per_fold: Optional[int] = None,
                                          fee_per_trade: float = 0.001):
    """Audit fix P3-18: monthly Bayesian-lite sweep of min_score/min_rr/risk_pct.

    Prefer purged walk-forward OOS sweep. Falls back to the legacy
    in-sample grid only when the ledger is too small to sustain
    expanding-window validation.
    """
    try:
        from learning.hyperparameter_sweep import (
            sweep_hyperparameters, sweep_walk_forward, should_apply_recommendation,
        )
        from smc_quant import read_trade_ledger
        records = read_trade_ledger(LedgerPaths.training_ledger(), symbol=symbol)
        wf_min_trades = (
            int(min_trades_per_fold)
            if min_trades_per_fold is not None
            else max(5, int(min_trades) // 2)
        )
        walk_forward = sweep_walk_forward(
            records,
            min_trades_per_fold=wf_min_trades,
            fee_per_trade=float(fee_per_trade),
        )
        fallback_in_sample = None
        mode = "walk_forward"
        sweep = walk_forward
        if walk_forward.get("status") != "ok":
            fallback_in_sample = sweep_hyperparameters(
                records,
                min_trades=int(min_trades),
                fee_per_trade=float(fee_per_trade),
            )
            sweep = fallback_in_sample
            mode = "in_sample_fallback"
        recommendation = should_apply_recommendation(sweep, current={})
        return {
            "mode": mode,
            "sweep": sweep,
            "walk_forward": walk_forward,
            "fallback_in_sample": fallback_in_sample,
            "recommendation": recommendation,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"hyperparameter-sweep failed: {e}")


@router.get("/api/smc-crypto/real-pnl-gates")
def api_smc_crypto_real_pnl_gates(symbol: Optional[str] = None,
                                    min_total_R: float = 0.5,
                                    min_correlation: float = 0.3,
                                    max_drawdown_R: float = 8.0):
    """Audit fix P3-17: hard gates from real ledger PnL (not synthetic scenarios).

    Returns three gates:
      • recent_30d_real_pnl       net R-multiple ≥ min_total_R
      • live_vs_backtest_correlation Pearson(bt, live) ≥ min_correlation
      • max_drawdown_30d           peak-to-trough DD ≤ max_drawdown_R
    """
    try:
        from learning.real_pnl_gates import run_real_pnl_gates
        from smc_quant import read_trade_ledger
        records = read_trade_ledger(LedgerPaths.training_ledger(), symbol=symbol)
        return run_real_pnl_gates(
            records,
            min_total_R=float(min_total_R),
            min_correlation=float(min_correlation),
            max_drawdown_R=float(max_drawdown_R),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"real-pnl-gates failed: {e}")

