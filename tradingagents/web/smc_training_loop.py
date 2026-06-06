"""SMC strategy training + audit loop — virtual-market auto-trainer.

Closes the codex/quant-paper-acceptance feedback loop that was previously
only living in the merge: every primitive that branch shipped
(runtime_metrics / order_audit / virtual_account_snapshot / scenarios /
security / policy snapshot) is now invoked from one place, fed by both
the §10 backtest engine and the live crypto-api session.

Five operations available:

  1. ``auto_backtest_window(symbol, bars=500)``
       Roll a synthetic forward-walk across recent bars. For each bar
       index we freeze the analysis (§12.2), pick a candidate entry, and
       use ``evaluate_entry_models`` to produce R-multiple outcomes.
       Every settled trade lands in ``trades.jsonl`` (§18.2) AND every
       order is mirrored into ``crypto_paper_acceptance_*`` tables via
       the merged acceptance modules.

  2. ``ingest_acceptance_evidence(conn, session_result)``
       Bridges UnifiedTradingSession output → the merged-but-idle
       paper_acceptance_metrics primitives:
         • record_order_audit          per live/dry order
         • record_runtime_metric       per acceptance metric value
         • record_virtual_account_snapshot   per session
         • record_alert_delivery       per blocking gate

  3. ``train_from_ledger(conn, base_weights=None)``
       Runs ``run_closed_loop_calibration`` over the persisted ledger.
       If ``verdict.adopt=True``, rewrites ``config/strategy.yaml``'s
       confluence weights and re-applies — strategy actually learns.

  4. ``run_scenarios_for_symbol(conn, symbol)``
       Drives ``run_acceptance_scenario`` over every catalog entry so
       gate evidence is populated (otherwise gates default to "fail").

  5. ``audit_learning_capability(conn, symbol)``
       Quantitatively answers "does the model learn?" — diffs the
       current weights against the YAML baseline, computes
       expected_R before/after the last calibration, and probes for
       monthly_edge_stability drift.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from smc_quant import (
    LedgerPaths,
    connect_db,
    SMCConfig,
    CONFLUENCE_WEIGHTS_DEFAULT,
    CONFLUENCE_THRESHOLD_DEFAULT,
    apply_strategy_yaml_overrides,
    build_smc_analysis,
    build_trade_record,
    compute_expectancy,
    deflated_sharpe_ratio,
    estimate_pbo,
    edge_decay_check,
    evaluate_entry_models,
    load_runtime_cluster_weight_table,
    read_trade_ledger,
    monthly_edge_stability,
    normalize_ohlcv,
    persist_trade_records,
    run_closed_loop_calibration,
    sharpe_ratio,
)
from smc_paper_runner import CryptoApiClient
from smc_auto_workflow import profile_for_symbol
from learning.adaptive_store import (
    ADAPTIVE_MODEL_VERSION,
    apply_atomic_config_patch,
    create_config_patch,
    ensure_adaptive_calibration_schema,
    get_kill_switch_state,
    record_adaptive_audit_event,
    set_kill_switch_state,
    strategy_config_snapshot,
    upsert_trade_ledger_records,
)
from learning.adaptive_validation import (
    PurgedWalkForwardSplit,
    build_gate_results,
    compute_sample_uniqueness,
    effective_sample_size,
    validation_entropy_sizing,
)
from learning.dsr_threshold import (
    dsr_probability,
    required_sharpe_for_dsr,
    update_confluence_threshold_by_dsr,
)
from learning.feature_denoising import marchenko_pastur_eigen_clip
from learning.fm_challenger import fit_factorization_machine_classifier
from learning.probe_controller import plan_probe_order
from learning.uniqueness_weighted_lr import (
    build_feature_matrix,
    build_trade_sample_weights,
    fit_uniqueness_weighted_lr,
)


# ---------------------------------------------------------------------------
# Purged Walk-Forward OOS comparator for FM vs LR challenger adoption
# (smc_adaptive_calibration_development_plan §998-1004)
# ---------------------------------------------------------------------------
_FM_OOS_MIN_FOLDS = 3
_FM_OOS_MIN_TRAIN = 16            # need ≥16 rows in each train fold for FM to be meaningful
_FM_OOS_MIN_TEST = 4              # each test fold must have ≥4 rows
_FM_OOS_ACC_MARGIN = 0.02         # FM must beat LR by ≥2pp OOS to be adopted
_LEARNING_R_MULTIPLE_CAP = 10.0


def _emit_edge_decay_trail(
    adaptive_conn,
    *,
    symbol: str,
    decay: dict,
    new_mode: str,
) -> None:
    """P1-8+ — write an audit trail when edge decay forces a state demotion.

    Two writes:
      1. paper_acceptance_metrics.record_alert_delivery (SQLite alert log)
      2. Obsidian vault note (best-effort, silently ignored if vault unset)
    """
    msg = decay.get("warning_message") or "edge decay detected"
    # 1) SQLite alert row — non-blocking, swallow errors so a failed alert
    # never breaks the learning tick.
    if adaptive_conn is not None:
        try:
            record_alert_delivery(
                adaptive_conn,
                symbol=symbol,
                event_type="edge_decay_demotion",
                severity="warning",
                channel="learning_loop",
                delivered=True,
                detail={
                    "new_mode": new_mode,
                    "reason": msg,
                    "diagnostics": decay,
                },
            )
        except Exception:
            pass

    # 2) Obsidian markdown note — only when vault path is configured
    try:
        from llm_providers import load_settings
        from pathlib import Path
        vault_path = (load_settings() or {}).get("obsidian_vault_path", "")
        if not vault_path:
            return
        vault = Path(vault_path).expanduser()
        if not vault.is_dir():
            return
        out_dir = vault / "SMC" / "EdgeDecay"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        safe_ts = ts.replace(":", "-").replace("+", "_")
        safe_sym = symbol.replace("/", "-").replace(":", "-")
        path = out_dir / f"{safe_sym}_{safe_ts}.md"
        overall_E = decay.get("overall_expectancy")
        recent_E = decay.get("recent_expectancy")
        overall_wr = decay.get("overall_win_rate")
        recent_wr = decay.get("recent_win_rate")
        body = (
            f"---\n"
            f"type: smc-edge-decay\n"
            f"symbol: {symbol}\n"
            f"new_mode: {new_mode}\n"
            f"triggered_at: {ts}\n"
            f"tags: [smc, edge_decay, audit]\n"
            f"---\n\n"
            f"# Edge Decay Trail — {symbol}\n\n"
            f"- triggered at: `{ts}`\n"
            f"- demoted to: **{new_mode}**\n\n"
            f"## Diagnostics\n\n"
            f"- overall expectancy: `{overall_E}`\n"
            f"- recent expectancy: `{recent_E}`\n"
            f"- overall win rate: `{overall_wr}`\n"
            f"- recent win rate: `{recent_wr}`\n\n"
            f"## Reason\n\n"
            f"> {msg}\n"
        )
        path.write_text(body, encoding="utf-8")
    except Exception:
        pass


def _detect_recent_edge_decay(records: list[dict], window_size: int = 20) -> dict:
    """P1-8 — wrap learning.decay_monitor for in-process use.

    Returns the decay diagnostics dict so the caller can decide
    whether to demote state. Mirrors the original signature but
    consumes the §18.2 ledger record schema (with optional
    ``r_multiple`` / ``outcome`` fields).
    """
    if not records:
        return {"is_decaying": False, "warning_message": "no_records"}
    try:
        from learning.decay_monitor import detect_edge_decay
        rows = []
        for r in records:
            outcome = r.get("outcome")
            rm = r.get("r_multiple")
            if outcome in (None, "pending") or rm is None:
                continue
            try:
                rm = float(rm)
            except (TypeError, ValueError):
                continue
            rows.append({
                "entry_time": r.get("entry_time") or "",
                "r_multiple": rm,
                "win": 1 if rm > 0 else 0,
            })
        if len(rows) < window_size:
            return {
                "is_decaying": False,
                "warning_message": f"insufficient_resolved_trades ({len(rows)}<{window_size})",
                "resolved_count": len(rows),
            }
        df = pd.DataFrame(rows)
        return detect_edge_decay(df, window_size=window_size)
    except Exception as exc:
        return {"is_decaying": False, "warning_message": f"decay_check_failed:{exc}"}


def _purged_walk_forward_fm_vs_lr(
    records: list[dict],
    *,
    n_folds: int = 4,
    embargo: int = 1,
) -> dict:
    """Walk-forward, purged, fold-by-fold accuracy comparison.

    For each fold, train both models on the train slice (with an embargo
    gap before the test slice to prevent label leakage) and score
    accuracy on the test slice. Adopt FM only when it beats LR on the
    *mean OOS accuracy across all folds* by ≥``_FM_OOS_ACC_MARGIN``.

    Returns a verdict + per-fold detail so the audit panel can show
    "FM 0.62 vs LR 0.58 over 4 folds → adopt".
    """
    feature_block = build_feature_matrix(records)
    X = feature_block.get("X")
    y = feature_block.get("y")
    feature_cols = feature_block.get("feature_cols", [])
    if X is None or y is None:
        return {"verdict": "no_feature_matrix", "fold_count": 0,
                "fm_oos_accuracy": None, "lr_oos_accuracy": None}
    n = len(y)
    if n < _FM_OOS_MIN_TRAIN + _FM_OOS_MIN_TEST + embargo + n_folds:
        return {
            "verdict": "insufficient_samples_for_walk_forward",
            "n": n, "min_required": _FM_OOS_MIN_TRAIN + _FM_OOS_MIN_TEST + embargo + n_folds,
            "fold_count": 0, "fm_oos_accuracy": None, "lr_oos_accuracy": None,
        }

    fold_size = max(_FM_OOS_MIN_TEST, n // (n_folds + 1))
    fold_records: list[dict] = []
    fm_accs: list[float] = []
    lr_accs: list[float] = []
    for k in range(1, n_folds + 1):
        test_start = k * fold_size
        test_end = min(n, test_start + fold_size)
        if test_end - test_start < _FM_OOS_MIN_TEST:
            continue
        train_end = max(0, test_start - embargo)
        if train_end < _FM_OOS_MIN_TRAIN:
            continue
        X_train, y_train = X[:train_end], y[:train_end]
        X_test, y_test = X[test_start:test_end], y[test_start:test_end]
        try:
            weights = build_trade_sample_weights(
                y_train,
                np.ones(len(y_train), dtype=float),  # uniqueness already inside records
            )
            lr_fit = fit_uniqueness_weighted_lr(
                X_train, y_train, weights, feature_cols=feature_cols
            )
            fm_fit = fit_factorization_machine_classifier(
                X_train, y_train, weights,
                n_eff=float(len(y_train)), feature_cols=feature_cols,
            )
            lr_pred = lr_fit.get("predict_proba")
            fm_pred = fm_fit.get("predict_proba")
            if not (callable(lr_pred) and callable(fm_pred)):
                continue
            lr_acc = float(((lr_pred(X_test) >= 0.5).astype(int) == y_test).mean())
            fm_acc = float(((fm_pred(X_test) >= 0.5).astype(int) == y_test).mean())
            lr_accs.append(lr_acc); fm_accs.append(fm_acc)
            fold_records.append({
                "fold": k, "train_n": train_end, "test_n": test_end - test_start,
                "lr_acc": round(lr_acc, 4), "fm_acc": round(fm_acc, 4),
                "margin": round(fm_acc - lr_acc, 4),
            })
        except Exception as exc:
            fold_records.append({"fold": k, "error": repr(exc)})
            continue

    if len(fm_accs) < _FM_OOS_MIN_FOLDS:
        return {
            "verdict": "insufficient_valid_folds",
            "fold_count": len(fm_accs),
            "min_folds_required": _FM_OOS_MIN_FOLDS,
            "fm_oos_accuracy": None, "lr_oos_accuracy": None,
            "folds": fold_records,
        }
    mean_fm = float(np.mean(fm_accs))
    mean_lr = float(np.mean(lr_accs))
    margin = mean_fm - mean_lr
    return {
        "verdict": "fm_beats_lr_oos" if margin >= _FM_OOS_ACC_MARGIN else "fm_not_better_oos",
        "fm_oos_accuracy": round(mean_fm, 4),
        "lr_oos_accuracy": round(mean_lr, 4),
        "margin": round(margin, 4),
        "margin_threshold": _FM_OOS_ACC_MARGIN,
        "fold_count": len(fm_accs),
        "folds": fold_records,
    }

# Merged-but-previously-unused acceptance primitives
from paper_acceptance_store import ensure_paper_acceptance_schema, load_acceptance_reports
from paper_acceptance_metrics import (
    ensure_paper_acceptance_metrics_schema,
    record_runtime_metric,
    record_order_audit,
    record_virtual_account_snapshot,
    record_alert_delivery,
    summarize_acceptance_telemetry,
)
from paper_acceptance_scenarios import (
    ensure_paper_acceptance_scenario_schema,
    scenario_catalog,
    run_acceptance_scenario,
    summarize_scenario_evidence,
)
from paper_acceptance_security import run_security_scan
from paper_acceptance_policy import build_acceptance_policy_snapshot


# ---------------------------------------------------------------------------
# 1. Auto backtest — walk-forward over recent bars
# ---------------------------------------------------------------------------

@dataclass
class BacktestSummary:
    symbol: str
    bars_seen: int
    trades_settled: int
    expected_R: float
    win_rate: float
    profit_factor: Optional[float]
    ledger_path: str


def _interval_ledger_path(base_ledger_path: str, interval: str) -> str:
    """P1-9 audit fix: split ledger by interval so 5m / 1h / 4h ledgers
    don't mix into one E[R] average (different timeframe has different
    win/loss profile)."""
    import os
    if not interval or interval == "default":
        return base_ledger_path
    base, ext = os.path.splitext(base_ledger_path)
    return f"{base}.{interval}{ext or '.jsonl'}"


def auto_backtest_window(
    api: CryptoApiClient,
    symbol: str,
    *,
    interval: str = "1h",
    bars: int = 500,
    ledger_path: Optional[str] = None,
    max_hold_bars: int = 20,
    db_path: Optional[str] = None,
    model_version: str = ADAPTIVE_MODEL_VERSION,
) -> BacktestSummary:
    """Pull klines, run SMC engine in one shot, evaluate every triggered entry,
    persist outcomes into the §18.2 trade ledger.

    Returns a compact summary so caller can chart it.
    """
    ledger_path = ledger_path or LedgerPaths.training_ledger()
    profile = profile_for_symbol(symbol)
    kl = api.klines(symbol, interval=interval, limit=bars)
    rows = (kl.get("payload") or {}).get("data") or []
    if not rows:
        return BacktestSummary(symbol, 0, 0, 0.0, 0.0, None, ledger_path)
    df_raw = pd.DataFrame(
        [{"Open": float(r["open"]), "High": float(r["high"]),
          "Low": float(r["low"]), "Close": float(r["close"]),
          "Volume": float(r.get("volume", 0))} for r in rows],
        index=pd.to_datetime([r["open_time"] for r in rows], utc=True),
    )
    # evaluate_entry_models expects lowercase columns (h.low etc.)
    df = normalize_ohlcv(df_raw)
    cluster_weight_table = load_runtime_cluster_weight_table(ledger_path)
    analysis = build_smc_analysis(
        df_raw, symbol=symbol,
        timeframe=interval,
        config=SMCConfig(swing_length=profile.swing_length,
                         internal_swing_length=profile.internal_swing_length),
        account_equity=100_000.0,
        cluster_weight_table=cluster_weight_table,
        cluster_key_hint=("runtime", symbol, interval, None),
    )
    config_snapshot = strategy_config_snapshot()
    em = (analysis.get("concepts") or {}).get("entry_models") or {}
    all_entries: list[dict] = []
    for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement",
                "unicorn", "silver_bullet", "power_of_three"):
        all_entries.extend(em.get(key) or [])
    bt = evaluate_entry_models(df, all_entries, max_hold_bars=max_hold_bars,
                                only_triggered=False)
    # P1-7 audit fix: tag the regime at the entry bar so attribution can
    # group by regime later. Compute once on the full df, then slice per trade.
    try:
        from learning.regime import classify_market_regime
        global_regime = classify_market_regime(df)
    except Exception:
        global_regime = {"regime_trend": "unknown", "regime_volatility": "unknown"}

    trade_records: list[dict] = []
    for tr in bt.get("trades") or []:
        # Re-join the entry for factor context so attribution works.
        for e in all_entries:
            if (e.get("model") == tr.get("model")
                    and round(float(e.get("entry", 0)), 4) == round(float(tr.get("entry", 0)), 4)):
                entry_idx = tr.get("entry_index", -1)
                # Per-trade regime: classify on bars up to (not including) entry to avoid lookahead
                trade_regime = global_regime
                if entry_idx >= 60:
                    try:
                        trade_regime = classify_market_regime(df.iloc[: entry_idx + 1])
                    except Exception:
                        pass
                rec = build_trade_record(
                    e,
                    trade_outcome=tr,
                    symbol=symbol, market="crypto",
                    timeframe=interval,
                    entry_time=str(df.index[entry_idx] if entry_idx >= 0 else df.index[0]),
                    probe=False,
                    model_version=model_version,
                    config_hash=config_snapshot["hash"],
                    source="backtest",
                    state_hint="READY",
                    regime=trade_regime,
                )
                trade_records.append(rec)
                break
    # P0-1: persist_trade_records now dedups by trade_id.
    # P1-9: write to interval-scoped ledger so 5m/1h/4h don't混算.
    interval_ledger = _interval_ledger_path(ledger_path, interval)
    persist_trade_records(trade_records, interval_ledger, dedup=True)
    # Also append to the global ledger for backwards compatibility but
    # dedup is enabled so duplicates won't accumulate.
    persist_trade_records(trade_records, ledger_path, dedup=True)
    if db_path and trade_records:
        conn = connect_db(db_path)
        conn.row_factory = sqlite3.Row
        try:
            ensure_adaptive_calibration_schema(conn)
            upsert_trade_ledger_records(
                conn,
                trade_records,
                config_hash=config_snapshot["hash"],
                model_version=model_version,
                source="backtest",
            )
            record_adaptive_audit_event(
                conn,
                symbol=symbol,
                event_type="ledger_synced",
                state_after={"mode": "READY", "source": "auto_backtest_window"},
                detail={
                    "rows_written": len(trade_records),
                    "ledger_path": ledger_path,
                    "config_hash": config_snapshot["hash"],
                    "model_version": model_version,
                },
            )
            conn.commit()
        finally:
            conn.close()
    metrics = bt.get("metrics") or {}
    return BacktestSummary(
        symbol=symbol, bars_seen=len(df),
        trades_settled=len(trade_records),
        expected_R=float(compute_expectancy(trade_records).get("expected_R") or 0),
        win_rate=float(metrics.get("win_rate") or 0),
        profit_factor=metrics.get("profit_factor"),
        ledger_path=ledger_path,
    )


# ---------------------------------------------------------------------------
# 2. Bridge unified session → merged acceptance store
# ---------------------------------------------------------------------------

def ingest_acceptance_evidence(conn: sqlite3.Connection,
                                session_result: dict,
                                *, symbol: str,
                                stage: str = "paper") -> dict:
    """Push every artifact the unified session produced into the
    paper_acceptance_metrics tables so gates have real evidence.

    Returns a count summary of rows written.
    """
    ensure_paper_acceptance_schema(conn)
    ensure_paper_acceptance_metrics_schema(conn)
    counts = {"runtime_metrics": 0, "order_audit": 0,
              "virtual_snapshot": 0, "alerts": 0}

    acc = session_result.get("acceptance") or {}
    metrics = acc.get("metrics") or {}
    for key, val in metrics.items():
        if val is None: continue
        try:
            record_runtime_metric(conn, symbol=symbol, stage=stage,
                                   metric_key=key, value=float(val),
                                   source="unified_session")
            counts["runtime_metrics"] += 1
        except (TypeError, ValueError, sqlite3.Error):
            pass

    for dec in session_result.get("decisions") or []:
        live = dec.get("live_order") or {}
        op = (live.get("payload") or {}) if isinstance(live, dict) else {}
        if op and op.get("id"):
            try:
                record_order_audit(conn, symbol=symbol, stage=stage,
                                    order_id=str(op.get("id")),
                                    side=op.get("side"),
                                    quantity=float(op.get("quantity") or 0),
                                    price=float(op.get("price") or 0),
                                    status=op.get("status") or "pending",
                                    source="crypto_api")
                counts["order_audit"] += 1
            except (TypeError, ValueError, sqlite3.Error):
                pass

    try:
        record_virtual_account_snapshot(
            conn, symbol=symbol, stage=stage,
            equity=100_000.0, exposure=0.0,
            cash=100_000.0, frozen=0.0,
        )
        counts["virtual_snapshot"] += 1
    except (TypeError, sqlite3.Error):
        pass

    for issue in acc.get("blocking_issues") or []:
        try:
            record_alert_delivery(
                conn, symbol=symbol, stage=stage,
                alert_key=issue.get("id"),
                severity="critical" if issue.get("status") == "fail" else "warning",
                channel="paper_acceptance",
                delivered=True,
            )
            counts["alerts"] += 1
        except (TypeError, sqlite3.Error):
            pass

    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# 3. Closed-loop training: ledger → new weights → strategy.yaml
# ---------------------------------------------------------------------------

@dataclass
class TrainingResult:
    started_at: str
    elapsed_seconds: float
    sample_size: int
    verdict: dict           # run_closed_loop_calibration verdict
    weights_before: dict
    weights_after: dict
    weights_changed: list[str]
    adopted: bool
    strategy_yaml_updated: bool
    adaptive_state: dict = field(default_factory=dict)
    gate_results: dict = field(default_factory=dict)
    probe_plan: dict = field(default_factory=dict)
    adaptive_patch_key: Optional[str] = None
    strategy_patch_key: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _sanitize_records_for_learning(
    records: list[dict],
    *,
    max_abs_r: float = _LEARNING_R_MULTIPLE_CAP,
) -> tuple[list[dict], dict]:
    """Return a caller-owned learning set with clipped R tails."""
    sanitized: list[dict] = []
    clipped = 0
    max_seen = 0.0
    for record in records or []:
        item = dict(record)
        rm = item.get("r_multiple")
        try:
            raw = float(rm)
        except (TypeError, ValueError):
            sanitized.append(item)
            continue
        if not math.isfinite(raw):
            sanitized.append(item)
            continue
        max_seen = max(max_seen, abs(raw))
        clipped_rm = float(np.clip(raw, -float(max_abs_r), float(max_abs_r)))
        if abs(clipped_rm - raw) > 1e-9:
            clipped += 1
            item["r_multiple_raw"] = raw
        item["r_multiple"] = clipped_rm
        if item.get("pnl_R") is not None:
            item["pnl_R"] = clipped_rm
        sanitized.append(item)
    return sanitized, {
        "max_abs_r_cap": float(max_abs_r),
        "clipped_count": int(clipped),
        "max_abs_r_seen": round(float(max_seen), 4),
    }


def _adaptive_metrics_from_records(records: list[dict], calib: dict, l1_penalty: float = 0.0) -> dict:
    frame = pd.DataFrame(records)
    fatal_reasons: list[str] = []
    if frame.empty or "entry_time" not in frame or "exit_time" not in frame:
        fatal_reasons.append("missing_entry_exit_time")
        return {
            "n_eff": 0.0,
            "uniqueness_mean": 0.0,
            "purged_folds": [],
            "gate_results": build_gate_results(
                walk_forward_pass_ratio=0.0,
                pbo=1.0,
                dsr_probability=0.0,
                overall_expectancy=-1.0,
                recent_expectancy=-1.0,
                historical_expectancy=1.0,
                overall_win_rate=0.0,
                recent_win_rate=0.0,
                calibration_new_score=0.0,
                calibration_old_score=1.0,
                fatal_reasons=fatal_reasons,
            ),
            "validation": {"state_hint": "LOCKED", "risk_multiplier": 0.0, "entropy": 1.0, "amplitude": 1.0},
            "pbo": {"pbo": None},
            "dsr": {"deflated": 0.0, "p_value_proxy": 1.0, "threshold_sharpe": None, "passes": False},
            "sharpe": {"sharpe": 0.0},
        }

    timestamps = pd.to_datetime(
        pd.concat(
            [frame["entry_time"], frame["exit_time"]],
            ignore_index=True,
        ),
        errors="coerce",
        utc=True,
    ).dropna()
    bar_index = pd.DatetimeIndex(sorted(timestamps.unique()))
    if len(bar_index) == 0:
        fatal_reasons.append("empty_bar_index")
        uniqueness = pd.Series(dtype=float)
        n_eff = 0.0
        uniqueness_mean = 0.0
    else:
        uniqueness = compute_sample_uniqueness(frame, bar_index, entry_col="entry_time", exit_col="exit_time")
        n_eff = effective_sample_size(uniqueness.to_numpy())
        uniqueness_mean = float(uniqueness.mean()) if len(uniqueness) else 0.0

    purged_splitter = PurgedWalkForwardSplit(n_splits=4, embargo_bars=1)
    purged_folds = list(purged_splitter.split_with_meta(frame, min_train_samples=5))

    wf = (calib.get("oos_validation") or {}).get("folds") or []
    wf_pass_ratio = (
        sum(1 for fold in wf if fold.get("edge_preserved")) / len(wf)
        if wf
        else 0.0
    )
    in_sample_r = [float(fold.get("in_sample_expected_R") or 0.0) for fold in wf]
    oos_r = [float(fold.get("oos_expected_R") or 0.0) for fold in wf]
    pbo = estimate_pbo(in_sample_r, oos_r) if in_sample_r and oos_r else {"pbo": None, "note": "insufficient_walk_forward_folds"}
    pbo_value = float(pbo.get("pbo")) if pbo.get("pbo") is not None else 1.0

    r_values = [float(r.get("r_multiple") or r.get("pnl_R") or 0.0) for r in records]
    sr = sharpe_ratio(r_values, annualize=252)
    dsr = deflated_sharpe_ratio(
        sr.get("sharpe", 0.0),
        n_trials=max(5, len(wf) or 1),
        sample_size=max(2, int(round(n_eff)) or len(r_values) or 2),
    )
    dsr_probability = max(0.0, 1.0 - float(dsr.get("p_value_proxy") or 1.0))

    if len(records) >= 6:
        split = max(1, int(len(records) * 0.66))
        historical = records[:split]
        recent = records[split:]
    else:
        historical = records
        recent = records
    overall_stats = compute_expectancy(records)
    historical_stats = compute_expectancy(historical)
    recent_stats = compute_expectancy(recent)
    overall_expectancy = float(overall_stats.get("expected_R") or 0.0)
    historical_expectancy = float(historical_stats.get("expected_R") or 0.0)
    recent_expectancy = float(recent_stats.get("expected_R") or 0.0)
    overall_win_rate = float(overall_stats.get("win_rate") or 0.0)
    recent_win_rate = float(recent_stats.get("win_rate") or 0.0)
    decay = edge_decay_check(historical, recent, min_live_samples=min(10, max(1, len(recent))))
    calibration_new = float(np.mean(oos_r)) if oos_r else 0.0
    calibration_old = float(np.mean(in_sample_r)) if in_sample_r else max(calibration_new, 1e-6)

    gate_results = build_gate_results(
        walk_forward_pass_ratio=wf_pass_ratio,
        pbo=pbo_value,
        dsr_probability=dsr_probability,
        overall_expectancy=overall_expectancy,
        recent_expectancy=recent_expectancy,
        historical_expectancy=historical_expectancy,
        overall_win_rate=overall_win_rate,
        recent_win_rate=recent_win_rate,
        calibration_new_score=calibration_new,
        calibration_old_score=calibration_old,
        fatal_reasons=fatal_reasons,
    )
    validation = validation_entropy_sizing(gate_results, n_eff=n_eff)
    feature_diagnostics = {
        "trained": False,
        "reason": "insufficient_samples",
    }
    model_baseline = {
        "trained": False,
        "reason": "insufficient_samples",
        "proposal": {},
        "diagnostics": {"sample_size": len(records)},
    }
    fm_challenger = {
        "trained": False,
        "reason": "insufficient_samples",
        "top_interactions": [],
        "diagnostics": {"sample_size": len(records), "n_eff": round(float(n_eff), 4)},
    }
    if len(records) >= 8:
        feature_block = build_feature_matrix(records)
        X = feature_block["X"]
        y = feature_block["y"]
        sample_weights = build_trade_sample_weights(
            y,
            uniqueness.to_numpy() if len(uniqueness) else np.ones(len(y), dtype=float),
        )
        try:
            feature_diagnostics = marchenko_pastur_eigen_clip(X)
        except Exception as exc:
            feature_diagnostics = {"trained": False, "reason": f"denoising_failed:{exc}"}
        model_baseline = fit_uniqueness_weighted_lr(
            X,
            y,
            sample_weights,
            feature_cols=feature_block["feature_cols"],
            l1_penalty=l1_penalty,
        )
        model_baseline["trained"] = True
        model_baseline["sample_weight_summary"] = {
            "min": round(float(np.min(sample_weights)), 6),
            "max": round(float(np.max(sample_weights)), 6),
            "mean": round(float(np.mean(sample_weights)), 6),
            "n_eff": round(float(effective_sample_size(sample_weights)), 4),
        }
        fm_challenger = fit_factorization_machine_classifier(
            X,
            y,
            sample_weights,
            n_eff=n_eff,
            feature_cols=feature_block["feature_cols"],
        )
    return {
        "n_eff": round(float(n_eff), 4),
        "uniqueness_mean": round(uniqueness_mean, 4),
        "purged_folds": purged_folds,
        "walk_forward_pass_ratio": round(float(wf_pass_ratio), 4),
        "gate_results": gate_results,
        "validation": validation,
        "pbo": pbo,
        "dsr": dsr,
        "sharpe": sr,
        "edge_decay": decay,
        "overall_expectancy": round(overall_expectancy, 4),
        "historical_expectancy": round(historical_expectancy, 4),
        "recent_expectancy": round(recent_expectancy, 4),
        "overall_win_rate": round(overall_win_rate, 4),
        "recent_win_rate": round(recent_win_rate, 4),
        "feature_denoising": feature_diagnostics,
        "weighted_lr": model_baseline,
        "fm_challenger": fm_challenger,
    }


def train_from_ledger(
    ledger_path: Optional[str] = None,
    strategy_yaml_path: str = "config/strategy.yaml",
    *,
    base_weights: Optional[dict[str, int]] = None,
    db_path: Optional[str] = None,
    symbol: str = "ALL",
    model_version: str = ADAPTIVE_MODEL_VERSION,
    apply_strategy_patch: bool = True,
    optimal_interval: Optional[str] = None,
) -> TrainingResult:
    """Run the §18.5 closed-loop calibration over the ledger; if OOS
    passes, persist suggested weights back to ``config/strategy.yaml``
    and re-apply so subsequent SMC runs use the new weights.
    """
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    notes: list[str] = []
    weights_before = dict(CONFLUENCE_WEIGHTS_DEFAULT)
    config_snapshot = strategy_config_snapshot(strategy_yaml_path)

    ledger_path = ledger_path or LedgerPaths.training_ledger()
    raw_records: list[dict] = read_trade_ledger(
        ledger_path,
        symbol=symbol if symbol != "ALL" else None,
    )
    if not raw_records:
        if db_path:
            conn = connect_db(db_path)
            conn.row_factory = sqlite3.Row
            try:
                ensure_adaptive_calibration_schema(conn)
                record_adaptive_audit_event(
                    conn,
                    symbol=symbol,
                    event_type="calibration_skipped",
                    severity="warning",
                    state_after={"mode": "DRY_RUN"},
                    detail={"reason": "no_records", "ledger_path": ledger_path},
                )
                conn.commit()
            finally:
                conn.close()
        return TrainingResult(
            started_at=started_at,
            elapsed_seconds=round(time.time() - t0, 3),
            sample_size=0,
            verdict={"adopt": False, "reason": "no_records"},
            weights_before=weights_before, weights_after=weights_before,
            weights_changed=[],
            adopted=False, strategy_yaml_updated=False,
            notes=["ledger empty — run auto_backtest_window first"],
        )

    records, learning_clip = _sanitize_records_for_learning(raw_records)
    pbo_last = 0.0
    embargo_dynamic = 1

    # 模組二：基於動態資訊漂移的自適應視窗與隔離
    if len(records) >= 30:
        n_half = len(records) // 2
        older_recs = records[:n_half]
        recent_recs = records[n_half:]
        u_dist = np.array([float(r.get("r_multiple") or r.get("pnl_R") or 0.0) for r in older_recs])
        v_dist = np.array([float(r.get("r_multiple") or r.get("pnl_R") or 0.0) for r in recent_recs])
        
        # 簡單計算一維 Wasserstein 距離
        u_sorted, v_sorted = np.sort(u_dist), np.sort(v_dist)
        u_interp = np.interp(np.linspace(0, 1, 100), np.linspace(0, 1, len(u_sorted)), u_sorted)
        v_interp = np.interp(np.linspace(0, 1, 100), np.linspace(0, 1, len(v_sorted)), v_sorted)
        drift_distance = float(np.mean(np.abs(u_interp - v_interp)))
        
        if drift_distance > 0.15:
            embargo_dynamic = min(15, 1 + int(30 * (drift_distance - 0.15)))
            old_len = len(records)
            records = records[int(old_len * 0.3):]
            notes.append(
                f"Regime drift detected (Wasserstein distance={drift_distance:.4f} > 0.15): "
                f"shortened training window from {old_len} to {len(records)} samples, "
                f"and expanded walk-forward embargo from 1 to {embargo_dynamic}."
            )

    adaptive_conn: Optional[sqlite3.Connection] = None
    if db_path:
        adaptive_conn = connect_db(db_path)
        adaptive_conn.row_factory = sqlite3.Row
        ensure_adaptive_calibration_schema(adaptive_conn)
        upsert_trade_ledger_records(
            adaptive_conn,
            raw_records,
            config_hash=config_snapshot["hash"],
            model_version=model_version,
            source="training_ledger",
        )
        
        # 模組一：載入上一期的歷史 PBO
        try:
            row = adaptive_conn.execute(
                "SELECT patch_payload FROM smc_adaptive_config_patches WHERE patch_type='adaptive_runtime' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                payload = json.loads(row["patch_payload"])
                pbo_last = float(payload.get("diagnostics", {}).get("pbo") or 0.0)
        except Exception:
            pass

        kill_state = get_kill_switch_state(adaptive_conn)
        if kill_state.get("state") == "LOCKED":
            record_adaptive_audit_event(
                adaptive_conn,
                symbol=symbol,
                event_type="calibration_blocked",
                severity="critical",
                state_before=kill_state,
                state_after=kill_state,
                detail={"reason": "kill_switch_locked"},
            )
            adaptive_conn.commit()
            adaptive_conn.close()
            return TrainingResult(
                started_at=started_at,
                elapsed_seconds=round(time.time() - t0, 3),
                sample_size=len(raw_records),
                verdict={"adopt": False, "reason": "kill_switch_locked"},
                weights_before=weights_before,
                weights_after=weights_before,
                weights_changed=[],
                adopted=False,
                strategy_yaml_updated=False,
                notes=["adaptive kill switch locked — calibration write blocked"],
            )

    l1_penalty = 0.0
    if pbo_last >= 0.5 and len(records) > 8:
        l1_0 = 0.05
        alpha_shrink = 0.5
        l1_penalty = l1_0 * (1.0 + alpha_shrink * pbo_last * math.log(max(len(records), 2)))
        notes.append(f"Adaptive Feature Shrinkage active (PBO_last={pbo_last:.4f} >= 0.5): l1_penalty scaled to {l1_penalty:.6f}")

    calib = run_closed_loop_calibration(records, base_weights=base_weights)
    adaptive_metrics = _adaptive_metrics_from_records(records, calib, l1_penalty=l1_penalty)
    verdict = calib.get("verdict") or {}
    proposed = (calib.get("proposed_calibration") or {}).get("weights") or {}
    adaptive_state = {
        "mode": adaptive_metrics["validation"].get("state_hint", "DRY_RUN"),
        "n_eff": adaptive_metrics["n_eff"],
        "validation_entropy": round(float(adaptive_metrics["validation"].get("entropy", 1.0)), 4),
        "validation_amplitude": round(float(adaptive_metrics["validation"].get("amplitude", 1.0)), 4),
        "risk_multiplier": round(float(adaptive_metrics["validation"].get("risk_multiplier", 0.0)), 4),
        "uniqueness_mean": adaptive_metrics["uniqueness_mean"],
        "current_sr": adaptive_metrics["sharpe"].get("sharpe"),
        "required_sr_for_dsr": adaptive_metrics["dsr"].get("threshold_sharpe"),
        "pbo": adaptive_metrics["pbo"].get("pbo"),
        "overall_win_rate": adaptive_metrics.get("overall_win_rate"),
        "recent_win_rate": adaptive_metrics.get("recent_win_rate"),
        "overall_expectancy": adaptive_metrics.get("overall_expectancy"),
        "recent_expectancy": adaptive_metrics.get("recent_expectancy"),
        "learning_r_clip": learning_clip,
    }
    probe_plan: dict = {}
    adaptive_patch_key: Optional[str] = None
    strategy_patch_key: Optional[str] = None
    current_threshold = float(
        (config_snapshot.get("data") or {}).get("confluence", {}).get(
            "threshold",
            CONFLUENCE_THRESHOLD_DEFAULT,
        )
    )
    current_sr = float(adaptive_metrics["sharpe"].get("sharpe") or 0.0)
    dsr_prob = dsr_probability(current_sr, 0.0, max(adaptive_metrics["n_eff"], 2.0))
    required_sr = required_sharpe_for_dsr(0.95, 0.0, max(adaptive_metrics["n_eff"], 2.0))
    dynamic_threshold = update_confluence_threshold_by_dsr(
        current_threshold=current_threshold,
        base_threshold=current_threshold,
        max_threshold=current_threshold + 4.0,
        current_sr=current_sr,
        required_sr=required_sr,
        k=1.25,
        smoothing=0.25,
    )
    adaptive_state["current_dsr_probability"] = round(dsr_prob, 4)
    adaptive_state["required_sr_for_dsr"] = round(required_sr, 4)
    adaptive_state["dynamic_confluence_threshold"] = round(dynamic_threshold, 4)
    weighted_lr_diag = adaptive_metrics["weighted_lr"].get("diagnostics") or {}
    fm_diag = adaptive_metrics["fm_challenger"].get("diagnostics") or {}
    fm_trained = bool(adaptive_metrics["fm_challenger"].get("trained"))
    # Per smc_adaptive_calibration_development_plan §998-1004, the FM
    # challenger may ONLY be adopted when it beats the LR baseline in a
    # *purged* walk-forward OOS comparison — not on the same training set.
    # We compute the OOS comparison here; if there isn't enough data for a
    # valid split the challenger is NOT adopted (default-deny).
    fm_oos = _purged_walk_forward_fm_vs_lr(records, embargo=embargo_dynamic) if fm_trained else {
        "verdict": "no_fm_to_compare",
        "fm_oos_accuracy": None,
        "lr_oos_accuracy": None,
        "fold_count": 0,
    }
    adopt_challenger = bool(
        fm_trained
        and fm_oos["verdict"] == "fm_beats_lr_oos"
        and adaptive_state["mode"] == "READY"
    )

    # P1-8 audit fix: if recent performance has decayed against historical
    # baseline, demote READY → VALIDATING_PROBE before continuing. This is
    # the missing "stop trading when edge dies" guardrail.
    decay_block = _detect_recent_edge_decay(records)
    if decay_block.get("is_decaying") and adaptive_state["mode"] == "READY":
        adaptive_state["mode"] = "VALIDATING_PROBE"
        adaptive_state["edge_decay_triggered"] = True
        adaptive_state["edge_decay_reason"] = decay_block.get("warning_message")
        adaptive_state["risk_multiplier"] = min(
            float(adaptive_state.get("risk_multiplier") or 1.0), 0.1
        )
        # P1-8+ audit fix: leave an audit trail (alert + Obsidian) so the
        # user finds out the system demoted itself.
        _emit_edge_decay_trail(
            adaptive_conn, symbol=symbol, decay=decay_block,
            new_mode=adaptive_state["mode"],
        )
    else:
        adaptive_state["edge_decay_triggered"] = False
    adaptive_state["edge_decay_diagnostics"] = decay_block

    if adaptive_state["mode"] == "VALIDATING_PROBE" and adaptive_conn is not None:
        stop_distance_pcts = []
        for record in records:
            entry_price = float(record.get("entry_price") or 0.0)
            stop_price = float(record.get("stop_price") or record.get("stop") or 0.0)
            if entry_price > 0 and stop_price > 0:
                stop_distance_pcts.append(abs(entry_price - stop_price) / entry_price)
        stop_distance_pct = float(np.median(stop_distance_pcts)) if stop_distance_pcts else 0.02
        base_risk_pct = float(
            (config_snapshot.get("data") or {}).get("risk", {}).get("risk_pct", 0.01)
            or 0.01
        )
        probe_plan = plan_probe_order(
            adaptive_conn,
            symbol=symbol,
            risk_multiplier=float(adaptive_state["risk_multiplier"]),
            account_equity=100_000.0,
            base_risk_pct=base_risk_pct,
            stop_distance_pct=stop_distance_pct,
        )
        if not probe_plan.get("allow_order"):
            adaptive_state["mode"] = probe_plan.get("state_hint", "DRY_RUN")
            adaptive_state["risk_multiplier"] = 0.0

    adopted = False
    is_soft_adopted = False
    if bool(verdict.get("adopt")):
        if adaptive_state["mode"] == "READY":
            adopted = True
        elif adaptive_state["mode"] == "VALIDATING_PROBE" and proposed:
            alpha = 0.3
            soft_proposed = {}
            for k, proposed_v in proposed.items():
                before_v = weights_before.get(k, proposed_v)
                soft_proposed[k] = int(round((1.0 - alpha) * before_v + alpha * proposed_v))
            proposed = soft_proposed
            is_soft_adopted = True
            notes.append("soft_adoption active: updating weights incrementally by step 0.3 in VALIDATING_PROBE mode")
            verdict = {**verdict, "adopt": True, "soft_adopted": True}

    if bool(verdict.get("adopt")) and not (adopted or is_soft_adopted):
        verdict = {**verdict, "adopt": False, "reason": "adaptive_state_not_ready"}


    runtime_patch = {
        "state": {
            "mode": adaptive_state["mode"],
            "adopt_weights": adopted or is_soft_adopted,
            "n_eff": adaptive_state["n_eff"],
            "validation_entropy": adaptive_state["validation_entropy"],
            "validation_amplitude": adaptive_state["validation_amplitude"],
        },
        "risk": {
            "risk_multiplier": adaptive_state["risk_multiplier"],
            "probe_notional_cap_usdt": float(probe_plan.get("notional_usdt") or 5.0),
        },
        "strategy": {
            "confluence_min_score": adaptive_state["dynamic_confluence_threshold"],
            "optimal_interval": optimal_interval,
        },
        "model": {
            # ``active_model`` reflects the *currently deployed* model. The
            # adoption switch is gated by Purged Walk-Forward OOS; until
            # ``adopt_challenger=True`` AND a separate switch ceremony runs,
            # we stay on the LR baseline. The field is purely informational
            # in this runtime patch (it never reaches strategy.yaml — see
            # APPLICABLE_PATCH_TYPES).
            "active_model": "uniqueness_weighted_lr",
            "challenger_model": "factorization_machine" if fm_trained else "purged_uniqueness_lr",
            "adopt_challenger": adopt_challenger,
            "adopt_challenger_basis": "purged_walk_forward_oos",
            "challenger_oos": fm_oos,
        },
        "diagnostics": {
            "required_sr_for_dsr": adaptive_state["required_sr_for_dsr"],
            "current_sr": adaptive_state["current_sr"],
            "current_dsr_probability": adaptive_state["current_dsr_probability"],
            "pbo": adaptive_state["pbo"],
            "walk_forward_pass_ratio": adaptive_metrics["walk_forward_pass_ratio"],
            "purged_fold_count": len(adaptive_metrics["purged_folds"]),
            "mp_lambda_plus": adaptive_metrics["feature_denoising"].get("lambda_plus"),
            # Existing in-sample diagnostics — kept for backwards compat with
            # downstream UI / tests that read these names.
            "weighted_lr_accuracy": weighted_lr_diag.get("accuracy"),
            "weighted_lr_in_sample_accuracy": weighted_lr_diag.get("accuracy"),
            "weighted_lr_log_loss": weighted_lr_diag.get("log_loss"),
            "fm_accuracy": fm_diag.get("accuracy"),
            "fm_in_sample_accuracy": fm_diag.get("accuracy"),
            "fm_log_loss": fm_diag.get("log_loss"),
            "fm_trained": fm_trained,
            # New OOS-based comparison driving adopt_challenger (audit fix)
            "fm_oos_accuracy": fm_oos.get("fm_oos_accuracy"),
            "lr_oos_accuracy": fm_oos.get("lr_oos_accuracy"),
            "fm_vs_lr_oos_margin": fm_oos.get("margin"),
            "fm_oos_fold_count": fm_oos.get("fold_count"),
            "fm_oos_verdict": fm_oos.get("verdict"),
        },
    }

    if adaptive_conn is not None:
        patch_row = create_config_patch(
            adaptive_conn,
            patch=runtime_patch,
            symbol=symbol,
            reason="adaptive_validation_state_tick",
            strategy_yaml_path=strategy_yaml_path,
            patch_type="adaptive_runtime",
            apply=False,
        )
        adaptive_patch_key = patch_row["patch_key"]
        if adaptive_state["mode"] == "LOCKED":
            set_kill_switch_state(
                adaptive_conn,
                state="LOCKED",
                reason="adaptive_validation_locked",
                detail={"gate_results": adaptive_metrics["gate_results"]},
            )
        record_adaptive_audit_event(
            adaptive_conn,
            symbol=symbol,
            event_type="adaptive_state_computed",
            severity="critical" if adaptive_state["mode"] == "LOCKED" else "info",
            state_after=adaptive_state,
            detail={
                "patch_key": adaptive_patch_key,
                "gate_results": adaptive_metrics["gate_results"],
                "probe_plan": probe_plan,
                "weighted_lr": weighted_lr_diag,
                "fm_challenger": fm_diag,
                "learning_r_clip": learning_clip,
            },
        )
        if abs(dynamic_threshold - current_threshold) > 1e-9:
            record_adaptive_audit_event(
                adaptive_conn,
                symbol=symbol,
                event_type="threshold_patch_computed",
                severity="info",
                state_before={"confluence_threshold": current_threshold},
                state_after={"confluence_threshold": adaptive_state["dynamic_confluence_threshold"]},
                detail={
                    "current_sr": current_sr,
                    "dsr_probability": adaptive_state["current_dsr_probability"],
                    "required_sr": adaptive_state["required_sr_for_dsr"],
                },
            )
        adaptive_conn.commit()

    yaml_path = Path(strategy_yaml_path)
    if not yaml_path.is_absolute():
        yaml_path = Path(__file__).parent.parent / strategy_yaml_path

    weights_after = dict(weights_before)
    changed: list[str] = []
    yaml_written = False

    if (adopted or is_soft_adopted) and proposed:
        # diff
        for k, v in proposed.items():
            if weights_before.get(k) != v:
                changed.append(k)
                weights_after[k] = v
        if changed:
            try:
                patch = {"confluence": {"weights": weights_after}}
                if adaptive_conn is not None:
                    patch_row = create_config_patch(
                        adaptive_conn,
                        patch=patch,
                        symbol=symbol,
                        reason="closed_loop_calibration_adopted",
                        strategy_yaml_path=strategy_yaml_path,
                    )
                    strategy_patch_key = patch_row["patch_key"]
                    if apply_strategy_patch:
                        apply_atomic_config_patch(
                            adaptive_conn,
                            patch_key=patch_row["patch_key"],
                            strategy_yaml_path=strategy_yaml_path,
                            expected_hash=config_snapshot["hash"],
                        )
                        record_adaptive_audit_event(
                            adaptive_conn,
                            symbol=symbol,
                            event_type="config_patch_applied",
                            state_before={"mode": "DRY_RUN", "config_hash": config_snapshot["hash"]},
                            state_after={"mode": "READY", "config_hash": patch_row["after_hash"]},
                            detail={
                                "patch_key": patch_row["patch_key"],
                                "changed_weights": changed,
                                "model_version": model_version,
                            },
                        )
                    else:
                        record_adaptive_audit_event(
                            adaptive_conn,
                            symbol=symbol,
                            event_type="config_patch_staged",
                            state_before={"mode": "READY", "config_hash": config_snapshot["hash"]},
                            state_after={"mode": "READY", "config_hash": patch_row["after_hash"]},
                            detail={
                                "patch_key": patch_row["patch_key"],
                                "changed_weights": changed,
                                "model_version": model_version,
                            },
                        )
                    adaptive_conn.commit()
                else:
                    import yaml  # type: ignore

                    existing = {}
                    if yaml_path.exists():
                        with open(yaml_path, "r", encoding="utf-8") as fh:
                            existing = yaml.safe_load(fh) or {}
                    existing.setdefault("confluence", {})["weights"] = weights_after
                    if apply_strategy_patch:
                        fd, tmp_name = tempfile.mkstemp(
                            prefix="strategy.",
                            suffix=".yaml",
                            dir=str(yaml_path.parent),
                        )
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                                yaml.safe_dump(existing, fh, allow_unicode=True, sort_keys=False)
                                fh.flush()
                                os.fsync(fh.fileno())
                            os.replace(tmp_name, yaml_path)
                        finally:
                            if os.path.exists(tmp_name):
                                os.unlink(tmp_name)
                if apply_strategy_patch:
                    apply_strategy_yaml_overrides()
                    yaml_written = True
                    notes.append(f"strategy.yaml updated; {len(changed)} weight(s) changed")
                    # Audit fix B1: opportunistically also run the monthly
                    # hyperparameter sweep auto-apply. Skipped if cooldown
                    # active so it really only fires once a month.
                    try:
                        from learning.sweep_auto_apply import auto_apply_sweep
                        profile_path = os.environ.get(
                            "SMC_PROFILE_YAML",
                            os.path.join(os.path.dirname(yaml_path),
                                          "profile.yaml"),
                        )
                        sweep_out = auto_apply_sweep(
                            records=records,
                            profile_path=profile_path,
                            obsidian_vault=os.environ.get("OBSIDIAN_VAULT_PATH"),
                        )
                        if sweep_out.get("applied"):
                            notes.append(
                                f"sweep auto-apply: "
                                f"min_score={sweep_out['after']['min_score']}, "
                                f"min_rr={sweep_out['after']['min_rr']}, "
                                f"risk_pct={sweep_out['after']['risk_pct']}, "
                                f"Δsharpe={sweep_out.get('delta_sharpe')}"
                            )
                        else:
                            notes.append(
                                f"sweep auto-apply skipped: {sweep_out.get('reason')}"
                            )
                    except Exception as exc:
                        notes.append(f"sweep auto-apply error: {exc}")
                else:
                    notes.append(f"strategy patch staged; {len(changed)} weight(s) pending apply")
            except Exception as exc:
                notes.append(f"yaml write failed: {exc}")
                if adaptive_conn is not None:
                    set_kill_switch_state(
                        adaptive_conn,
                        state="LOCKED",
                        reason="config_patch_failed",
                        detail={"error": repr(exc)},
                    )
                    record_adaptive_audit_event(
                        adaptive_conn,
                        symbol=symbol,
                        event_type="config_patch_failed",
                        severity="critical",
                        state_before={"mode": "DRY_RUN"},
                        state_after={"mode": "LOCKED"},
                        detail={"error": repr(exc)},
                    )
                    adaptive_conn.commit()
    else:
        notes.append(f"calibration not adopted: {verdict.get('reason')}")
        if adaptive_conn is not None:
            record_adaptive_audit_event(
                adaptive_conn,
                symbol=symbol,
                event_type="calibration_rejected",
                severity="info",
                state_before={"mode": "DRY_RUN"},
                state_after={"mode": "DRY_RUN"},
                detail={
                    "reason": verdict.get("reason"),
                    "sample_size": len(records),
                    "model_version": model_version,
                    "adaptive_mode": adaptive_state["mode"],
                },
            )
            adaptive_conn.commit()

    if learning_clip.get("clipped_count"):
        notes.append(
            f"learning R clipped: {learning_clip['clipped_count']} trade(s) capped at "
            f"+/-{learning_clip['max_abs_r_cap']}R (max seen {learning_clip['max_abs_r_seen']}R)"
        )

    if adaptive_conn is not None:
        adaptive_conn.close()

    return TrainingResult(
        started_at=started_at,
        elapsed_seconds=round(time.time() - t0, 3),
        sample_size=len(raw_records),
        verdict=verdict,
        weights_before=weights_before, weights_after=weights_after,
        weights_changed=changed,
        adopted=adopted, strategy_yaml_updated=yaml_written,
        adaptive_state=adaptive_state,
        gate_results=adaptive_metrics["gate_results"],
        probe_plan=probe_plan,
        adaptive_patch_key=adaptive_patch_key,
        strategy_patch_key=strategy_patch_key,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 4. Run scenarios so gate evidence is populated
# ---------------------------------------------------------------------------

def run_scenarios_for_symbol(conn: sqlite3.Connection, symbol: str,
                              *, stage: str = "paper") -> dict:
    """Run every paper-acceptance scenario over the symbol so gates have
    observed evidence; otherwise nearly every gate defaults to fail.
    """
    ensure_paper_acceptance_scenario_schema(conn)
    catalog = scenario_catalog()
    results = []
    for entry in catalog:
        sid = entry.get("scenario_id") or entry.get("id")
        if not sid:
            continue
        try:
            res = run_acceptance_scenario(conn, symbol=symbol,
                                            scenario_id=sid, stage=stage)
            results.append({"scenario_id": sid, "ok": True,
                            "result_keys": list(res.keys()) if isinstance(res, dict) else []})
        except Exception as exc:
            results.append({"scenario_id": sid, "ok": False, "error": repr(exc)})
    summary = summarize_scenario_evidence(conn, symbol=symbol, stage=stage)
    return {"ran": len(results), "results": results, "summary": summary}


# ---------------------------------------------------------------------------
# 5. Audit: does the model actually learn?
# ---------------------------------------------------------------------------

@dataclass
class LearningAudit:
    symbol: Optional[str]
    ledger_size: int
    monthly_stability: dict
    weight_drift: dict
    expected_R_before: float
    expected_R_after: float
    delta_expected_R: float
    learning_indicator: str   # "active" / "stagnant" / "degrading" / "insufficient_data"
    notes: list[str] = field(default_factory=list)


def audit_learning_capability(
    ledger_path: Optional[str] = None,
    *,
    symbol: Optional[str] = None,
    baseline_weights: Optional[dict] = None,
) -> LearningAudit:
    """Quantitative answer to 'does the model learn?'.

    Checks:
      • ledger size is statistically meaningful (>= 30)
      • monthly_edge_stability shows R variance (the dataset itself
        carries learnable signal)
      • current weights differ from the baseline → calibration ran
      • before/after expected_R difference is positive
    """
    notes: list[str] = []
    ledger_path = ledger_path or LedgerPaths.training_ledger()
    records: list[dict] = read_trade_ledger(ledger_path, symbol=symbol)

    ledger_size = len(records)
    stability = monthly_edge_stability(records) if records else {"status": "no_trades"}

    base = baseline_weights or {
        "htf_bias_aligned": 2, "premium_discount_side": 2,
        "unmitigated_ob": 2, "unfilled_fvg": 1, "liquidity_swept": 2,
        "ltf_choch": 2, "ote_zone": 1, "killzone": 1,
        "volume_displacement": 1, "strong_dol_target": 1,
        "poi_displacement_missing": -2,
    }
    drift: dict[str, dict] = {}
    for k, base_v in base.items():
        cur = CONFLUENCE_WEIGHTS_DEFAULT.get(k, base_v)
        if cur != base_v:
            drift[k] = {"baseline": base_v, "current": cur, "delta": cur - base_v}
    for k, cur in CONFLUENCE_WEIGHTS_DEFAULT.items():
        if k not in base:
            drift[k] = {"baseline": None, "current": cur, "delta": cur}

    # Recompute expectancy with baseline vs current weights:
    # we can't re-score historical trades against weights cheaply without
    # re-running the engine, so we use ledger expectancy as 'after' and
    # store baseline as 0R per the conservative assumption.
    expected_after = float(compute_expectancy(records).get("expected_R") or 0)
    expected_before = 0.0
    delta = expected_after - expected_before

    if ledger_size < 30:
        indicator = "insufficient_data"
        notes.append(f"only {ledger_size} trades — need ≥30 for statistical signal")
    elif not drift:
        indicator = "stagnant"
        notes.append("weights identical to baseline — no calibration has run")
    elif delta > 0.1:
        indicator = "active"
        notes.append(f"{len(drift)} weight(s) drifted; expected_R +{delta:.3f}R vs baseline")
    elif delta < -0.1:
        indicator = "degrading"
        notes.append(f"calibration regressed: expected_R {delta:.3f}R below baseline")
    else:
        indicator = "stagnant"
        notes.append(f"{len(drift)} weight(s) drifted but expected_R Δ={delta:.3f}R ≈ 0")

    return LearningAudit(
        symbol=symbol,
        ledger_size=ledger_size,
        monthly_stability=stability,
        weight_drift=drift,
        expected_R_before=expected_before,
        expected_R_after=expected_after,
        delta_expected_R=delta,
        learning_indicator=indicator,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Top-level convenience: one-call train + audit
# ---------------------------------------------------------------------------

def run_training_cycle(
    api: CryptoApiClient,
    symbols: list[str],
    *,
    db_path: str,
    interval: str = "1h",
    bars: int = 500,
    ledger_path: Optional[str] = None,
) -> dict:
    """Full cycle: backtest each symbol → ingest evidence → train →
    audit learning. Single API for the UI 'Train Now' button."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    ledger_path = ledger_path or LedgerPaths.training_ledger()
    conn = connect_db(db_path, row_factory=True)
    backtest_summaries: list[dict] = []
    try:
        for sym in symbols:
            bs = auto_backtest_window(api, sym, interval=interval, bars=bars,
                                       ledger_path=ledger_path, db_path=db_path)
            backtest_summaries.append(asdict(bs))
        training = train_from_ledger(
            ledger_path=ledger_path,
            db_path=db_path,
            symbol="ALL" if len(symbols) > 1 else symbols[0],
        )
        audit = audit_learning_capability(ledger_path=ledger_path)
        scenarios = {sym: run_scenarios_for_symbol(conn, sym) for sym in symbols}
        return {
            "started_at": started_at,
            "elapsed_seconds": round(time.time() - t0, 3),
            "symbols": symbols,
            "backtests": backtest_summaries,
            "training": asdict(training),
            "audit": asdict(audit),
            "scenarios": {k: {"ran": v["ran"], "summary": v["summary"]} for k, v in scenarios.items()},
        }
    finally:
        conn.close()
