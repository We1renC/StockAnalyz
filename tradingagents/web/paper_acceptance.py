"""Paper-trading acceptance gates for quant strategy promotion.

The module turns the paper-trading acceptance standard into a deterministic
reporting contract. It does not place orders. It evaluates evidence collected
from backtests, paper journals, shadow orders, reconciliation, monitoring, and
risk controls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean, pstdev
from typing import Any, Iterable, Literal, Mapping, Optional


GateStatus = Literal["pass", "partial", "fail", "unavailable", "not_applicable"]
Conclusion = Literal[
    "passed",
    "conditionally_passed",
    "failed_repeat_paper",
    "strategy_invalidated",
]


PASSING_STATUSES: set[str] = {"pass", "not_applicable"}
NON_PASSING_STATUSES: set[str] = {"partial", "fail", "unavailable"}


@dataclass(frozen=True)
class AcceptanceGateDefinition:
    """Definition of one acceptance gate from the standard."""

    gate_id: str
    section: str
    title: str
    passing_standard: str
    evidence_keys: tuple[str, ...]
    blocking: bool = True
    optional_when: Optional[str] = None


ACCEPTANCE_GATES: tuple[AcceptanceGateDefinition, ...] = (
    AcceptanceGateDefinition(
        "strategy_logic",
        "3.1",
        "Strategy Logic Check",
        "Logic is clearly describable, reproducible, verifiable, fixed, and lookahead-safe.",
        (
            "entry_conditions",
            "exit_conditions",
            "stop_loss_conditions",
            "take_profit_conditions",
            "no_trade_conditions",
            "parameters_frozen",
            "no_future_data",
            "unfinished_candle_policy",
            "oos_test_completed",
        ),
    ),
    AcceptanceGateDefinition(
        "instrument_liquidity",
        "3.2",
        "Trading Instrument Check",
        "Expected order size must not significantly exceed available liquidity.",
        (
            "volume_sufficient",
            "spread_acceptable",
            "order_book_depth_sufficient",
            "liquidity_dry_up_checked",
            "expected_profit_gt_cost",
        ),
    ),
    AcceptanceGateDefinition(
        "data_source_traceability",
        "3.3",
        "Data Source Check",
        "Every signal can be replayed from timestamped, traceable market data.",
        (
            "market_data_source",
            "timestamps_recorded",
            "latency_handled",
            "missing_data_handled",
            "duplicate_data_handled",
            "out_of_order_data_handled",
            "reconnect_backfill",
            "clock_sync",
        ),
    ),
    AcceptanceGateDefinition(
        "market_execution_model",
        "4.1",
        "Market Order Execution Model",
        "Market orders are simulated with conservative book-side VWAP, depth, and partial-fill logic.",
        (
            "ask_bid_depth_used",
            "multi_level_book_consumption",
            "vwap_execution_price",
            "insufficient_depth_policy",
            "market_slippage_records",
        ),
    ),
    AcceptanceGateDefinition(
        "limit_execution_model",
        "4.2",
        "Limit Order Execution Model",
        "Limit fills do not rely on touch-price equals filled assumptions.",
        (
            "queue_position_considered",
            "volume_at_level_considered",
            "partial_fills_supported",
            "unfilled_orders_supported",
            "timeout_cancel_supported",
            "post_only_rejection_supported",
            "fill_rate_measured",
            "adverse_selection_measured",
        ),
    ),
    AcceptanceGateDefinition(
        "slippage_market_impact",
        "4.3",
        "Slippage and Market Impact Check",
        "The strategy remains positive expectancy after dynamic slippage and market impact are deducted.",
        (
            "signal_price_recorded",
            "theoretical_execution_price_recorded",
            "simulated_execution_price_recorded",
            "slippage_recorded",
            "spread_adjusted_slippage",
            "depth_adjusted_slippage",
            "size_adjusted_slippage",
            "volatility_adjusted_slippage",
        ),
    ),
    AcceptanceGateDefinition(
        "fees",
        "5.1",
        "Fee Check",
        "Performance is evaluated on net equity after maker/taker and relevant fees are deducted.",
        (
            "maker_taker_distinguished",
            "fee_schedule_configured",
            "pair_fee_differences_considered",
            "fee_recorded_per_trade",
            "gross_and_net_profit_reported",
        ),
    ),
    AcceptanceGateDefinition(
        "derivatives_costs",
        "5.2",
        "Additional Cost Checks for Derivatives Trading",
        "Funding, leverage, margin, mark price, and liquidation risk are explicitly controlled.",
        (
            "funding_rates_included",
            "leverage_included",
            "margin_usage_included",
            "liquidation_price_calculated",
            "funding_reflected_in_equity",
            "liquidation_stress_tested",
        ),
        optional_when="non_derivative",
    ),
    AcceptanceGateDefinition(
        "order_lifecycle",
        "6.1",
        "Order State Check",
        "Every order is traceable, replayable, auditable, and supports the full state lifecycle.",
        (
            "unique_order_id",
            "client_order_id",
            "strategy_parameter_versions",
            "order_fields_recorded",
            "new_state_supported",
            "partial_fill_state_supported",
            "filled_state_supported",
            "cancel_reject_expire_supported",
            "unknown_state_supported",
        ),
    ),
    AcceptanceGateDefinition(
        "unknown_order_state",
        "6.2",
        "Unknown Order State Check",
        "Unknown state stops risk expansion and triggers reconciliation instead of blind resubmission.",
        (
            "timeout_simulated",
            "no_confirmation_simulated",
            "no_blind_resend",
            "client_order_id_query",
            "unknown_state_suspends_trading",
            "unknown_state_alerts",
        ),
    ),
    AcceptanceGateDefinition(
        "virtual_account",
        "7.1",
        "Virtual Account Check",
        "The paper account resembles a real account and cannot create invalid balances or orders.",
        (
            "initial_capital_defined",
            "multi_currency_balances",
            "available_and_frozen_balance",
            "realized_unrealized_pnl",
            "fee_deduction",
            "position_market_value",
            "equity_curve",
            "insufficient_balance_rejection",
            "minimum_notional_enforced",
            "precision_rules_enforced",
        ),
    ),
    AcceptanceGateDefinition(
        "reconciliation",
        "7.2",
        "Reconciliation Check",
        "Balance, position, order, and trade differences are explainable, traceable, and correctable.",
        (
            "order_state_compared",
            "position_state_compared",
            "balance_state_compared",
            "trade_records_compared",
            "reconciliation_frequency_defined",
            "differences_marked",
            "major_differences_suspend_trading",
            "reconciliation_logged",
            "reconciliation_alerts",
        ),
    ),
    AcceptanceGateDefinition(
        "api_rate_limits",
        "8.1",
        "API Rate Limit Check",
        "Rate limits are centrally controlled with backoff, priority, and bounded retry behavior.",
        (
            "global_request_weight_management",
            "order_count_management",
            "central_control_for_shared_api",
            "backoff_on_rate_limit",
            "bounded_retries",
            "request_priority_rules",
            "api_latency_recorded",
            "api_error_rate_recorded",
            "api_abnormality_pauses_strategy",
        ),
    ),
    AcceptanceGateDefinition(
        "latency",
        "8.2",
        "Latency Check",
        "The system records latency distributions and pauses when latency exceeds tolerance.",
        (
            "market_data_latency",
            "signal_compute_time",
            "order_request_latency",
            "exchange_response_latency",
            "database_write_latency",
            "loop_runtime",
            "p95_p99_latency",
            "latency_alerts",
            "latency_pause_policy",
        ),
    ),
    AcceptanceGateDefinition(
        "system_stability",
        "8.3",
        "System Stability Check",
        "The system can run unattended for at least seven days without manual repair.",
        (
            "seven_day_runtime",
            "memory_stable",
            "cpu_stable",
            "disk_space_sufficient",
            "bounded_logs",
            "db_connections_stable",
            "websocket_reconnect",
            "restart_state_recovery",
            "scheduled_tasks_ok",
            "time_sync",
        ),
    ),
    AcceptanceGateDefinition(
        "network_abnormality",
        "9.1",
        "Network Abnormality Check",
        "Network failures do not cause duplicate orders, incorrect positions, or uncontrolled trading.",
        (
            "ws_disconnect_simulated",
            "rest_timeout_simulated",
            "dns_failure_simulated",
            "network_outage_simulated",
            "latency_spike_simulated",
            "exchange_errors_simulated",
            "reconnect_works",
            "backfill_works",
            "pause_during_recovery",
            "reconcile_after_recovery",
        ),
    ),
    AcceptanceGateDefinition(
        "market_abnormality",
        "9.2",
        "Market Abnormality Check",
        "Market abnormalities may lose money but must not cause loss of control.",
        (
            "price_jump_simulated",
            "spread_widening_simulated",
            "depth_disappearance_simulated",
            "volume_collapse_simulated",
            "one_way_market_simulated",
            "consecutive_stop_losses_simulated",
            "missed_fill_simulated",
            "excess_slippage_simulated",
            "volatility_size_reduction",
            "liquidity_stop_orders",
        ),
    ),
    AcceptanceGateDefinition(
        "program_abnormality",
        "9.3",
        "Program Abnormality Check",
        "Program failures must stop safely and preserve logs instead of expanding trading risk.",
        (
            "strategy_crash_simulated",
            "db_write_failure_simulated",
            "config_read_failure_simulated",
            "oom_simulated",
            "disk_exhaustion_simulated",
            "duplicate_startup_simulated",
            "bad_parameters_simulated",
            "delisted_symbol_simulated",
            "precision_violation_simulated",
            "safe_stop",
            "error_logs_preserved",
        ),
    ),
    AcceptanceGateDefinition(
        "risk_control_priority",
        "10.1",
        "Risk Control Priority",
        "Risk control has higher authority than strategy logic and cannot be bypassed.",
        (
            "signals_cannot_bypass_risk",
            "orders_pass_risk_before_submission",
            "risk_reject_not_resubmitted",
            "risk_stop_not_strategy_restarted",
            "risk_params_versioned",
            "risk_events_logged",
            "risk_events_alerted",
        ),
    ),
    AcceptanceGateDefinition(
        "position_risk",
        "10.2",
        "Position Risk Check",
        "New orders are rejected when order, position, leverage, margin, or exposure limits are exceeded.",
        (
            "max_order_size",
            "max_position_per_pair",
            "total_position_limit",
            "max_leverage",
            "max_margin_usage",
            "max_open_orders",
            "max_scaling_operations",
            "directional_exposure_limit",
            "correlated_exposure_limit",
            "limit_rejection_tested",
        ),
    ),
    AcceptanceGateDefinition(
        "loss_risk",
        "10.3",
        "Loss Risk Check",
        "Loss limits are defined, tested, and stop new trades when reached.",
        (
            "max_loss_per_trade",
            "max_daily_loss",
            "max_weekly_loss",
            "max_total_drawdown",
            "max_consecutive_losses",
            "erroneous_trade_limit",
            "shutdown_conditions",
            "loss_limit_stops_new_trades",
            "shutdown_events_recorded",
        ),
    ),
    AcceptanceGateDefinition(
        "kill_switch",
        "10.4",
        "Kill Switch Check",
        "Manual and automatic shutdown have been tested and block new orders until confirmed restart.",
        (
            "manual_shutdown",
            "automatic_shutdown",
            "new_orders_blocked_after_shutdown",
            "cancel_open_orders_after_shutdown",
            "position_retention_or_flatten_policy",
            "shutdown_logged",
            "shutdown_alerted",
            "manual_restart_confirmation",
            "shutdown_test_successful",
        ),
    ),
    AcceptanceGateDefinition(
        "monitoring_dashboard",
        "11.1",
        "Monitoring Dashboard Check",
        "The operator can quickly determine account, strategy, data, API, and risk health.",
        (
            "real_time_equity",
            "available_balance",
            "current_positions",
            "open_orders",
            "daily_total_pnl",
            "max_drawdown_displayed",
            "strategy_status",
            "data_connection_status",
            "api_error_rate",
            "recent_trades",
            "risk_control_status",
        ),
    ),
    AcceptanceGateDefinition(
        "alerting",
        "11.2",
        "Alerting Mechanism Check",
        "Major abnormalities actively notify the operator with reason, timestamp, strategy, symbol, and severity.",
        (
            "start_stop_notifications",
            "order_failure_notifications",
            "api_error_notifications",
            "ws_disconnect_notifications",
            "reconciliation_notifications",
            "slippage_notifications",
            "loss_warning_notifications",
            "kill_switch_notifications",
            "crash_notifications",
            "alert_payload_complete",
        ),
    ),
    AcceptanceGateDefinition(
        "performance_metrics",
        "12.1",
        "Basic Performance Metrics",
        "Return is evaluated together with net profit, costs, drawdown, ratios, trade count, and streaks.",
        (
            "total_return",
            "net_profit",
            "gross_profit",
            "total_fees",
            "total_slippage",
            "max_drawdown",
            "win_rate",
            "average_win_loss",
            "profit_factor",
            "sharpe_sortino_calmar",
            "trade_count",
            "average_holding_time",
            "max_consecutive_losses_wins",
        ),
    ),
    AcceptanceGateDefinition(
        "trade_quality",
        "12.2",
        "Trade Quality Metrics",
        "Profitability is not mainly caused by idealized execution assumptions.",
        (
            "average_slippage",
            "maximum_slippage",
            "slippage_std",
            "average_execution_latency",
            "maximum_execution_latency",
            "fill_rate",
            "partial_fill_ratio",
            "cancellation_ratio",
            "rejection_ratio",
            "timeout_ratio",
            "limit_waiting_time",
            "market_impact",
            "signal_vs_execution_delta",
            "missed_fill_price_movement",
        ),
    ),
    AcceptanceGateDefinition(
        "behavior_deviation",
        "13",
        "Strategy Behavior Deviation Check",
        "Paper behavior is broadly consistent with backtest and research assumptions.",
        (
            "trade_frequency_matches",
            "win_rate_matches",
            "win_loss_ratio_matches",
            "holding_time_matches",
            "drawdown_matches",
            "slippage_not_materially_higher",
            "fill_rate_not_materially_lower",
            "no_abnormal_regime_trading",
            "no_small_extreme_event_dependency",
            "risk_profile_stable_after_losses",
        ),
    ),
    AcceptanceGateDefinition(
        "shadow_trading",
        "14",
        "Shadow Trading Check",
        "Shadow mode shares live architecture except final exchange submission.",
        (
            "live_market_data_source",
            "live_signal_process",
            "live_risk_module",
            "live_order_generation",
            "live_logging_alerting",
            "no_exchange_submission",
            "theoretical_submission_time",
            "order_book_snapshot_recorded",
            "likely_execution_price",
            "post_order_price_behavior",
        ),
        blocking=False,
    ),
    AcceptanceGateDefinition(
        "sample_size_period",
        "15",
        "Sample Size and Testing Period Check",
        "The sample is sufficient for the strategy type and market regimes.",
        (
            "complete_trading_cycle",
            "sufficient_trade_samples",
            "enough_market_conditions",
            "not_only_one_way_market",
            "vol_expansion_contraction",
            "no_trade_periods",
            "consecutive_loss_periods",
            "weak_liquidity_periods",
        ),
    ),
    AcceptanceGateDefinition(
        "research_discipline",
        "16",
        "Research Discipline and Secondary Overfitting",
        "Logic, parameters, and risk controls are frozen; modifications restart statistics.",
        (
            "strategy_logic_frozen",
            "strategy_parameters_frozen",
            "risk_parameters_frozen",
            "no_short_term_parameter_tuning",
            "modifications_restart_stats",
            "modification_reasons_recorded",
            "failed_versions_retained",
            "no_selective_best_result_retention",
            "version_result_mapping",
        ),
    ),
    AcceptanceGateDefinition(
        "api_security",
        "17",
        "API Key and Permission Security Check",
        "API permissions are minimized, separated by environment, not hardcoded, and revocable.",
        (
            "minimum_permissions",
            "unneeded_permissions_disabled",
            "withdrawal_disabled",
            "ip_whitelist",
            "no_hardcoded_keys",
            "secrets_storage",
            "logs_avoid_secrets",
            "test_live_keys_separated",
            "revocation_process",
        ),
    ),
    AcceptanceGateDefinition(
        "capacity_scaling",
        "18",
        "Capacity and Capital Scaling Rules",
        "Capital scaling is staged and rechecks slippage, fill rate, drawdown, and stability.",
        (
            "order_size_vs_book_depth",
            "order_size_vs_recent_volume",
            "total_position_vs_capacity",
            "slippage_reestimated_after_scaling",
            "fill_rate_reestimated_after_scaling",
            "drawdown_reestimated_after_scaling",
            "predefined_scaling_multiple",
            "observation_after_scaling",
            "scaling_stop_conditions",
        ),
    ),
    AcceptanceGateDefinition(
        "paper_live_comparison",
        "19",
        "Paper vs Small-Scale Live Trading Comparison",
        "Paper results become the benchmark for small-scale live deviation analysis.",
        (
            "slippage_comparison",
            "fill_rate_comparison",
            "rejection_rate_comparison",
            "api_latency_comparison",
            "holding_time_comparison",
            "trade_frequency_comparison",
            "win_rate_comparison",
            "drawdown_comparison",
            "cost_erosion_comparison",
        ),
        blocking=False,
        optional_when="pre_live",
    ),
    AcceptanceGateDefinition(
        "quantitative_thresholds",
        "20",
        "Quantitative Passing and Failing Thresholds",
        "The system has explicit thresholds for stability, tracking, reconciliation, costs, risk, samples, logging, alerts, and capacity.",
        (
            "stability_threshold",
            "order_tracking_threshold",
            "reconciliation_threshold",
            "fee_threshold",
            "slippage_threshold",
            "limit_fill_threshold",
            "api_error_threshold",
            "risk_control_threshold",
            "kill_switch_threshold",
            "sample_size_threshold",
            "behavior_deviation_threshold",
            "logging_threshold",
            "alerting_threshold",
            "capacity_threshold",
        ),
    ),
    AcceptanceGateDefinition(
        "final_report",
        "22",
        "Final Acceptance Report Format",
        "The report explicitly concludes whether the strategy may enter the next stage.",
        (
            "basic_information",
            "performance_summary",
            "trade_quality",
            "behavior_deviation",
            "system_stability",
            "risk_control_records",
            "security_check",
            "abnormal_events",
            "final_conclusion",
        ),
    ),
)


PROHIBITION_FLAGS: tuple[tuple[str, str], ...] = (
    ("fees_missing", "Paper trading is profitable, but transaction fees are not included."),
    ("slippage_missing", "Paper trading is profitable, but slippage is not included."),
    ("execution_model_idealized", "The execution model is overly idealized."),
    ("touch_equals_filled", "Limit orders assume touch price equals filled."),
    ("order_states_incomplete", "Order states cannot be fully tracked."),
    ("duplicate_orders", "The system has produced duplicate orders."),
    ("incorrect_positions", "The system has produced incorrect positions."),
    ("unexplained_reconciliation_diff", "Balance or position differences cannot be explained."),
    ("reconciliation_missing", "Reconciliation is not implemented."),
    ("risk_controls_untested", "Risk controls have not been tested."),
    ("kill_switch_untested", "The kill switch has not been tested."),
    ("api_errors_uncontrolled", "API errors can cause the strategy to lose control."),
    ("websocket_recovery_missing", "WebSocket disconnection cannot be recovered."),
    ("restart_state_inconsistent", "State becomes inconsistent after program restart."),
    ("repeated_parameter_changes", "The strategy only looks good after repeated parameter changes during paper trading."),
    ("sample_size_too_small", "The paper trading sample size is too small."),
    ("single_extreme_event_profit", "Profit mainly comes from a single extreme event."),
    ("averaging_down_without_limits", "The strategy averages down after losses without strict limits."),
    ("stop_condition_unclear", "The operator cannot clearly explain when the strategy should stop."),
    ("logging_incomplete", "Logging is incomplete."),
    ("alerting_missing", "Alerting is missing."),
    ("capital_or_loss_limits_missing", "There are no capital limits or loss limits."),
    ("api_permissions_excessive", "API key permissions are excessive or security controls are insufficient."),
    ("paper_live_comparison_missing", "Paper trading results cannot be compared with small-scale live trading results."),
)


SAMPLE_REQUIREMENTS = {
    "high_frequency": {"min_trades": 300, "min_days": 7},
    "intraday": {"min_trades": 50, "min_days": 28},
    "swing": {"min_trades": 20, "min_days": 60},
    "low_frequency": {"min_trades": 10, "min_days": 90},
}


def acceptance_gate_ids() -> list[str]:
    """Return stable gate ids in standard order."""

    return [gate.gate_id for gate in ACCEPTANCE_GATES]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "pass", "passed", "ok"}
    return bool(value)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _status_from_checks(checks: Mapping[str, Any], required_keys: Iterable[str]) -> GateStatus:
    keys = list(required_keys)
    if not keys:
        return "unavailable"
    present = [key for key in keys if key in checks]
    if not present:
        return "unavailable"
    passed = sum(1 for key in keys if _as_bool(checks.get(key)))
    if passed == len(keys):
        return "pass"
    if passed == 0:
        return "fail"
    return "partial"


def _gate_evidence(context: Mapping[str, Any], gate_id: str) -> dict[str, Any]:
    evidence = context.get("evidence") or {}
    raw = evidence.get(gate_id) or {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {"status": raw}


def _apply_optional_gate(definition: AcceptanceGateDefinition, context: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    strategy = context.get("strategy") or {}
    stage = (context.get("stage") or strategy.get("stage") or "paper").lower()
    instrument_type = (strategy.get("instrument_type") or strategy.get("asset_type") or "").lower()
    if definition.optional_when == "non_derivative" and instrument_type not in {"derivative", "futures", "perpetual"}:
        return {
            "status": "not_applicable",
            "reason": "Instrument is not marked as derivative/futures/perpetual.",
        }
    if definition.optional_when == "pre_live" and stage in {"paper", "paper_trading", "shadow"}:
        return {
            "status": "not_applicable",
            "reason": "Small-scale live comparison is required after paper acceptance, not before live data exists.",
        }
    return None


def _derived_status(definition: AcceptanceGateDefinition, context: Mapping[str, Any], checks: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    metrics = context.get("metrics") or {}
    strategy = context.get("strategy") or {}

    if definition.gate_id == "fees":
        fees_included = _as_bool(metrics["fees_included"]) if "fees_included" in metrics else _as_bool(checks.get("fee_recorded_per_trade"))
        total_fees = _num(metrics.get("total_fees"))
        status: GateStatus = "pass" if fees_included and total_fees is not None else "fail"
        return {"status": status, "reason": "Fees must be recorded and deducted from net performance."}

    if definition.gate_id == "slippage_market_impact":
        slippage_included = _as_bool(metrics["slippage_included"]) if "slippage_included" in metrics else _as_bool(checks.get("slippage_recorded"))
        expectancy = _num(metrics.get("expectancy_after_costs"))
        positive = expectancy is None or expectancy > 0
        status = "pass" if slippage_included and positive else "fail"
        return {"status": status, "reason": "Slippage must be included and not erase expectancy."}

    if definition.gate_id == "performance_metrics":
        trade_count = _num(metrics.get("trade_count"), 0) or 0
        required = ("net_profit", "gross_profit", "total_fees", "max_drawdown", "win_rate", "profit_factor")
        complete = all(metrics.get(key) is not None for key in required)
        expectancy = _num(metrics.get("expectancy_after_costs"))
        if trade_count <= 0:
            status = "unavailable"
        elif expectancy is not None and expectancy <= 0:
            status = "fail"
        else:
            status = "pass" if complete else "partial"
        return {"status": status, "reason": "Performance must be net of costs and include core risk metrics."}

    if definition.gate_id == "trade_quality":
        quality_keys = ("average_slippage", "maximum_slippage", "fill_rate", "rejection_ratio")
        status = "pass" if all(metrics.get(key) is not None for key in quality_keys) else _status_from_checks(checks, definition.evidence_keys)
        return {"status": status, "reason": "Execution quality metrics must be available for paper acceptance."}

    if definition.gate_id == "sample_size_period":
        strategy_type = (strategy.get("strategy_type") or "swing").lower()
        req = SAMPLE_REQUIREMENTS.get(strategy_type, SAMPLE_REQUIREMENTS["swing"])
        trade_count = int(_num(metrics.get("trade_count"), 0) or 0)
        days = int(_num(metrics.get("testing_days"), 0) or 0)
        status = "pass" if trade_count >= req["min_trades"] and days >= req["min_days"] else "fail"
        return {
            "status": status,
            "reason": f"{strategy_type} requires at least {req['min_trades']} trades and {req['min_days']} days.",
            "threshold": req,
        }

    if definition.gate_id == "reconciliation":
        abnormalities = int(_num(metrics.get("unresolved_reconciliation_count"), 0) or 0)
        implemented = _as_bool(checks.get("order_state_compared") or metrics.get("reconciliation_implemented"))
        status = "pass" if implemented and abnormalities == 0 else "fail"
        return {"status": status, "reason": "Unresolved reconciliation differences block promotion."}

    if definition.gate_id == "kill_switch":
        tested = _as_bool(checks.get("shutdown_test_successful") or metrics.get("kill_switch_tested"))
        status = "pass" if tested else "fail"
        return {"status": status, "reason": "A tested kill switch is a minimum live-trading requirement."}

    if definition.gate_id == "research_discipline":
        frozen = _as_bool(metrics.get("parameters_frozen") or checks.get("strategy_parameters_frozen"))
        changes = int(_num(metrics.get("parameter_change_count"), 0) or 0)
        status = "pass" if frozen and changes == 0 else "fail"
        return {"status": status, "reason": "Paper validation cannot reuse repeatedly tuned parameters."}

    if definition.gate_id == "api_security":
        hardcoded = _as_bool(metrics.get("hardcoded_api_keys"))
        withdrawal = _as_bool(metrics.get("withdrawal_permission_enabled"))
        status = "fail" if hardcoded or withdrawal else _status_from_checks(checks, definition.evidence_keys)
        return {"status": status, "reason": "Secrets and excessive permissions block live trading."}

    return None


def evaluate_gate(definition: AcceptanceGateDefinition, context: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one acceptance gate."""

    optional = _apply_optional_gate(definition, context)
    if optional:
        status: GateStatus = optional["status"]  # type: ignore[assignment]
        return {
            "id": definition.gate_id,
            "section": definition.section,
            "title": definition.title,
            "status": status,
            "blocking": False,
            "passing_standard": definition.passing_standard,
            "reason": optional.get("reason", ""),
            "missing_evidence": [],
            "evidence": {},
        }

    evidence = _gate_evidence(context, definition.gate_id)
    explicit_status = evidence.get("status")
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), Mapping) else evidence
    derived = _derived_status(definition, context, checks)
    if explicit_status in {"pass", "partial", "fail", "unavailable", "not_applicable"}:
        status = explicit_status
        reason = evidence.get("reason") or "Explicit evidence status."
    elif derived:
        status = derived["status"]
        reason = derived.get("reason", "")
    else:
        status = _status_from_checks(checks, definition.evidence_keys)
        reason = evidence.get("reason") or "Evaluated from checklist evidence."

    missing = [key for key in definition.evidence_keys if key not in checks or checks.get(key) in (None, "")]
    if status == "pass":
        missing = []

    return {
        "id": definition.gate_id,
        "section": definition.section,
        "title": definition.title,
        "status": status,
        "blocking": definition.blocking,
        "passing_standard": definition.passing_standard,
        "reason": reason,
        "missing_evidence": missing,
        "evidence": checks,
    }


def evaluate_prohibitions(context: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return Section 21 live-trading prohibition hits."""

    raw = context.get("prohibitions") or {}
    metrics = context.get("metrics") or {}
    merged = {**metrics, **raw}
    hits = []
    for flag, description in PROHIBITION_FLAGS:
        if _as_bool(merged.get(flag)):
            hits.append({"flag": flag, "description": description})
    return hits


def _compute_metric_summary(context: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(context.get("metrics") or {})
    trades = list(context.get("trades") or [])
    if trades and "trade_count" not in metrics:
        metrics["trade_count"] = len(trades)
    if trades:
        r_values = [_num(t.get("r_multiple")) for t in trades]
        r_values = [v for v in r_values if v is not None]
        pnl_values = [_num(t.get("pnl")) for t in trades]
        pnl_values = [v for v in pnl_values if v is not None]
        slip_values = [_num(t.get("slippage")) for t in trades]
        slip_values = [v for v in slip_values if v is not None]
        if r_values and "expectancy_after_costs" not in metrics:
            metrics["expectancy_after_costs"] = mean(r_values)
        if pnl_values and "net_profit" not in metrics:
            metrics["net_profit"] = sum(pnl_values)
        if slip_values:
            metrics.setdefault("average_slippage", mean(slip_values))
            metrics.setdefault("maximum_slippage", max(slip_values))
            metrics.setdefault("slippage_std", pstdev(slip_values) if len(slip_values) > 1 else 0.0)
    return {key: _round(value) if isinstance(value, float) else value for key, value in metrics.items()}


def determine_conclusion(gates: list[dict[str, Any]], prohibitions: list[dict[str, str]], metrics: Mapping[str, Any]) -> Conclusion:
    """Determine §22.9 conclusion from gate and prohibition results."""

    expectancy = _num(metrics.get("expectancy_after_costs"))
    if expectancy is not None and expectancy <= 0:
        return "strategy_invalidated"
    if prohibitions:
        return "failed_repeat_paper"
    blocking_failures = [
        gate for gate in gates
        if gate.get("blocking") and gate.get("status") in {"fail", "unavailable"}
    ]
    if blocking_failures:
        return "failed_repeat_paper"
    unresolved = [gate for gate in gates if gate.get("status") in NON_PASSING_STATUSES]
    if unresolved:
        return "conditionally_passed"
    return "passed"


def conclusion_label(conclusion: str) -> str:
    return {
        "passed": "Passed. The strategy may enter small-scale live trading.",
        "conditionally_passed": "Conditionally passed. Specific issues must be fixed before small-scale live trading.",
        "failed_repeat_paper": "Failed. Paper trading must be repeated.",
        "strategy_invalidated": "Strategy invalidated. Development should pause or return to research stage.",
    }.get(conclusion, conclusion)


def build_acceptance_report(context: Mapping[str, Any]) -> dict[str, Any]:
    """Build the full paper-trading acceptance report schema."""

    metrics = _compute_metric_summary(context)
    enriched = dict(context)
    enriched["metrics"] = metrics
    gates = [evaluate_gate(gate, enriched) for gate in ACCEPTANCE_GATES]
    prohibitions = evaluate_prohibitions(enriched)
    conclusion = determine_conclusion(gates, prohibitions, metrics)
    blocking = [
        gate for gate in gates
        if gate.get("blocking") and gate.get("status") in {"fail", "unavailable"}
    ]
    partial = [gate for gate in gates if gate.get("status") == "partial"]
    strategy = dict(context.get("strategy") or {})
    return {
        "schema_version": "paper_acceptance.v1",
        "generated_at": _now_iso(),
        "standard": "quant_paper_trading_acceptance_standard_v1.0",
        "strategy": strategy,
        "metrics": metrics,
        "gates": gates,
        "prohibitions": prohibitions,
        "summary": {
            "gate_count": len(gates),
            "passed": sum(1 for gate in gates if gate["status"] == "pass"),
            "partial": len(partial),
            "failed": sum(1 for gate in gates if gate["status"] == "fail"),
            "unavailable": sum(1 for gate in gates if gate["status"] == "unavailable"),
            "not_applicable": sum(1 for gate in gates if gate["status"] == "not_applicable"),
            "blocking_issue_count": len(blocking) + len(prohibitions),
            "conclusion": conclusion,
            "conclusion_label": conclusion_label(conclusion),
        },
        "blocking_issues": [
            {
                "id": gate["id"],
                "section": gate["section"],
                "title": gate["title"],
                "status": gate["status"],
                "reason": gate["reason"],
            }
            for gate in blocking
        ] + [
            {
                "id": item["flag"],
                "section": "21",
                "title": "Live Trading Prohibition",
                "status": "fail",
                "reason": item["description"],
            }
            for item in prohibitions
        ],
    }


def render_acceptance_markdown(report: Mapping[str, Any]) -> str:
    """Render the report in the §22 final acceptance report shape."""

    strategy = report.get("strategy") or {}
    metrics = report.get("metrics") or {}
    summary = report.get("summary") or {}
    gates = list(report.get("gates") or [])
    blocking = list(report.get("blocking_issues") or [])
    prohibitions = list(report.get("prohibitions") or [])

    def metric(key: str) -> Any:
        value = metrics.get(key)
        return "—" if value is None else value

    def strat(key: str) -> Any:
        value = strategy.get(key)
        return "—" if value is None or value == "" else value

    gate_lines = [
        f"- §{gate['section']} {gate['title']}: {gate['status']} — {gate.get('reason') or gate.get('passing_standard')}"
        for gate in gates
    ]
    blocking_lines = [
        f"- §{item['section']} {item['title']}: {item['reason']}"
        for item in blocking
    ] or ["- None"]
    prohibition_lines = [
        f"- {item['flag']}: {item['description']}"
        for item in prohibitions
    ] or ["- None"]

    return "\n".join([
        "# Paper Trading Acceptance Report",
        "",
        "## 22.1 Basic Information",
        f"- Strategy name: {strat('name')}",
        f"- Strategy version: {strat('strategy_version')}",
        f"- Parameter version: {strat('parameter_version')}",
        f"- Risk control version: {strat('risk_control_version')}",
        f"- Execution model version: {strat('execution_model_version')}",
        f"- Testing period: {strat('testing_period')}",
        f"- Exchange: {strat('exchange')}",
        f"- Trading pair: {strat('symbol')}",
        f"- Initial capital: {strat('initial_capital')}",
        f"- Order type: {strat('order_type')}",
        f"- Fee setting: {strat('fee_setting')}",
        f"- Slippage setting: {strat('slippage_setting')}",
        f"- Data source: {strat('data_source')}",
        f"- Whether shadow trading was used: {strat('shadow_trading_used')}",
        f"- Whether parameter changes were allowed during testing: {strat('parameter_changes_allowed')}",
        "",
        "## 22.2 Performance Summary",
        f"- Total return: {metric('total_return')}",
        f"- Net profit: {metric('net_profit')}",
        f"- Gross profit: {metric('gross_profit')}",
        f"- Total transaction fees: {metric('total_fees')}",
        f"- Total slippage: {metric('total_slippage')}",
        f"- Maximum drawdown: {metric('max_drawdown')}",
        f"- Win rate: {metric('win_rate')}",
        f"- Win-loss ratio: {metric('win_loss_ratio')}",
        f"- Profit factor: {metric('profit_factor')}",
        f"- Sharpe ratio: {metric('sharpe_ratio')}",
        f"- Number of trades: {metric('trade_count')}",
        f"- Average holding time: {metric('average_holding_time')}",
        f"- Maximum consecutive losses: {metric('max_consecutive_losses')}",
        "",
        "## 22.3 Trade Quality",
        f"- Average slippage: {metric('average_slippage')}",
        f"- Maximum slippage: {metric('maximum_slippage')}",
        f"- Average execution latency: {metric('average_execution_latency')}",
        f"- Maximum execution latency: {metric('maximum_execution_latency')}",
        f"- Fill rate: {metric('fill_rate')}",
        f"- Partial fill ratio: {metric('partial_fill_ratio')}",
        f"- Cancellation ratio: {metric('cancellation_ratio')}",
        f"- Rejection ratio: {metric('rejection_ratio')}",
        f"- Order timeout count: {metric('order_timeout_count')}",
        f"- Missed fill count: {metric('missed_fill_count')}",
        "",
        "## 22.4 Strategy Behavior Deviation",
        f"- Trade frequency matched expectations: {metric('trade_frequency_matches')}",
        f"- Win rate matched expectations: {metric('win_rate_matches')}",
        f"- Win-loss ratio matched expectations: {metric('win_loss_ratio_matches')}",
        f"- Average holding time matched expectations: {metric('holding_time_matches')}",
        f"- Maximum drawdown matched expectations: {metric('drawdown_matches')}",
        f"- Slippage higher than expected: {metric('slippage_higher_than_expected')}",
        f"- Fill rate lower than expected: {metric('fill_rate_lower_than_expected')}",
        f"- Abnormal trading behavior occurred: {metric('abnormal_trading_behavior')}",
        f"- Explanation of deviations: {metric('deviation_explanation')}",
        "",
        "## 22.5 System Stability",
        f"- Continuous system running time: {metric('runtime_days')} days",
        f"- Number of WebSocket disconnections: {metric('websocket_disconnect_count')}",
        f"- Number of API errors: {metric('api_error_count')}",
        f"- Number of reconciliation abnormalities: {metric('reconciliation_abnormality_count')}",
        f"- Number of program restarts: {metric('program_restart_count')}",
        f"- Number of alerts: {metric('alert_count')}",
        f"- Description of major errors: {metric('major_error_description')}",
        "",
        "## 22.6 Risk Control Trigger Records",
        f"- Per-trade loss limit triggered: {metric('per_trade_loss_limit_triggered')}",
        f"- Daily loss limit triggered: {metric('daily_loss_limit_triggered')}",
        f"- Maximum drawdown limit triggered: {metric('drawdown_limit_triggered')}",
        f"- Position limit triggered: {metric('position_limit_triggered')}",
        f"- Slippage limit triggered: {metric('slippage_limit_triggered')}",
        f"- Kill switch tested: {metric('kill_switch_tested')}",
        f"- Shutdown process worked correctly: {metric('shutdown_process_ok')}",
        "",
        "## 22.7 Security Check",
        f"- API key permissions minimized: {metric('api_key_permissions_minimized')}",
        f"- IP whitelist enabled: {metric('ip_whitelist')}",
        f"- Withdrawal permission disabled: {not bool(metric('withdrawal_permission_enabled')) if metric('withdrawal_permission_enabled') != '—' else '—'}",
        f"- Test and live environments separated: {metric('test_live_keys_separated')}",
        f"- Logs avoided sensitive information: {metric('logs_avoid_secrets')}",
        f"- API key revocation process exists: {metric('revocation_process')}",
        "",
        "## 22.8 Abnormal Event Records",
        "\n".join(blocking_lines),
        "",
        "## Gate Checklist",
        "\n".join(gate_lines),
        "",
        "## Section 21 Prohibition Hits",
        "\n".join(prohibition_lines),
        "",
        "## 22.9 Final Conclusion",
        f"**{summary.get('conclusion_label', conclusion_label(str(summary.get('conclusion', 'failed_repeat_paper'))))}**",
    ])


__all__ = [
    "ACCEPTANCE_GATES",
    "PROHIBITION_FLAGS",
    "SAMPLE_REQUIREMENTS",
    "acceptance_gate_ids",
    "build_acceptance_report",
    "conclusion_label",
    "determine_conclusion",
    "evaluate_gate",
    "evaluate_prohibitions",
    "render_acceptance_markdown",
]
