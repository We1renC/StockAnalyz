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
            "hardcoded_api_keys": False,
            "withdrawal_permission_enabled": False,
            "test_live_keys_separated": True,
            "logs_avoid_secrets": True,
            "revocation_process": True,
            "ip_whitelist": True,
        },
        "evidence": evidence,
        "prohibitions": {},
    }


def test_policy_snapshot_marks_shadow_when_review_not_approved():
    payload = build_acceptance_policy_snapshot(_context(), review={"review_status": "reviewing", "retest_required": False})
    assert payload["shared_architecture_ready"] is True
    assert payload["recommendation"] == "shadow"
    assert payload["can_promote"] is False


def test_policy_snapshot_blocks_on_threshold_and_prohibition_failure():
    ctx = _context()
    ctx["metrics"]["fill_rate"] = 0.3
    ctx["prohibitions"] = {"duplicate_orders": True}
    payload = build_acceptance_policy_snapshot(ctx, review={"review_status": "approved", "can_promote_to_live": True})
    assert "20 fill_rate_threshold_failed" in payload["blockers"]
    assert "21 prohibition_flags_present" in payload["blockers"]
