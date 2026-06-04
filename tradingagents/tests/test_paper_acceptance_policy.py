"""Tests for acceptance promotion policy snapshot."""

from paper_acceptance import ACCEPTANCE_GATES
from paper_acceptance_policy import build_acceptance_policy_snapshot


def _context():
    evidence = {
        gate.gate_id: {"checks": {key: True for key in gate.evidence_keys}}
        for gate in ACCEPTANCE_GATES
    }
    return {
        "strategy": {
            "name": "SMC",
            "symbol": "ABAT",
            "strategy_type": "intraday",
            "shadow_trading_used": True,
            "strategy_version": "v1",
            "parameter_version": "p1",
        },
        "metrics": {
            "trade_count": 60,
            "testing_days": 25,
            "api_error_rate": 0.01,
            "average_slippage": 50.0,
            "fill_rate": 0.9,
            "rejection_ratio": 0.02,
            "max_drawdown": -0.08,
            "parameters_frozen": True,
            "parameter_change_count": 0,
            "kill_switch_tested": True,
            "fees_included": True,
            "reconciliation_implemented": True,
            "total_fees": 10,
            "total_slippage": 20,
            "average_api_latency": 120,
            "average_holding_minutes": 45,
            "capital_stage_count": 1,
            "trade_day_span": 25,
            "traded_day_count": 12,
            "idle_day_count": 4,
            "session_bucket_count": 3,
            "volatility_bucket_count": 3,
            "liquidity_bucket_count": 2,
            "regime_combo_count": 4,
            "high_vol_trade_count": 8,
            "low_vol_trade_count": 7,
            "thin_liquidity_trade_count": 5,
            "regime_coverage_score": 0.88,
            "shadow_trace_count": 2,
            "shadow_parity_score": 0.92,
            "shadow_market_data_shared_ratio": 1.0,
            "shadow_signal_process_shared_ratio": 1.0,
            "shadow_risk_module_shared_ratio": 1.0,
            "shadow_order_generation_shared_ratio": 1.0,
            "shadow_logging_alerting_shared_ratio": 1.0,
            "shadow_no_exchange_submission_ratio": 1.0,
            "shadow_order_book_snapshot_ratio": 1.0,
            "shadow_likely_execution_price_ratio": 1.0,
            "shadow_post_order_price_behavior_ratio": 1.0,
            "hardcoded_api_keys": False,
            "withdrawal_permission_enabled": False,
            "test_live_keys_separated": True,
            "logs_avoid_secrets": True,
            "revocation_process": True,
            "ip_whitelist": True,
        },
        "capital_stages": [{"stage_name": "stage1_1_5", "capital_ratio": 0.05}],
        "deviation_snapshots": [{"baseline_source": "paper", "comparison_source": "live", "deviation_score": 0.2}],
        "evidence": evidence,
        "prohibitions": {},
    }


def test_policy_snapshot_marks_shadow_when_review_not_approved():
    payload = build_acceptance_policy_snapshot(_context(), review={"review_status": "reviewing", "retest_required": False})
    assert payload["shared_architecture_ready"] is True
    assert payload["recommendation"] == "shadow"
    assert payload["can_promote"] is False
    assert payload["promotion_ladder"]["current_stage"]["stage_name"] == "stage1_1_5"
    assert payload["promotion_ladder"]["next_stage"]["stage_name"] == "stage2_10_20"


def test_policy_snapshot_blocks_on_threshold_and_prohibition_failure():
    ctx = _context()
    ctx["metrics"]["fill_rate"] = 0.3
    ctx["prohibitions"] = {"duplicate_orders": True}
    payload = build_acceptance_policy_snapshot(ctx, review={"review_status": "approved", "can_promote_to_live": True})
    assert "20 fill_rate_threshold_failed" in payload["blockers"]
    assert "21 prohibition_flags_present" in payload["blockers"]
    assert any(row["key"] == "fill_rate" for row in payload["promotion_ladder"]["blocker_deltas"])


def test_policy_snapshot_blocks_when_regime_coverage_is_thin():
    ctx = _context()
    ctx["metrics"]["regime_coverage_score"] = 0.2
    ctx["metrics"]["regime_combo_count"] = 1
    ctx["metrics"]["volatility_bucket_count"] = 1
    ctx["metrics"]["liquidity_bucket_count"] = 1
    ctx["metrics"]["session_bucket_count"] = 1
    ctx["metrics"]["high_vol_trade_count"] = 0
    ctx["metrics"]["low_vol_trade_count"] = 0
    ctx["metrics"]["thin_liquidity_trade_count"] = 0
    payload = build_acceptance_policy_snapshot(ctx, review={"review_status": "reviewing"})
    assert "15 sample_size_or_market_regime_insufficient" in payload["blockers"]
    assert any(row["key"] == "regime_coverage" for row in payload["promotion_ladder"]["blocker_deltas"])
