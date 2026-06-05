"""Tests for quantitative paper-trading acceptance gates."""

from paper_acceptance import (
    ACCEPTANCE_GATES,
    PROHIBITION_FLAGS,
    acceptance_gate_ids,
    build_acceptance_report,
    render_acceptance_markdown,
)


def _passing_context():
    evidence = {
        gate.gate_id: {"checks": {key: True for key in gate.evidence_keys}}
        for gate in ACCEPTANCE_GATES
    }
    return {
        "stage": "paper",
        "strategy": {
            "name": "SMC Sweep Reversal",
            "symbol": "BTCUSDT",
            "strategy_type": "intraday",
            "instrument_type": "spot",
            "strategy_version": "v1.0",
            "parameter_version": "2026-06-04",
            "risk_control_version": "risk-v1",
            "execution_model_version": "paper-v1",
            "testing_period": "2026-05-01 ~ 2026-06-04",
            "exchange": "paper-sim",
            "initial_capital": 10000,
            "order_type": "limit+market",
            "fee_setting": "maker/taker",
            "slippage_setting": "dynamic",
            "data_source": "exchange-realtime",
            "shadow_trading_used": True,
            "parameter_changes_allowed": False,
        },
        "metrics": {
            "trade_count": 80,
            "testing_days": 35,
            "fees_included": True,
            "total_fees": 42.5,
            "slippage_included": True,
            "total_slippage": 18.3,
            "expectancy_after_costs": 0.42,
            "gross_profit": 980.0,
            "net_profit": 810.0,
            "total_return": 0.081,
            "max_drawdown": -0.045,
            "win_rate": 0.55,
            "win_loss_ratio": 1.7,
            "profit_factor": 1.8,
            "sharpe_ratio": 1.2,
            "average_holding_time": "45m",
            "max_consecutive_losses": 3,
            "average_slippage": 0.001,
            "maximum_slippage": 0.005,
            "fill_rate": 0.91,
            "rejection_ratio": 0.01,
            "reconciliation_implemented": True,
            "unresolved_reconciliation_count": 0,
            "kill_switch_tested": True,
            "parameters_frozen": True,
            "parameter_change_count": 0,
            "hardcoded_api_keys": False,
            "withdrawal_permission_enabled": False,
        },
        "evidence": evidence,
    }


def test_acceptance_standard_covers_major_sections():
    sections = {gate.section for gate in ACCEPTANCE_GATES}
    assert len(ACCEPTANCE_GATES) >= 30
    assert {"3.1", "4.1", "6.2", "7.2", "10.4", "12.2", "16", "21", "22"} - sections == {"21"}
    assert "kill_switch" in acceptance_gate_ids()
    assert "paper_live_comparison" in acceptance_gate_ids()
    assert any(flag == "fees_missing" for flag, _ in PROHIBITION_FLAGS)


def test_complete_acceptance_context_passes_and_renders_report():
    report = build_acceptance_report(_passing_context())
    assert report["summary"]["conclusion"] == "passed"
    assert report["summary"]["blocking_issue_count"] == 0
    assert report["summary"]["failed"] == 0
    assert report["summary"]["unavailable"] == 0

    markdown = render_acceptance_markdown(report)
    assert "# 前測驗收報告" in markdown
    assert "## 22.0 Reviewer 摘要" in markdown
    assert "## 22.9 Final Conclusion" in markdown
    assert "通過，可進入小規模實盤" in markdown


def test_missing_costs_slippage_and_kill_switch_are_blockers():
    ctx = _passing_context()
    ctx["metrics"] = {
        **ctx["metrics"],
        "fees_included": False,
        "total_fees": None,
        "slippage_included": False,
        "kill_switch_tested": False,
    }
    ctx["evidence"]["kill_switch"] = {"checks": {key: False for key in ctx["evidence"]["kill_switch"]["checks"]}}

    report = build_acceptance_report(ctx)
    blockers = {item["id"] for item in report["blocking_issues"]}
    assert report["summary"]["conclusion"] == "failed_repeat_paper"
    assert {"fees", "slippage_market_impact", "kill_switch"}.issubset(blockers)


def test_negative_expectancy_invalidates_strategy_even_when_process_is_complete():
    ctx = _passing_context()
    ctx["metrics"] = {**ctx["metrics"], "expectancy_after_costs": -0.05}

    report = build_acceptance_report(ctx)
    assert report["summary"]["conclusion"] == "strategy_invalidated"


def test_section_21_prohibitions_block_live_trading():
    ctx = _passing_context()
    ctx["prohibitions"] = {
        "duplicate_orders": True,
        "api_permissions_excessive": True,
    }

    report = build_acceptance_report(ctx)
    assert report["summary"]["conclusion"] == "failed_repeat_paper"
    assert {hit["flag"] for hit in report["prohibitions"]} == {
        "duplicate_orders",
        "api_permissions_excessive",
    }


def test_derivatives_cost_gate_requires_funding_margin_and_liquidation_controls():
    ctx = _passing_context()
    ctx["strategy"]["instrument_type"] = "perpetual"
    ctx["evidence"]["derivatives_costs"] = {
        "checks": {
            "funding_rates_included": True,
            "leverage_included": True,
            "margin_usage_included": True,
            "liquidation_price_calculated": True,
            "funding_reflected_in_equity": True,
            "liquidation_stress_tested": True,
        }
    }

    report = build_acceptance_report(ctx)
    gate = next(item for item in report["gates"] if item["id"] == "derivatives_costs")
    assert gate["status"] == "pass"

    ctx["evidence"]["derivatives_costs"]["checks"]["liquidation_stress_tested"] = False
    failed = build_acceptance_report(ctx)
    gate_failed = next(item for item in failed["gates"] if item["id"] == "derivatives_costs")
    assert gate_failed["status"] == "fail"
