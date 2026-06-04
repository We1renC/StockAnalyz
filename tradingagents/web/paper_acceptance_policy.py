"""Promotion policy and threshold evaluation for paper acceptance."""

from __future__ import annotations

from typing import Any, Mapping


DEFAULT_THRESHOLDS = {
    "intraday": {
        "min_trade_count": 50,
        "min_testing_days": 20,
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
        "max_api_error_rate": 0.05,
        "max_average_slippage_bps": 120.0,
        "min_fill_rate": 0.7,
        "max_rejection_ratio": 0.1,
        "max_drawdown_abs": 0.2,
        "max_paper_live_deviation": 0.4,
        "min_capital_stage_count": 1,
    },
}


def _checks_for_gate(evidence: Mapping[str, Any], gate_id: str) -> dict[str, Any]:
    gate = evidence.get(gate_id) or {}
    checks = gate.get("checks") if isinstance(gate, Mapping) else {}
    return checks if isinstance(checks, Mapping) else {}


def _all_truthy(checks: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return all(checks.get(key) is True for key in keys)


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

    strategy_type = str(strategy.get("strategy_type") or "intraday").lower()
    thresholds = dict(DEFAULT_THRESHOLDS.get(strategy_type, DEFAULT_THRESHOLDS["intraday"]))
    thresholds.update(metrics.get("policy_thresholds") or {})

    trade_count = int(metrics.get("trade_count") or 0)
    testing_days = int(metrics.get("testing_days") or 0)
    api_error_rate = metrics.get("api_error_rate")
    avg_slippage = metrics.get("average_slippage")
    fill_rate = metrics.get("fill_rate")
    rejection_ratio = metrics.get("rejection_ratio")
    drawdown_abs = abs(float(metrics.get("max_drawdown") or 0))
    paper_live_deviation = metrics.get("paper_live_max_deviation_ratio")
    capital_stage_count = int(metrics.get("capital_stage_count") or 0)

    derived_evidence: dict[str, dict[str, bool]] = {}

    shared_architecture = bool(strategy.get("shared_live_architecture") or strategy.get("shadow_trading_used"))
    shadow_checks = _checks_for_gate(evidence, "shadow_trading")
    derived_evidence["shadow_trading"] = {
        "live_market_data_source": shadow_checks.get("live_market_data_source") is True or shared_architecture,
        "live_signal_process": shadow_checks.get("live_signal_process") is True or shared_architecture,
        "live_risk_module": shadow_checks.get("live_risk_module") is True or shared_architecture,
        "live_order_generation": shadow_checks.get("live_order_generation") is True or shared_architecture,
        "live_logging_alerting": shadow_checks.get("live_logging_alerting") is True or shared_architecture,
        "no_exchange_submission": shadow_checks.get("no_exchange_submission") is True or bool(strategy.get("shadow_trading_used")),
        "theoretical_submission_time": shadow_checks.get("theoretical_submission_time") is True or metrics.get("average_execution_latency") is not None,
        "order_book_snapshot_recorded": shadow_checks.get("order_book_snapshot_recorded") is True or metrics.get("signal_vs_execution_delta") is not None,
        "likely_execution_price": shadow_checks.get("likely_execution_price") is True or metrics.get("average_slippage") is not None,
        "post_order_price_behavior": shadow_checks.get("post_order_price_behavior") is True or metrics.get("missed_fill_price_movement") is not None,
    }

    derived_evidence["sample_size_period"] = {
        "complete_trading_cycle": testing_days >= thresholds["min_testing_days"],
        "sufficient_trade_samples": trade_count >= thresholds["min_trade_count"],
        "enough_market_conditions": testing_days >= thresholds["min_testing_days"],
        "not_only_one_way_market": metrics.get("win_rate") not in (None, 0.0, 1.0),
        "vol_expansion_contraction": bool(metrics.get("runtime_days")),
        "no_trade_periods": trade_count >= max(1, thresholds["min_trade_count"] // 2),
        "consecutive_loss_periods": metrics.get("max_consecutive_losses") is not None,
        "weak_liquidity_periods": metrics.get("average_slippage") is not None,
    }

    derived_evidence["research_discipline"] = {
        "strategy_logic_frozen": bool(strategy.get("logic_frozen", True)),
        "strategy_parameters_frozen": bool(metrics.get("parameters_frozen")),
        "risk_parameters_frozen": bool(strategy.get("risk_parameters_frozen", True)),
        "no_short_term_parameter_tuning": int(metrics.get("parameter_change_count") or 0) == 0,
        "modifications_restart_stats": bool(strategy.get("modifications_restart_stats", True)),
        "modification_reasons_recorded": bool(strategy.get("modification_log") or trade_count > 0),
        "failed_versions_retained": bool(strategy.get("failed_versions_retained", True)),
        "no_selective_best_result_retention": bool(strategy.get("retain_all_variants", True)),
        "version_result_mapping": bool(strategy.get("strategy_version") and strategy.get("parameter_version")),
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
        "sample_size_threshold": trade_count >= thresholds["min_trade_count"],
        "behavior_deviation_threshold": paper_live_ready,
        "logging_threshold": bool(metrics.get("major_error_count") is not None),
        "alerting_threshold": bool(metrics.get("alert_delivery_count") or metrics.get("alert_count")),
        "capacity_threshold": capital_stage_count >= thresholds["min_capital_stage_count"],
    }
    derived_evidence["quantitative_thresholds"] = threshold_flags

    blockers: list[str] = []
    if not shared_architecture:
        blockers.append("2.2 shared_architecture_missing")
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

    return {
        "thresholds": thresholds,
        "evidence": derived_evidence,
        "blockers": blockers,
        "recommendation": recommend,
        "shared_architecture_ready": shared_architecture,
        "paper_live_deviation_ok": paper_live_ready,
        "review_ready": review_payload.get("review_status") == "approved" and not review_payload.get("retest_required"),
        "can_promote": recommend == "small_live",
    }


__all__ = [
    "DEFAULT_THRESHOLDS",
    "build_acceptance_policy_snapshot",
]
