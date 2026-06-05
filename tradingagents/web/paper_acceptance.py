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


GATE_TITLE_ZH: dict[str, str] = {
    "strategy_logic": "策略邏輯檢查",
    "instrument_liquidity": "交易標的流動性檢查",
    "data_source_traceability": "資料來源與可追溯性",
    "market_execution_model": "市價單成交模型",
    "limit_execution_model": "限價單成交模型",
    "slippage_market_impact": "滑價與市場衝擊",
    "fees": "交易費用",
    "derivatives_costs": "衍生品額外成本",
    "order_lifecycle": "訂單生命週期",
    "unknown_order_state": "未知訂單狀態",
    "virtual_account": "虛擬帳戶",
    "reconciliation": "對帳機制",
    "api_rate_limits": "API 速率限制",
    "latency": "延遲量測",
    "system_stability": "系統穩定性",
    "network_abnormality": "網路異常",
    "market_abnormality": "市場異常",
    "program_abnormality": "程式異常",
    "risk_control_priority": "風控優先權",
    "position_risk": "部位風險",
    "loss_risk": "損失風險",
    "kill_switch": "Kill Switch",
    "monitoring_dashboard": "監控面板",
    "alerting": "告警機制",
    "performance_metrics": "績效指標",
    "trade_quality": "交易品質",
    "behavior_deviation": "行為偏差",
    "shadow_trading": "Shadow Trading",
    "sample_size_period": "樣本數與測試期間",
    "research_discipline": "研究紀律",
    "api_security": "API 安全",
    "capacity_scaling": "容量與資金擴張",
    "paper_live_comparison": "前測與小規模實盤比較",
    "quantitative_thresholds": "量化門檻",
    "final_report": "最終驗收報告",
}

GATE_PASSING_STANDARD_ZH: dict[str, str] = {
    "strategy_logic": "邏輯、參數與訊號定義可書面重現，且無前視偏差。",
    "instrument_liquidity": "預期下單量不得明顯超出市場流動性與可承受成本。",
    "data_source_traceability": "每筆訊號都能回溯到具時間戳的來源資料。",
    "market_execution_model": "市價單以保守簿深、VWAP 與部分成交規則模擬。",
    "limit_execution_model": "限價單不得依賴碰價即成交的樂觀假設獲利。",
    "slippage_market_impact": "扣除動態滑價與市場衝擊後，策略仍須維持正期望。",
    "fees": "績效必須以扣除 maker/taker 等實際費用後的淨值評估。",
    "derivatives_costs": "資金費率、槓桿、保證金與清算風險均已納入控制。",
    "order_lifecycle": "每筆訂單都可追蹤、回放並覆蓋完整生命週期。",
    "unknown_order_state": "未知狀態會先停風險並觸發對帳，而非盲目重送。",
    "virtual_account": "虛擬帳戶必須貼近真實帳戶，不得產生無效餘額或下單。",
    "reconciliation": "餘額、部位、訂單與成交差異必須可解釋且可修正。",
    "api_rate_limits": "速率限制須集中管理，具退避、優先序與有限重試。",
    "latency": "系統需量測延遲分佈，超門檻時可告警或暫停。",
    "system_stability": "系統需可無人值守穩定運行至少 7 天。",
    "network_abnormality": "網路故障不得造成重複下單、錯誤部位或失控交易。",
    "market_abnormality": "市場異常可以虧損，但不得讓系統失控。",
    "program_abnormality": "程式異常必須安全停止並保留可追溯紀錄。",
    "risk_control_priority": "風控權限高於策略，不能被訊號繞過。",
    "position_risk": "超出部位、槓桿、保證金或曝險上限時必須拒單。",
    "loss_risk": "停損與回撤上限需明確定義並能實際阻止新單。",
    "kill_switch": "手動與自動停機都已實測，且重啟前不得再送新單。",
    "monitoring_dashboard": "操作者可快速判斷帳戶、策略、資料、API 與風控健康度。",
    "alerting": "重大異常需主動通知，且包含原因、時間、策略、標的與嚴重度。",
    "performance_metrics": "績效必須連同淨利、成本、回撤、比率與筆數一起評估。",
    "trade_quality": "獲利不得主要來自過度理想化的成交假設。",
    "behavior_deviation": "前測行為需與回測及研究假設大致一致。",
    "shadow_trading": "Shadow 模式除最後不送單外，應共用實盤架構。",
    "sample_size_period": "樣本數與測試期間需覆蓋策略所需市場情境。",
    "research_discipline": "邏輯、參數與風控需凍結，修改後要重算統計。",
    "api_security": "API 權限最小化、環境隔離、不可硬編碼且可撤銷。",
    "capacity_scaling": "資金擴張需分階段進行，逐步重估滑價、成交率與回撤。",
    "paper_live_comparison": "前測結果需能作為小規模實盤的偏差基準。",
    "quantitative_thresholds": "穩定性、追蹤、對帳、成本、風控與容量門檻需明確。",
    "final_report": "報告需明確說明是否可進入下一個交易階段。",
}

CHECK_LABEL_OVERRIDES: dict[str, str] = {
    "no_future_data": "未使用未來資料",
    "oos_test_completed": "已完成樣本外測試",
    "ask_bid_depth_used": "使用買賣盤深度估算成交",
    "multi_level_book_consumption": "會吃穿多檔位深度",
    "vwap_execution_price": "以 VWAP 計算成交價",
    "insufficient_depth_policy": "深度不足時會拒單或部分成交",
    "queue_position_considered": "已考慮排隊順位",
    "volume_at_level_considered": "已考慮該價位實際成交量",
    "partial_fills_supported": "支援部分成交",
    "unfilled_orders_supported": "支援未成交",
    "timeout_cancel_supported": "支援逾時與取消",
    "post_only_rejection_supported": "支援 post-only 拒絕",
    "fill_rate_measured": "有量測成交率",
    "adverse_selection_measured": "有量測逆向選擇",
    "maker_taker_distinguished": "區分 maker / taker 費",
    "fee_schedule_configured": "已依實際費率設定",
    "pair_fee_differences_considered": "已考慮標的費率差異",
    "fee_recorded_per_trade": "逐筆記錄費用",
    "gross_and_net_profit_reported": "同時提供毛利與淨利",
    "client_order_id": "有 client order id",
    "unknown_state_supported": "支援 unknown 狀態",
    "no_blind_resend": "未知狀態不盲目重送",
    "client_order_id_query": "以 client order id 查詢狀態",
    "unknown_state_suspends_trading": "未知狀態會暫停交易",
    "unknown_state_alerts": "未知狀態會觸發告警",
    "available_and_frozen_balance": "區分可用與凍結資金",
    "realized_unrealized_pnl": "同時計算已實現與未實現損益",
    "minimum_notional_enforced": "有最小成交金額限制",
    "precision_rules_enforced": "有精度限制",
    "reconciliation_frequency_defined": "已定義對帳頻率",
    "major_differences_suspend_trading": "重大差異會暫停交易",
    "backoff_on_rate_limit": "觸發速率限制會退避",
    "bounded_retries": "重試次數有上限",
    "api_abnormality_pauses_strategy": "API 異常會暫停策略",
    "p95_p99_latency": "有 P95 / P99 延遲",
    "seven_day_runtime": "連續穩定運行至少 7 天",
    "restart_state_recovery": "重啟後可恢復狀態",
    "signals_cannot_bypass_risk": "訊號無法繞過風控",
    "orders_pass_risk_before_submission": "下單前必過風控",
    "risk_reject_not_resubmitted": "風控拒單後策略不自動重送",
    "risk_events_logged": "風控事件有記錄",
    "risk_events_alerted": "風控事件有告警",
    "manual_shutdown": "支援手動停機",
    "automatic_shutdown": "支援自動停機",
    "new_orders_blocked_after_shutdown": "停機後阻擋新單",
    "cancel_open_orders_after_shutdown": "停機後可撤單",
    "manual_restart_confirmation": "重啟需人工確認",
    "shutdown_test_successful": "停機流程已實測成功",
    "start_stop_notifications": "啟停有通知",
    "order_failure_notifications": "下單失敗有通知",
    "reconciliation_notifications": "對帳異常有通知",
    "kill_switch_notifications": "Kill Switch 有通知",
    "trade_count": "交易筆數",
    "average_holding_time": "平均持有時間",
    "trade_frequency_matches": "交易頻率符合預期",
    "win_rate_matches": "勝率符合預期",
    "win_loss_ratio_matches": "盈虧比符合預期",
    "holding_time_matches": "持有時間符合預期",
    "drawdown_matches": "回撤符合預期",
    "slippage_not_materially_higher": "滑價未顯著高於預期",
    "fill_rate_not_materially_lower": "成交率未顯著低於預期",
    "live_market_data_source": "沿用實盤行情來源",
    "live_signal_process": "沿用實盤訊號流程",
    "live_risk_module": "沿用實盤風控模組",
    "live_order_generation": "沿用實盤下單模組",
    "live_logging_alerting": "沿用實盤記錄與告警",
    "no_exchange_submission": "不真正送出到交易所",
    "complete_trading_cycle": "覆蓋完整交易循環",
    "sufficient_trade_samples": "交易樣本數足夠",
    "enough_market_conditions": "市場情境覆蓋足夠",
    "strategy_logic_frozen": "策略邏輯已凍結",
    "strategy_parameters_frozen": "策略參數已凍結",
    "risk_parameters_frozen": "風控參數已凍結",
    "no_short_term_parameter_tuning": "未因短期績效調參",
    "version_result_mapping": "版本與結果可對映",
    "minimum_permissions": "API 權限最小化",
    "withdrawal_disabled": "已停用提領權限",
    "no_hardcoded_keys": "未硬編碼 API 金鑰",
    "test_live_keys_separated": "測試與實盤金鑰分離",
    "order_size_vs_book_depth": "單筆下單量受限於簿深",
    "fill_rate_reestimated_after_scaling": "擴張後重新估算成交率",
    "drawdown_reestimated_after_scaling": "擴張後重新估算回撤",
    "slippage_comparison": "前測/實盤滑價比較",
    "fill_rate_comparison": "前測/實盤成交率比較",
    "rejection_rate_comparison": "前測/實盤拒單率比較",
    "api_latency_comparison": "前測/實盤 API 延遲比較",
    "trade_frequency_comparison": "前測/實盤交易頻率比較",
    "cost_erosion_comparison": "成本侵蝕比較",
    "stability_threshold": "有穩定性門檻",
    "order_tracking_threshold": "有訂單追蹤門檻",
    "reconciliation_threshold": "有對帳門檻",
    "limit_fill_threshold": "有限價成交門檻",
    "risk_control_threshold": "有風控門檻",
    "sample_size_threshold": "有樣本數門檻",
    "logging_threshold": "有日誌門檻",
    "alerting_threshold": "有告警門檻",
    "basic_information": "基本資訊完整",
    "performance_summary": "績效摘要完整",
    "behavior_deviation": "行為偏差段落完整",
    "system_stability": "系統穩定性段落完整",
    "risk_control_records": "風控觸發紀錄完整",
    "security_check": "安全檢查段落完整",
    "abnormal_events": "異常事件段落完整",
    "final_conclusion": "最終結論完整",
}

CHECK_TOKEN_ZH: dict[str, str] = {
    "entry": "進場",
    "exit": "出場",
    "stop": "停損",
    "loss": "損失",
    "take": "停利",
    "profit": "獲利",
    "conditions": "條件",
    "condition": "條件",
    "parameters": "參數",
    "parameter": "參數",
    "frozen": "凍結",
    "market": "市場",
    "data": "資料",
    "source": "來源",
    "timestamps": "時間戳",
    "latency": "延遲",
    "missing": "缺失",
    "duplicate": "重複",
    "order": "訂單",
    "orders": "訂單",
    "book": "委託簿",
    "depth": "深度",
    "price": "價格",
    "volume": "成交量",
    "queue": "排隊",
    "position": "部位",
    "positions": "部位",
    "balance": "餘額",
    "trade": "交易",
    "trades": "交易",
    "rates": "速率",
    "rate": "速率",
    "api": "API",
    "ws": "WebSocket",
    "dns": "DNS",
    "restart": "重啟",
    "risk": "風險",
    "control": "控制",
    "controls": "控制",
    "daily": "每日",
    "weekly": "每週",
    "drawdown": "回撤",
    "kill": "Kill",
    "switch": "Switch",
    "notifications": "通知",
    "notification": "通知",
    "average": "平均",
    "maximum": "最大",
    "partial": "部分",
    "waiting": "等待",
    "time": "時間",
    "shadow": "Shadow",
    "live": "實盤",
    "paper": "前測",
    "comparison": "比較",
    "threshold": "門檻",
    "report": "報告",
    "security": "安全",
    "capacity": "容量",
    "scaling": "擴張",
    "logging": "日誌",
    "alerting": "告警",
    "event": "事件",
    "events": "事件",
}


PROHIBITION_FLAGS: tuple[tuple[str, str], ...] = (
    ("fees_missing", "前測顯示獲利，但尚未納入交易費用。"),
    ("slippage_missing", "前測顯示獲利，但尚未納入滑價。"),
    ("execution_model_idealized", "成交模型過度理想化。"),
    ("touch_equals_filled", "限價單仍以碰價即成交為假設。"),
    ("order_states_incomplete", "訂單狀態無法完整追蹤。"),
    ("duplicate_orders", "系統曾產生重複下單。"),
    ("incorrect_positions", "系統曾產生錯誤部位。"),
    ("unexplained_reconciliation_diff", "餘額或部位差異無法解釋。"),
    ("reconciliation_missing", "尚未實作對帳機制。"),
    ("risk_controls_untested", "風控尚未完成測試。"),
    ("kill_switch_untested", "Kill Switch 尚未完成測試。"),
    ("api_errors_uncontrolled", "API 異常可能導致策略失控。"),
    ("websocket_recovery_missing", "WebSocket 斷線後無法可靠恢復。"),
    ("restart_state_inconsistent", "程式重啟後狀態不一致。"),
    ("repeated_parameter_changes", "前測期間反覆調參後才看起來有效。"),
    ("sample_size_too_small", "前測樣本數過小。"),
    ("single_extreme_event_profit", "獲利主要來自單一極端事件。"),
    ("averaging_down_without_limits", "虧損後加碼攤平但缺乏嚴格限制。"),
    ("stop_condition_unclear", "操作者無法清楚說明何時應停止策略。"),
    ("logging_incomplete", "日誌紀錄不完整。"),
    ("alerting_missing", "告警機制缺失。"),
    ("capital_or_loss_limits_missing", "尚未定義資金上限或虧損上限。"),
    ("api_permissions_excessive", "API 權限過大或安全控制不足。"),
    ("paper_live_comparison_missing", "前測結果無法與小規模實盤比較。"),
)


SAMPLE_REQUIREMENTS = {
    "high_frequency": {"min_trades": 300, "min_days": 7},
    "intraday": {"min_trades": 50, "min_days": 28},
    "swing": {"min_trades": 20, "min_days": 60},
    "low_frequency": {"min_trades": 10, "min_days": 90},
}


def _humanize_check_key(key: str) -> str:
    if key in CHECK_LABEL_OVERRIDES:
        return CHECK_LABEL_OVERRIDES[key]
    parts = []
    for token in key.split("_"):
        if token in CHECK_TOKEN_ZH:
            parts.append(CHECK_TOKEN_ZH[token])
        elif token.isupper():
            parts.append(token)
        elif token.startswith("p") and token[1:].isdigit():
            parts.append(token.upper())
        else:
            normalized = (
                token.replace("choch", "CHoCH")
                .replace("bos", "BOS")
                .replace("fvg", "FVG")
                .replace("ote", "OTE")
            )
            parts.append(normalized)
    label = " ".join(parts).strip()
    return label[:1].upper() + label[1:] if label else key


def _section_sort_key(section: str) -> tuple[int, float]:
    try:
        return (0, float(section))
    except ValueError:
        head = section.split(".", 1)[0]
        try:
            return (0, float(head))
        except ValueError:
            return (1, float("inf"))


def acceptance_gate_map() -> dict[str, AcceptanceGateDefinition]:
    """Return acceptance gates keyed by stable gate id."""

    return {gate.gate_id: gate for gate in ACCEPTANCE_GATES}


def acceptance_catalog(evidence: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """Return the full standard as section/gate/check catalog for UI and storage."""

    evidence = evidence or {}
    sections: dict[str, dict[str, Any]] = {}
    for gate in ACCEPTANCE_GATES:
        section = sections.setdefault(gate.section, {
            "section": gate.section,
            "gates": [],
        })
        raw = evidence.get(gate.gate_id) or {}
        gate_checks = raw.get("checks") if isinstance(raw, Mapping) and isinstance(raw.get("checks"), Mapping) else raw
        gate_sources = raw.get("sources") if isinstance(raw, Mapping) and isinstance(raw.get("sources"), Mapping) else {}
        gate_notes = raw.get("notes") if isinstance(raw, Mapping) and isinstance(raw.get("notes"), Mapping) else {}
        section["gates"].append({
            "id": gate.gate_id,
            "section": gate.section,
            "title": gate.title,
            "title_zh": GATE_TITLE_ZH.get(gate.gate_id, gate.title),
            "blocking": gate.blocking,
            "passing_standard": gate.passing_standard,
            "passing_standard_zh": GATE_PASSING_STANDARD_ZH.get(gate.gate_id, gate.passing_standard),
            "checks": [
                {
                    "key": check_key,
                    "label": _humanize_check_key(check_key),
                    "value": gate_checks.get(check_key) if isinstance(gate_checks, Mapping) else None,
                    "source": gate_sources.get(check_key, "missing"),
                    "note": gate_notes.get(check_key, ""),
                }
                for check_key in gate.evidence_keys
            ],
        })
    return [sections[key] for key in sorted(sections, key=_section_sort_key)]


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
            "reason": "目前標的未標記為衍生品、期貨或永續商品。",
        }
    if definition.optional_when == "pre_live" and stage in {"paper", "paper_trading", "shadow"}:
        return {
            "status": "not_applicable",
            "reason": "小規模實盤比較應在取得實盤資料後進行，而不是純前測階段。",
        }
    return None


def _derived_status(definition: AcceptanceGateDefinition, context: Mapping[str, Any], checks: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    metrics = context.get("metrics") or {}
    strategy = context.get("strategy") or {}

    if definition.gate_id == "fees":
        fees_included = _as_bool(metrics["fees_included"]) if "fees_included" in metrics else _as_bool(checks.get("fee_recorded_per_trade"))
        total_fees = _num(metrics.get("total_fees"))
        status: GateStatus = "pass" if fees_included and total_fees is not None else "fail"
        return {"status": status, "reason": "費用必須逐筆記錄，並從淨績效中扣除。"}

    if definition.gate_id == "slippage_market_impact":
        slippage_included = _as_bool(metrics["slippage_included"]) if "slippage_included" in metrics else _as_bool(checks.get("slippage_recorded"))
        expectancy = _num(metrics.get("expectancy_after_costs"))
        positive = expectancy is None or expectancy > 0
        status = "pass" if slippage_included and positive else "fail"
        return {"status": status, "reason": "必須納入滑價，且滑價不能把期望值侵蝕到失真。"}

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
        return {"status": status, "reason": "績效必須以成本後淨值衡量，並包含核心風險指標。"}

    if definition.gate_id == "trade_quality":
        quality_keys = ("average_slippage", "maximum_slippage", "fill_rate", "rejection_ratio")
        status = "pass" if all(metrics.get(key) is not None for key in quality_keys) else _status_from_checks(checks, definition.evidence_keys)
        return {"status": status, "reason": "前測驗收必須能提供成交品質相關指標。"}

    if definition.gate_id == "derivatives_costs":
        complete = all(checks.get(key) is True for key in definition.evidence_keys)
        status = "pass" if complete else "fail"
        return {"status": status, "reason": "衍生品前測必須完整納入 funding、槓桿、保證金與清算壓力。"}

    if definition.gate_id == "sample_size_period":
        strategy_type = (strategy.get("strategy_type") or "swing").lower()
        req = SAMPLE_REQUIREMENTS.get(strategy_type, SAMPLE_REQUIREMENTS["swing"])
        trade_count = int(_num(metrics.get("trade_count"), 0) or 0)
        days = int(_num(metrics.get("testing_days"), 0) or 0)
        status = "pass" if trade_count >= req["min_trades"] and days >= req["min_days"] else "fail"
        return {
            "status": status,
            "reason": f"{strategy_type} 至少需要 {req['min_trades']} 筆交易與 {req['min_days']} 天樣本。",
            "threshold": req,
        }

    if definition.gate_id == "reconciliation":
        abnormalities = int(_num(metrics.get("unresolved_reconciliation_count"), 0) or 0)
        implemented = _as_bool(checks.get("order_state_compared") or metrics.get("reconciliation_implemented"))
        status = "pass" if implemented and abnormalities == 0 else "fail"
        return {"status": status, "reason": "存在未解決的對帳差異時，不可升級到下一階段。"}

    if definition.gate_id == "kill_switch":
        tested = _as_bool(checks.get("shutdown_test_successful") or metrics.get("kill_switch_tested"))
        status = "pass" if tested else "fail"
        return {"status": status, "reason": "Kill Switch 完成實測，是進入實盤的最低要求。"}

    if definition.gate_id == "research_discipline":
        frozen = _as_bool(metrics.get("parameters_frozen") or checks.get("strategy_parameters_frozen"))
        changes = int(_num(metrics.get("parameter_change_count"), 0) or 0)
        status = "pass" if frozen and changes == 0 else "fail"
        return {"status": status, "reason": "前測驗證不得建立在反覆調參後的結果上。"}

    if definition.gate_id == "api_security":
        hardcoded = _as_bool(metrics.get("hardcoded_api_keys"))
        withdrawal = _as_bool(metrics.get("withdrawal_permission_enabled"))
        status = "fail" if hardcoded or withdrawal else _status_from_checks(checks, definition.evidence_keys)
        return {"status": status, "reason": "金鑰安全與過大權限會直接阻擋實盤升級。"}

    if definition.gate_id == "system_stability":
        runtime_days = _num(metrics.get("runtime_days"))
        major_error_count = int(_num(metrics.get("major_error_count"), 0) or 0)
        restart_count = int(_num(metrics.get("program_restart_count"), 0) or 0)
        if runtime_days is not None:
            status = "pass" if runtime_days >= 7 and major_error_count == 0 and restart_count <= 1 else "fail"
            return {"status": status, "reason": "系統穩定性取決於多日連續運行與低重大錯誤頻率。"}

    if definition.gate_id == "alerting":
        alert_count = _num(metrics.get("alert_count"))
        if alert_count is not None:
            status = "pass" if alert_count >= 0 and _status_from_checks(checks, definition.evidence_keys) != "fail" else "partial"
            return {"status": status, "reason": "重大異常必須由告警主動通知，而不是被動發現。"}

    if definition.gate_id == "behavior_deviation":
        alignment_score = _num(metrics.get("behavior_alignment_score"))
        if alignment_score is not None:
            status = "pass" if alignment_score >= 0.6 else "fail"
            return {"status": status, "reason": "前測行為需與研究假設大致一致，不可明顯漂移。"}

    if definition.gate_id == "capacity_scaling":
        stage_count = int(_num(metrics.get("capital_stage_count"), 0) or 0)
        if stage_count:
            status = "pass" if stage_count >= 2 else "partial"
            return {"status": status, "reason": "資金擴張需分階段進行，且每階段都要觀察與重估。"}

    if definition.gate_id == "paper_live_comparison":
        comparison_ready = _as_bool(metrics.get("paper_live_comparison_ready"))
        max_dev = _num(metrics.get("paper_live_max_deviation_ratio"))
        if comparison_ready or max_dev is not None:
            status = "pass" if comparison_ready and (max_dev is None or max_dev <= 0.35) else "partial"
            return {"status": status, "reason": "前測結果必須能持續作為小規模實盤的比較基準。"}

    if definition.gate_id == "quantitative_thresholds":
        thresholds_defined = _as_bool(metrics.get("thresholds_defined"))
        if thresholds_defined:
            return {"status": "pass", "reason": "量化門檻已明確定義。"}

    if definition.gate_id == "final_report":
        required = ("basic_information", "performance_summary", "trade_quality", "final_conclusion")
        present = sum(1 for key in required if _as_bool(checks.get(key)) or metrics.get(key) is not None)
        status = "pass" if present == len(required) else "partial" if present else "fail"
        return {"status": status, "reason": "驗收報告必須清楚說明是否已具備進入下一階段的條件。"}

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
            "title_zh": GATE_TITLE_ZH.get(definition.gate_id, definition.title),
            "status": status,
            "blocking": False,
            "passing_standard": definition.passing_standard,
            "passing_standard_zh": GATE_PASSING_STANDARD_ZH.get(definition.gate_id, definition.passing_standard),
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
        reason = evidence.get("reason") or "依明確證據狀態判定。"
    elif derived:
        status = derived["status"]
        reason = derived.get("reason", "")
    else:
        status = _status_from_checks(checks, definition.evidence_keys)
        reason = evidence.get("reason") or "依檢查項證據判定。"

    missing = [key for key in definition.evidence_keys if key not in checks or checks.get(key) in (None, "")]
    if status == "pass":
        missing = []

    return {
        "id": definition.gate_id,
        "section": definition.section,
        "title": definition.title,
        "title_zh": GATE_TITLE_ZH.get(definition.gate_id, definition.title),
        "status": status,
        "blocking": definition.blocking,
        "passing_standard": definition.passing_standard,
        "passing_standard_zh": GATE_PASSING_STANDARD_ZH.get(definition.gate_id, definition.passing_standard),
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


def summarize_sections(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate gate results by standard section for UI/report navigation."""

    status_field = {
        "pass": "passed",
        "partial": "partial",
        "fail": "failed",
        "unavailable": "unavailable",
        "not_applicable": "not_applicable",
    }
    buckets: dict[str, dict[str, Any]] = {}
    for gate in gates:
        section = str(gate.get("section") or "")
        bucket = buckets.setdefault(section, {
            "section": section,
            "passed": 0,
            "partial": 0,
            "failed": 0,
            "unavailable": 0,
            "not_applicable": 0,
            "gate_count": 0,
            "blocking_issue_count": 0,
            "titles": [],
        })
        bucket["gate_count"] += 1
        field = status_field.get(gate.get("status") or "unavailable", "unavailable")
        bucket[field] += 1
        if gate.get("blocking") and gate.get("status") in {"fail", "unavailable"}:
            bucket["blocking_issue_count"] += 1
        if gate.get("title"):
            bucket["titles"].append(gate["title"])
    return [buckets[key] for key in sorted(buckets, key=_section_sort_key)]


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
    sections = summarize_sections(gates)
    return {
        "schema_version": "paper_acceptance.v1",
        "generated_at": _now_iso(),
        "standard": "quant_paper_trading_acceptance_standard_v1.0",
        "strategy": strategy,
        "metrics": metrics,
        "gates": gates,
        "sections": sections,
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
                "title_zh": gate.get("title_zh", gate["title"]),
                "status": gate["status"],
                "reason": gate["reason"],
            }
            for gate in blocking
        ] + [
            {
                "id": item["flag"],
                "section": "21",
                "title": "Live Trading Prohibition",
                "title_zh": "實盤升級禁止條件",
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
    "CHECK_LABEL_OVERRIDES",
    "GATE_TITLE_ZH",
    "PROHIBITION_FLAGS",
    "SAMPLE_REQUIREMENTS",
    "acceptance_catalog",
    "acceptance_gate_map",
    "acceptance_gate_ids",
    "build_acceptance_report",
    "conclusion_label",
    "determine_conclusion",
    "evaluate_gate",
    "evaluate_prohibitions",
    "render_acceptance_markdown",
    "summarize_sections",
]
