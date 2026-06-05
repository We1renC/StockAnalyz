"""Promotion policy and threshold evaluation for paper acceptance."""

from __future__ import annotations

from typing import Any, Mapping


DEFAULT_THRESHOLDS = {
    "intraday": {
        "min_trade_count": 50,
        "min_testing_days": 20,
        "min_regime_coverage_score": 0.7,
        "max_api_error_rate": 0.05,
        "max_average_slippage_bps": 80.0,
        "min_fill_rate": 0.75,
        "max_rejection_ratio": 0.1,
        "max_drawdown_abs": 0.15,
        "max_paper_live_deviation": 0.35,
        "min_capital_stage_count": 1,
    },
    "swing": {
        "min_trade_count": 20,
        "min_testing_days": 30,
        "min_regime_coverage_score": 0.55,
        "max_api_error_rate": 0.05,
        "max_average_slippage_bps": 120.0,
        "min_fill_rate": 0.7,
        "max_rejection_ratio": 0.1,
        "max_drawdown_abs": 0.2,
        "max_paper_live_deviation": 0.4,
        "min_capital_stage_count": 1,
    },
}

PROMOTION_STAGES = (
    {"stage_name": "stage0_paper", "label": "Stage 0 Paper", "capital_ratio": 0.0},
    {"stage_name": "stage1_1_5", "label": "Stage 1 1%-5%", "capital_ratio": 0.05},
    {"stage_name": "stage2_10_20", "label": "Stage 2 10%-20%", "capital_ratio": 0.20},
    {"stage_name": "stage3_25_50", "label": "Stage 3 25%-50%", "capital_ratio": 0.50},
    {"stage_name": "stage4_full", "label": "Stage 4 Full", "capital_ratio": 1.00},
)


def _checks_for_gate(evidence: Mapping[str, Any], gate_id: str) -> dict[str, Any]:
    gate = evidence.get(gate_id) or {}
    checks = gate.get("checks") if isinstance(gate, Mapping) else {}
    return checks if isinstance(checks, Mapping) else {}


def _all_truthy(checks: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return all(checks.get(key) is True for key in keys)


def _stage_meta_by_name(stage_name: str | None) -> dict[str, Any]:
    for row in PROMOTION_STAGES:
        if row["stage_name"] == stage_name:
            return dict(row)
    return dict(PROMOTION_STAGES[0])


def _infer_current_stage(metrics: Mapping[str, Any], capital_stages: list[dict]) -> dict[str, Any]:
    if capital_stages:
        return _stage_meta_by_name(capital_stages[0].get("stage_name"))
    stage_count = int(metrics.get("capital_stage_count") or 0)
    if stage_count >= 4:
        return dict(PROMOTION_STAGES[4])
    if stage_count >= 3:
        return dict(PROMOTION_STAGES[3])
    if stage_count >= 2:
        return dict(PROMOTION_STAGES[2])
    if stage_count >= 1:
        return dict(PROMOTION_STAGES[1])
    return dict(PROMOTION_STAGES[0])


def _build_promotion_ladder(
    *,
    strategy: Mapping[str, Any],
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    blockers: list[str],
    review_payload: Mapping[str, Any],
    capital_stages: list[dict],
    deviation_snapshots: list[dict],
    shared_architecture: bool,
    paper_live_ready: bool,
) -> dict[str, Any]:
    current = _infer_current_stage(metrics, capital_stages)
    current_index = next((idx for idx, row in enumerate(PROMOTION_STAGES) if row["stage_name"] == current["stage_name"]), 0)
    next_stage = dict(PROMOTION_STAGES[min(current_index + 1, len(PROMOTION_STAGES) - 1)])
    threshold_source = {
        "kind": "threshold_profile" if metrics.get("threshold_profile_active") else "default",
        "version_tag": metrics.get("threshold_profile_version_tag") or "",
        "profile_name": metrics.get("threshold_profile_name") or "",
        "approved_by": metrics.get("threshold_profile_approved_by") or "",
        "source_summary": metrics.get("threshold_profile_source_summary") or {},
    }
    latest_live_deviation = next(
        (row for row in deviation_snapshots if row.get("baseline_source") == "paper" and row.get("comparison_source") == "live"),
        None,
    )
    latest_backtest_deviation = next(
        (row for row in deviation_snapshots if row.get("baseline_source") == "backtest" and row.get("comparison_source") == "paper"),
        None,
    )
    trade_count = int(metrics.get("trade_count") or 0)
    testing_days = int(metrics.get("testing_days") or 0)
    fill_rate = metrics.get("fill_rate")
    avg_slippage = metrics.get("average_slippage")
    api_error_rate = metrics.get("api_error_rate")
    shadow_score = metrics.get("shadow_parity_score")
    live_deviation = metrics.get("paper_live_max_deviation_ratio")
    regime_coverage_score = metrics.get("regime_coverage_score")

    checks = [
        {
            "key": "trade_count",
            "label": "Trade Count",
            "current": trade_count,
            "threshold": thresholds["min_trade_count"],
            "delta": max(0, thresholds["min_trade_count"] - trade_count),
            "pass": trade_count >= thresholds["min_trade_count"],
            "unit": "trades",
            "source": {"kind": "paper_journal", "metric": "trade_count"},
        },
        {
            "key": "testing_days",
            "label": "Testing Days",
            "current": testing_days,
            "threshold": thresholds["min_testing_days"],
            "delta": max(0, thresholds["min_testing_days"] - testing_days),
            "pass": testing_days >= thresholds["min_testing_days"],
            "unit": "days",
            "source": {"kind": "paper_journal", "metric": "testing_days"},
        },
        {
            "key": "fill_rate",
            "label": "Fill Rate",
            "current": fill_rate,
            "threshold": thresholds["min_fill_rate"],
            "delta": round(max(0.0, float(thresholds["min_fill_rate"]) - float(fill_rate or 0)), 4) if fill_rate is not None else None,
            "pass": fill_rate is not None and float(fill_rate) >= thresholds["min_fill_rate"],
            "unit": "ratio",
            "source": {"kind": "runtime_metrics", "metric": "fill_rate", **threshold_source},
        },
        {
            "key": "average_slippage",
            "label": "Average Slippage",
            "current": avg_slippage,
            "threshold": thresholds["max_average_slippage_bps"],
            "delta": round(max(0.0, float(avg_slippage or 0) - float(thresholds["max_average_slippage_bps"])), 4) if avg_slippage is not None else None,
            "pass": avg_slippage is not None and float(avg_slippage) <= thresholds["max_average_slippage_bps"],
            "unit": "bps",
            "source": {"kind": "runtime_metrics", "metric": "average_slippage", **threshold_source},
        },
        {
            "key": "api_error_rate",
            "label": "API Error Rate",
            "current": api_error_rate,
            "threshold": thresholds["max_api_error_rate"],
            "delta": round(max(0.0, float(api_error_rate or 0) - float(thresholds["max_api_error_rate"])), 4) if api_error_rate is not None else None,
            "pass": api_error_rate is not None and float(api_error_rate) <= thresholds["max_api_error_rate"],
            "unit": "ratio",
            "source": {"kind": "runtime_metrics", "metric": "api_error_rate", **threshold_source},
        },
        {
            "key": "regime_coverage",
            "label": "Regime Coverage",
            "current": regime_coverage_score,
            "threshold": thresholds["min_regime_coverage_score"],
            "delta": round(max(0.0, float(thresholds["min_regime_coverage_score"]) - float(regime_coverage_score or 0)), 4) if regime_coverage_score is not None else None,
            "pass": regime_coverage_score is not None and float(regime_coverage_score) >= thresholds["min_regime_coverage_score"],
            "unit": "score",
            "source": {"kind": "regime_coverage_matrix", "metric": "regime_coverage_score", **threshold_source},
        },
        {
            "key": "shadow_parity",
            "label": "Shadow Parity",
            "current": shadow_score,
            "threshold": 0.8,
            "delta": round(max(0.0, 0.8 - float(shadow_score or 0)), 4) if shadow_score is not None else None,
            "pass": shared_architecture and shadow_score is not None and float(shadow_score) >= 0.8,
            "unit": "score",
            "source": {"kind": "shadow_parity_trace", "metric": "shadow_parity_score"},
        },
        {
            "key": "paper_live_deviation",
            "label": "Paper-Live Deviation",
            "current": live_deviation,
            "threshold": thresholds["max_paper_live_deviation"],
            "delta": round(max(0.0, float(live_deviation or 0) - float(thresholds["max_paper_live_deviation"])), 4) if live_deviation is not None else None,
            "pass": paper_live_ready,
            "unit": "ratio",
            "source": {
                "kind": "deviation_snapshot",
                "metric": "paper_live_max_deviation_ratio",
                "snapshot": latest_live_deviation or latest_backtest_deviation or {},
                **threshold_source,
            },
        },
    ]

    blocker_deltas = [row for row in checks if row["pass"] is False]
    rationale: list[str] = []
    if blocker_deltas:
        for row in blocker_deltas[:5]:
            rationale.append(
                f"{row['label']} 未達門檻：目前 {row['current']}，標準 {row['threshold']}。"
            )
    if blockers:
        rationale.extend(f"阻擋項：{item}" for item in blockers[:5])
    if not rationale:
        if review_payload.get("review_status") == "approved" and review_payload.get("can_promote_to_live"):
            rationale.append("審閱已批准，且量化門檻與禁止條件均已通過。")
        else:
            rationale.append("量化門檻已達成，但仍需審閱批准後才能升級。")

    checkpoints: list[dict[str, Any]] = []
    for idx, row in enumerate(PROMOTION_STAGES):
        if idx < current_index:
            status = "completed"
        elif idx == current_index:
            status = "current"
        elif idx == current_index + 1:
            status = "ready" if not blockers and not blocker_deltas else "blocked"
        else:
            status = "pending"
        checkpoints.append({
            "stage_name": row["stage_name"],
            "label": row["label"],
            "capital_ratio": row["capital_ratio"],
            "status": status,
        })

    return {
        "strategy_type": str(strategy.get("strategy_type") or "intraday"),
        "current_stage": current,
        "next_stage": next_stage,
        "checkpoints": checkpoints,
        "threshold_checks": checks,
        "blocker_deltas": blocker_deltas,
        "rationale": rationale,
        "review_ready": review_payload.get("review_status") == "approved" and not review_payload.get("retest_required"),
        "capital_stage_count": int(metrics.get("capital_stage_count") or 0),
        "shadow_trace_count": int(metrics.get("shadow_trace_count") or 0),
        "deviation_snapshot_count": len(deviation_snapshots or []),
    }


def build_acceptance_policy_snapshot(
    context: Mapping[str, Any],
    *,
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate governance policy, derived evidence, and promotion blockers."""

    strategy = dict(context.get("strategy") or {})
    metrics = dict(context.get("metrics") or {})
    evidence = dict(context.get("evidence") or {})
    prohibitions = dict(context.get("prohibitions") or {})
    review_payload = dict(review or {})
    capital_stages = list(context.get("capital_stages") or [])
    deviation_snapshots = list(context.get("deviation_snapshots") or [])

    strategy_type = str(strategy.get("strategy_type") or "intraday").lower()
    thresholds = dict(DEFAULT_THRESHOLDS.get(strategy_type, DEFAULT_THRESHOLDS["intraday"]))
    thresholds.update(metrics.get("policy_thresholds") or {})

    trade_count = int(metrics.get("trade_count") or 0)
    testing_days = int(metrics.get("testing_days") or 0)
    traded_day_count = int(metrics.get("traded_day_count") or 0)
    api_error_rate = metrics.get("api_error_rate")
    avg_slippage = metrics.get("average_slippage")
    fill_rate = metrics.get("fill_rate")
    rejection_ratio = metrics.get("rejection_ratio")
    drawdown_abs = abs(float(metrics.get("max_drawdown") or 0))
    paper_live_deviation = metrics.get("paper_live_max_deviation_ratio")
    capital_stage_count = int(metrics.get("capital_stage_count") or 0)
    regime_combo_count = int(metrics.get("regime_combo_count") or 0)
    session_bucket_count = int(metrics.get("session_bucket_count") or 0)
    volatility_bucket_count = int(metrics.get("volatility_bucket_count") or 0)
    liquidity_bucket_count = int(metrics.get("liquidity_bucket_count") or 0)
    regime_coverage_score = metrics.get("regime_coverage_score")

    enough_market_conditions = (
        regime_coverage_score is not None
        and float(regime_coverage_score) >= thresholds["min_regime_coverage_score"]
        and regime_combo_count >= 3
        and volatility_bucket_count >= 2
        and liquidity_bucket_count >= 2
        and (session_bucket_count >= 2 if strategy_type == "intraday" else session_bucket_count >= 1)
    )

    derived_evidence: dict[str, dict[str, bool]] = {}

    shadow_trace_count = int(metrics.get("shadow_trace_count") or 0)
    shared_architecture = bool(strategy.get("shared_live_architecture") or shadow_trace_count > 0 or strategy.get("shadow_trading_used"))
    shadow_checks = _checks_for_gate(evidence, "shadow_trading")
    def _ratio_ok(key: str, minimum: float = 0.8) -> bool:
        value = metrics.get(key)
        if value is None:
            return False
        return float(value) >= minimum
    derived_evidence["shadow_trading"] = {
        "live_market_data_source": shadow_checks.get("live_market_data_source") is True or _ratio_ok("shadow_market_data_shared_ratio"),
        "live_signal_process": shadow_checks.get("live_signal_process") is True or _ratio_ok("shadow_signal_process_shared_ratio"),
        "live_risk_module": shadow_checks.get("live_risk_module") is True or _ratio_ok("shadow_risk_module_shared_ratio"),
        "live_order_generation": shadow_checks.get("live_order_generation") is True or _ratio_ok("shadow_order_generation_shared_ratio"),
        "live_logging_alerting": shadow_checks.get("live_logging_alerting") is True or _ratio_ok("shadow_logging_alerting_shared_ratio"),
        "no_exchange_submission": shadow_checks.get("no_exchange_submission") is True or _ratio_ok("shadow_no_exchange_submission_ratio", 1.0),
        "theoretical_submission_time": shadow_checks.get("theoretical_submission_time") is True or metrics.get("shadow_avg_intent_to_adapter_ms") is not None,
        "order_book_snapshot_recorded": shadow_checks.get("order_book_snapshot_recorded") is True or _ratio_ok("shadow_order_book_snapshot_ratio"),
        "likely_execution_price": shadow_checks.get("likely_execution_price") is True or _ratio_ok("shadow_likely_execution_price_ratio"),
        "post_order_price_behavior": shadow_checks.get("post_order_price_behavior") is True or _ratio_ok("shadow_post_order_price_behavior_ratio"),
    }

    derived_evidence["sample_size_period"] = {
        "complete_trading_cycle": testing_days >= thresholds["min_testing_days"] and traded_day_count >= max(5, thresholds["min_testing_days"] // 3),
        "sufficient_trade_samples": trade_count >= thresholds["min_trade_count"],
        "enough_market_conditions": enough_market_conditions,
        "not_only_one_way_market": metrics.get("win_rate") not in (None, 0.0, 1.0),
        "vol_expansion_contraction": int(metrics.get("high_vol_trade_count") or 0) > 0 and int(metrics.get("low_vol_trade_count") or 0) > 0,
        "no_trade_periods": int(metrics.get("idle_day_count") or 0) > 0,
        "consecutive_loss_periods": int(metrics.get("max_consecutive_losses") or 0) >= 2,
        "weak_liquidity_periods": int(metrics.get("thin_liquidity_trade_count") or 0) > 0,
    }

    derived_evidence["research_discipline"] = {
        "strategy_logic_frozen": bool(strategy.get("logic_frozen", True)) and int(metrics.get("logic_change_count") or 0) == 0 and int(metrics.get("freeze_violation_count") or 0) == 0,
        "strategy_parameters_frozen": bool(metrics.get("parameters_frozen")) and int(metrics.get("freeze_violation_count") or 0) == 0,
        "risk_parameters_frozen": bool(strategy.get("risk_parameters_frozen", True)) and int(metrics.get("risk_change_count") or 0) == 0 and int(metrics.get("freeze_violation_count") or 0) == 0,
        "no_short_term_parameter_tuning": int(metrics.get("parameter_change_count") or 0) == 0 and int(metrics.get("freeze_violation_count") or 0) == 0,
        "modifications_restart_stats": float(metrics.get("restart_stats_completion_ratio") or 0) >= 1.0,
        "modification_reasons_recorded": float(metrics.get("governance_reason_coverage_ratio") or 0) >= 1.0,
        "failed_versions_retained": bool(strategy.get("failed_versions_retained", True)),
        "no_selective_best_result_retention": bool(strategy.get("retain_all_variants", True)),
        "version_result_mapping": (
            bool(strategy.get("strategy_version") and strategy.get("parameter_version"))
            or float(metrics.get("governance_version_mapping_ratio") or 0) >= 1.0
        ),
    }

    derived_evidence["api_security"] = {
        "minimum_permissions": bool(metrics.get("api_key_permissions_minimized", True)),
        "unneeded_permissions_disabled": not bool(metrics.get("withdrawal_permission_enabled")),
        "withdrawal_disabled": not bool(metrics.get("withdrawal_permission_enabled")),
        "ip_whitelist": bool(metrics.get("ip_whitelist", True)),
        "no_hardcoded_keys": not bool(metrics.get("hardcoded_api_keys")),
        "secrets_storage": bool(strategy.get("secrets_storage", True)),
        "logs_avoid_secrets": bool(metrics.get("logs_avoid_secrets", True)),
        "test_live_keys_separated": bool(metrics.get("test_live_keys_separated", True)),
        "revocation_process": bool(metrics.get("revocation_process", True)),
    }

    derived_evidence["capacity_scaling"] = {
        "order_size_vs_book_depth": metrics.get("market_impact") is not None,
        "order_size_vs_recent_volume": metrics.get("fill_rate") is not None,
        "total_position_vs_capacity": capital_stage_count >= thresholds["min_capital_stage_count"],
        "slippage_reestimated_after_scaling": metrics.get("average_slippage") is not None,
        "fill_rate_reestimated_after_scaling": metrics.get("fill_rate") is not None,
        "drawdown_reestimated_after_scaling": metrics.get("max_drawdown") is not None,
        "predefined_scaling_multiple": capital_stage_count >= thresholds["min_capital_stage_count"],
        "observation_after_scaling": trade_count > 0,
        "scaling_stop_conditions": bool(strategy.get("scaling_stop_conditions", True)),
    }

    paper_live_ready = paper_live_deviation is None or float(paper_live_deviation) <= thresholds["max_paper_live_deviation"]
    derived_evidence["paper_live_comparison"] = {
        "slippage_comparison": metrics.get("average_slippage") is not None,
        "fill_rate_comparison": metrics.get("fill_rate") is not None,
        "rejection_rate_comparison": metrics.get("rejection_ratio") is not None,
        "api_latency_comparison": metrics.get("average_api_latency") is not None,
        "holding_time_comparison": metrics.get("average_holding_minutes") is not None,
        "trade_frequency_comparison": metrics.get("paper_live_comparison_ready") is True or metrics.get("live_trade_count", 0) == 0,
        "win_rate_comparison": metrics.get("win_rate") is not None,
        "drawdown_comparison": metrics.get("max_drawdown") is not None,
        "cost_erosion_comparison": metrics.get("total_fees") is not None and metrics.get("total_slippage") is not None and paper_live_ready,
    }

    threshold_flags = {
        "stability_threshold": (metrics.get("runtime_days") or 0) >= thresholds["min_testing_days"],
        "order_tracking_threshold": trade_count >= thresholds["min_trade_count"],
        "reconciliation_threshold": bool(metrics.get("reconciliation_implemented")),
        "fee_threshold": bool(metrics.get("fees_included")),
        "slippage_threshold": avg_slippage is not None and float(avg_slippage) <= thresholds["max_average_slippage_bps"],
        "limit_fill_threshold": fill_rate is not None and float(fill_rate) >= thresholds["min_fill_rate"],
        "api_error_threshold": api_error_rate is not None and float(api_error_rate) <= thresholds["max_api_error_rate"],
        "risk_control_threshold": bool(metrics.get("kill_switch_tested")),
        "kill_switch_threshold": bool(metrics.get("kill_switch_tested")),
        "sample_size_threshold": trade_count >= thresholds["min_trade_count"] and enough_market_conditions,
        "behavior_deviation_threshold": paper_live_ready,
        "logging_threshold": bool(metrics.get("major_error_count") is not None),
        "alerting_threshold": bool(metrics.get("alert_delivery_count") or metrics.get("alert_count")),
        "capacity_threshold": capital_stage_count >= thresholds["min_capital_stage_count"],
    }
    derived_evidence["quantitative_thresholds"] = threshold_flags
    if metrics.get("threshold_profile_active"):
        derived_evidence["quantitative_thresholds"]["threshold_profile_active"] = True
        derived_evidence["quantitative_thresholds"]["threshold_profile_approved"] = bool(metrics.get("threshold_profile_approved"))

    blockers: list[str] = []
    if not shared_architecture:
        blockers.append("2.2 shared_architecture_missing")
    if shadow_trace_count > 0 and not _all_truthy(
        derived_evidence["shadow_trading"],
        (
            "live_market_data_source",
            "live_signal_process",
            "live_risk_module",
            "live_order_generation",
            "live_logging_alerting",
            "no_exchange_submission",
        ),
    ):
        blockers.append("14 shadow_parity_incomplete")
    if not _all_truthy(
        derived_evidence["sample_size_period"],
        ("complete_trading_cycle", "sufficient_trade_samples", "enough_market_conditions"),
    ):
        blockers.append("15 sample_size_or_market_regime_insufficient")
    if not _all_truthy(derived_evidence["research_discipline"], ("strategy_parameters_frozen", "no_short_term_parameter_tuning", "version_result_mapping")):
        blockers.append("16 research_discipline_incomplete")
    if not _all_truthy(derived_evidence["api_security"], ("no_hardcoded_keys", "withdrawal_disabled", "test_live_keys_separated")):
        blockers.append("17 api_security_incomplete")
    if not threshold_flags["api_error_threshold"]:
        blockers.append("20 api_error_threshold_failed")
    if not threshold_flags["slippage_threshold"]:
        blockers.append("20 slippage_threshold_failed")
    if not threshold_flags["limit_fill_threshold"]:
        blockers.append("20 fill_rate_threshold_failed")
    if metrics.get("threshold_profile_active") and not metrics.get("threshold_profile_approved"):
        blockers.append("20 threshold_profile_unapproved")
    if any(bool(value) for value in prohibitions.values()):
        blockers.append("21 prohibition_flags_present")
    if review_payload and review_payload.get("review_status") == "blocked":
        blockers.append("review blocked")
    if review_payload and review_payload.get("retest_required"):
        blockers.append("review retest_required")

    recommend = "paper"
    if not blockers and review_payload.get("can_promote_to_live") and review_payload.get("review_status") == "approved":
        recommend = "small_live"
    elif not blockers:
        recommend = "shadow"

    promotion_ladder = _build_promotion_ladder(
        strategy=strategy,
        metrics=metrics,
        thresholds=thresholds,
        blockers=blockers,
        review_payload=review_payload,
        capital_stages=capital_stages,
        deviation_snapshots=deviation_snapshots,
        shared_architecture=shared_architecture,
        paper_live_ready=paper_live_ready,
    )
    promotion_ladder["decision"] = recommend

    return {
        "thresholds": thresholds,
        "evidence": derived_evidence,
        "blockers": blockers,
        "recommendation": recommend,
        "promotion_ladder": promotion_ladder,
        "shared_architecture_ready": shared_architecture,
        "paper_live_deviation_ok": paper_live_ready,
        "review_ready": review_payload.get("review_status") == "approved" and not review_payload.get("retest_required"),
        "can_promote": recommend == "small_live",
    }


__all__ = [
    "DEFAULT_THRESHOLDS",
    "build_acceptance_policy_snapshot",
]
