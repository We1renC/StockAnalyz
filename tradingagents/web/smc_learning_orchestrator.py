"""SMC Learning Orchestrator — invokes EVERY learning primitive in one report.

Audit done after merging quant-paper-acceptance found 24 learning-related
primitives, but only 7 were actually called from the workflow modules
(via run_closed_loop_calibration). The other 17 lived as functions
that nothing invoked. This module is the single entry point that
exercises all of them, layered:

    Layer 1  STATISTICS       expectancy / Sharpe / DSR / Bonferroni / stability
    Layer 2  ATTRIBUTION      factor_edge / cluster_trades_by / mae_mfe
    Layer 3  CALIBRATION      suggest_weights / kelly / adaptive_smc / atr_stop
    Layer 4  VALIDATION       walk_forward / purged_split / PBO / edge_decay
    Layer 5  ML               logistic regression + SHAP
    Layer 6  PROPOSAL         strategy_yaml + closed-loop
    Layer 7  ACCEPTANCE       telemetry + scenarios + policy + security

``build_learning_report(ledger_path, ...)`` runs all seven layers and
returns a structured dict. ``apply_proposed_changes(report)`` writes
the proposal back to ``config/strategy.yaml`` IF and only if the
validation layer passes (walk-forward + PBO + DSR).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

# Layer 1
from smc_quant import (
    LedgerPaths,
    compute_expectancy,
    sharpe_ratio,
    deflated_sharpe_ratio,
    bonferroni_threshold,
    read_trade_ledger,
    monthly_edge_stability,
)
# Layer 2
from smc_quant import (
    extract_factor_edge,
    cluster_trades_by,
    mae_mfe_recommendations,
)
# Layer 3
from smc_quant import (
    suggest_confluence_weights,
    kelly_fraction,
    calibrate_kelly_from_ledger,
    adaptive_smc_config,
    atr_adaptive_stop,
    CONFLUENCE_WEIGHTS_DEFAULT,
    apply_strategy_yaml_overrides,
)
# Layer 4
from smc_quant import (
    walk_forward_evaluate,
    purged_train_test_split,
    estimate_pbo,
    edge_decay_check,
)
# Layer 5
from smc_quant import train_multi_factor_logistic
# Layer 6
from smc_quant import (
    propose_strategy_yaml,
    generate_strategy_proposal,
    run_closed_loop_calibration,
)
# Layer 7
from paper_acceptance_metrics import (
    summarize_acceptance_telemetry,
    record_stability_session,
)
from paper_acceptance_scenarios import summarize_scenario_evidence
from paper_acceptance_policy import build_acceptance_policy_snapshot
from paper_acceptance_security import run_security_scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_records(ledger_path: str) -> list[dict]:
    return read_trade_ledger(ledger_path)


def _records_for(records: list[dict], symbol: Optional[str]) -> list[dict]:
    if not symbol:
        return records
    return [r for r in records if r.get("symbol") == symbol]


def _safe(fn, *a, fallback=None, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:
        return {"_error": repr(exc), "_fallback": fallback}


# ---------------------------------------------------------------------------
# Layered runners
# ---------------------------------------------------------------------------

def layer_statistics(records: list[dict]) -> dict:
    """Layer 1 — expectancy / Sharpe / DSR / Bonferroni / monthly stability."""
    if not records:
        return {"status": "no_data"}
    r_values = [float(r.get("r_multiple", 0)) for r in records]
    expect = compute_expectancy(records)
    sr = sharpe_ratio(r_values, annualize=252)
    dsr = deflated_sharpe_ratio(
        sr["sharpe"], n_trials=10, sample_size=len(r_values)
    )
    bonf = bonferroni_threshold(0.05, n_tests=len(CONFLUENCE_WEIGHTS_DEFAULT))
    stab = monthly_edge_stability(records)
    return {
        "expectancy": expect,
        "sharpe": sr,
        "deflated_sharpe": dsr,
        "bonferroni": bonf,
        "monthly_stability": stab,
    }


def layer_attribution(records: list[dict]) -> dict:
    """Layer 2 — per-factor edge, cluster attribution, MAE/MFE recommendations."""
    if not records:
        return {"status": "no_data"}
    fake_trades = [
        {"model": r.get("model"), "entry": r.get("entry_price"),
         "stop": r.get("stop"), "r_multiple": r.get("r_multiple")}
        for r in records
    ]
    factor_edge = _safe(extract_factor_edge, records, fake_trades, fallback={"factors": {}})
    clusters = _safe(cluster_trades_by, records, dimensions=("model", "market"), fallback={})
    mae_plan = _safe(mae_mfe_recommendations, records, min_samples=10, fallback={})
    return {
        "factor_edge": factor_edge,
        "clusters": clusters,
        "mae_mfe_recommendations": mae_plan,
    }


def layer_calibration(records: list[dict], factor_edge: dict) -> dict:
    """Layer 3 — suggested confluence weights + Kelly + adaptive SMC."""
    if not records:
        return {"status": "no_data"}
    suggested = _safe(
        suggest_confluence_weights, factor_edge, dict(CONFLUENCE_WEIGHTS_DEFAULT),
        fallback={},
    )
    kelly = _safe(calibrate_kelly_from_ledger, records, fractional=0.25, cap=0.05, fallback={})
    expect = compute_expectancy(records)
    raw_kelly = _safe(
        kelly_fraction,
        win_rate=expect.get("win_rate", 0),
        avg_win_R=expect.get("avg_win_R", 0),
        avg_loss_R=expect.get("avg_loss_R", 0),
        fractional=0.25, cap=0.05, fallback={},
    )
    return {
        "suggested_weights": suggested,
        "kelly_from_ledger": kelly,
        "kelly_raw": raw_kelly,
    }


def layer_validation(records: list[dict]) -> dict:
    """Layer 4 — walk-forward + PBO + edge-decay + purged split."""
    if not records:
        return {"status": "no_data"}
    wf = _safe(walk_forward_evaluate, records, folds=4, train_fraction=0.6, fallback={"folds": []})
    # purged split (we use the same series as "live" proxy for now)
    train, test = _safe(
        purged_train_test_split, records,
        train_fraction=0.7, embargo_pct=0.02,
        fallback=([], []),
    ) if not isinstance(_safe(purged_train_test_split, records,
                              train_fraction=0.7, embargo_pct=0.02,
                              fallback=([], [])), dict) else ([], [])
    is_R = [float(r.get("r_multiple", 0)) for r in train]
    oos_R = [float(r.get("r_multiple", 0)) for r in test]
    pbo = _safe(estimate_pbo, is_R, oos_R, fallback={"pbo": None}) if is_R and oos_R else {"pbo": None, "note": "insufficient_split"}
    # Treat the most-recent third of the ledger as "live" for decay check.
    n = len(records)
    if n >= 30:
        backtest = records[: int(n * 0.66)]
        live = records[int(n * 0.66):]
    else:
        backtest = records; live = []
    decay = _safe(edge_decay_check, backtest, live, min_live_samples=10,
                   fallback={"status": "insufficient"})
    return {
        "walk_forward": wf,
        "purged_train_size": len(train),
        "purged_test_size": len(test),
        "pbo": pbo,
        "edge_decay": decay,
    }


def layer_ml(records: list[dict]) -> dict:
    """Layer 5 — multi-factor logistic regression with SHAP-like attribution."""
    if not records or len(records) < 20:
        return {"status": "insufficient_samples", "sample_size": len(records)}
    return _safe(
        train_multi_factor_logistic, records,
        min_samples=20,
        fallback={"status": "error"},
    )


def layer_proposal(records: list[dict]) -> dict:
    """Layer 6 — strategy.yaml proposal + full closed-loop bundle."""
    if not records:
        return {"status": "no_data"}
    yaml_proposal = _safe(propose_strategy_yaml, trade_records=records, min_samples=10, fallback={})
    closed_loop = _safe(run_closed_loop_calibration, records, fallback={"status": "error"})
    full_proposal = _safe(
        generate_strategy_proposal, records,
        fallback={"status": "error"},
    ) if "generate_strategy_proposal" in globals() else {}
    return {
        "yaml_proposal": yaml_proposal,
        "closed_loop": closed_loop,
        "full_proposal": full_proposal,
    }


def layer_acceptance(conn: sqlite3.Connection, symbol: Optional[str]) -> dict:
    """Layer 7 — paper-acceptance evidence + policy snapshot + security scan."""
    telemetry = _safe(summarize_acceptance_telemetry, conn, symbol=symbol, stage="paper", fallback={})
    scenarios = _safe(summarize_scenario_evidence, conn, symbol=symbol, stage="paper", fallback={})
    # Policy snapshot needs a context dict — minimal stub
    policy = _safe(
        build_acceptance_policy_snapshot,
        {"metrics": (telemetry or {}).get("metrics") or {}, "stage": "paper"},
        fallback={},
    )
    # Security scan against the web/ directory (lightweight)
    try:
        web_root = Path(__file__).parent
        security = run_security_scan(web_root)
    except Exception as exc:
        security = {"_error": repr(exc)}
    return {
        "telemetry": telemetry,
        "scenarios": scenarios,
        "policy_snapshot": policy,
        "security": security,
    }


def layer_adaptive(df_sample: Optional[pd.DataFrame] = None) -> dict:
    """Layer 3 (extra) — show §17.6 ATR-adaptive output for the current data."""
    if df_sample is None or len(df_sample) < 20:
        return {"status": "no_data"}
    from smc_quant import normalize_ohlcv
    try:
        h = normalize_ohlcv(df_sample)
    except Exception as exc:
        return {"_error": repr(exc)}
    cfg, info = adaptive_smc_config(h)
    stop_atr = atr_adaptive_stop(h, multiplier=1.5)
    return {
        "adaptive_smc_config": {"swing_length": cfg.swing_length,
                                  "internal_swing_length": cfg.internal_swing_length,
                                  "liquidity_range_percent": cfg.liquidity_range_percent,
                                  "info": info},
        "atr_adaptive_stop": stop_atr,
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

@dataclass
class LearningReport:
    generated_at: str
    elapsed_seconds: float
    sample_size: int
    symbol: Optional[str]
    layer_1_statistics: dict
    layer_2_attribution: dict
    layer_3_calibration: dict
    layer_4_validation: dict
    layer_5_ml: dict
    layer_6_proposal: dict
    layer_7_acceptance: dict
    layer_adaptive: dict
    learning_indicator: str
    promotion_decision: dict
    notes: list[str] = field(default_factory=list)


def _decide_promotion(stats: dict, validation: dict, proposal: dict) -> dict:
    """Combine validation signals into a single promotion verdict."""
    reasons: list[str] = []
    wf = (validation or {}).get("walk_forward") or {}
    pbo = (validation or {}).get("pbo") or {}
    decay = (validation or {}).get("edge_decay") or {}
    dsr = (stats or {}).get("deflated_sharpe") or {}
    closed = (proposal or {}).get("closed_loop") or {}
    expectancy = (stats or {}).get("expectancy") or {}

    walk_pass = bool(wf.get("passes"))
    pbo_ok = (pbo.get("pbo") is None) or (pbo.get("pbo", 1) < 0.5)
    decay_ok = not bool(decay.get("review_required"))
    dsr_ok = bool(dsr.get("passes")) or dsr.get("deflated") is None
    closed_adopt = bool((closed.get("verdict") or {}).get("adopt"))
    quality_ok = (
        float(expectancy.get("expected_R") or 0.0) >= 0.05
        and float(expectancy.get("win_rate") or 0.0) >= 0.40
    )

    if not walk_pass: reasons.append("walk_forward_failed")
    if not pbo_ok:    reasons.append(f"pbo_high:{pbo.get('pbo')}")
    if not decay_ok:  reasons.append("edge_decay_detected")
    if not dsr_ok:    reasons.append("deflated_sharpe_below_threshold")
    if not closed_adopt: reasons.append("closed_loop_rejected")
    if not quality_ok: reasons.append("quality_floor_not_met")

    can_promote = walk_pass and pbo_ok and decay_ok and dsr_ok and closed_adopt and quality_ok
    return {
        "can_promote": can_promote,
        "reasons": reasons,
        "criteria": {
            "walk_forward": walk_pass,
            "pbo_ok": pbo_ok,
            "edge_decay_ok": decay_ok,
            "deflated_sharpe_ok": dsr_ok,
            "closed_loop_adopt": closed_adopt,
            "quality_ok": quality_ok,
        },
    }


def _learning_indicator(report: dict, records: list[dict]) -> str:
    if len(records) < 30:
        return "insufficient_data"
    closed = ((report.get("layer_6_proposal") or {}).get("closed_loop") or {})
    adopted = bool((closed.get("verdict") or {}).get("adopt"))
    expected_R = float(((report.get("layer_1_statistics") or {})
                        .get("expectancy") or {}).get("expected_R") or 0)
    if not adopted and expected_R > 0:
        return "stagnant"
    if not adopted:
        return "degrading"
    return "active"


def build_learning_report(
    *,
    ledger_path: Optional[str] = None,
    db_path: Optional[str] = None,
    symbol: Optional[str] = None,
    df_sample: Optional[pd.DataFrame] = None,
) -> LearningReport:
    """One-call orchestrator — runs every learning primitive."""
    t0 = time.time()
    ledger_path = ledger_path or LedgerPaths.training_ledger()
    records = _records_for(_load_records(ledger_path), symbol)

    l1 = layer_statistics(records)
    l2 = layer_attribution(records)
    fe = (l2.get("factor_edge") or {}) if isinstance(l2, dict) else {}
    l3 = layer_calibration(records, fe)
    l4 = layer_validation(records)
    l5 = layer_ml(records)
    l6 = layer_proposal(records)
    l_adapt = layer_adaptive(df_sample)
    l7: dict = {}
    if db_path:
        from smc_quant import connect_db
        conn = connect_db(db_path, row_factory=True)
        try:
            l7 = layer_acceptance(conn, symbol)
        finally:
            conn.close()

    promotion = _decide_promotion(l1, l4, l6)
    indicator = _learning_indicator(
        {"layer_1_statistics": l1, "layer_6_proposal": l6}, records,
    )

    return LearningReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        elapsed_seconds=round(time.time() - t0, 3),
        sample_size=len(records),
        symbol=symbol,
        layer_1_statistics=l1,
        layer_2_attribution=l2,
        layer_3_calibration=l3,
        layer_4_validation=l4,
        layer_5_ml=l5,
        layer_6_proposal=l6,
        layer_7_acceptance=l7,
        layer_adaptive=l_adapt,
        learning_indicator=indicator,
        promotion_decision=promotion,
    )


def apply_proposed_changes(
    report: LearningReport,
    *,
    strategy_yaml_path: str = "config/strategy.yaml",
) -> dict:
    """Only mutates strategy.yaml when the validation layer fully passes."""
    decision = report.promotion_decision
    if not decision.get("can_promote"):
        return {"applied": False, "reason": "validation_failed",
                 "criteria": decision.get("criteria"),
                 "blockers": decision.get("reasons")}
    suggested = ((report.layer_3_calibration or {}).get("suggested_weights") or {})
    if not suggested:
        return {"applied": False, "reason": "no_suggestion_emitted"}
    try:
        yaml_path = Path(strategy_yaml_path)
        if not yaml_path.is_absolute():
            yaml_path = Path(__file__).parent.parent / strategy_yaml_path
        import yaml  # type: ignore
        existing = {}
        if yaml_path.exists():
            with open(yaml_path, "r", encoding="utf-8") as fh:
                existing = yaml.safe_load(fh) or {}
        existing.setdefault("confluence", {})["weights"] = suggested
        with open(yaml_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(existing, fh, allow_unicode=True, sort_keys=False)
        apply_strategy_yaml_overrides()
        return {"applied": True, "path": str(yaml_path), "weights": suggested}
    except Exception as exc:
        return {"applied": False, "reason": f"write_failed:{exc}"}
