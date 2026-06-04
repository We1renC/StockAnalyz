"""API-level tests for paper acceptance endpoints."""

from unittest.mock import patch

import app
from app import (
    PaperAcceptanceAlertDeliveryCreate,
    PaperAcceptanceCapitalStageCreate,
    PaperAcceptanceCheckUpdate,
    PaperAcceptanceDeviationSnapshotCreate,
    PaperAcceptanceEventCreate,
    PaperAcceptanceGenerateRequest,
    PaperAcceptanceOrderAuditCreate,
    PaperAcceptanceReconciliationCreate,
    PaperAcceptanceReviewUpdate,
    PaperAcceptanceRuntimeMetricCreate,
    PaperAcceptanceScenarioRunRequest,
    PaperAcceptanceShadowParityCreate,
    PaperAcceptanceWorkspaceUpdate,
    SMCJournalCreate,
)


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "paper_acceptance_api.db")
    app.init_db()
    return original


def test_api_generate_smc_paper_acceptance_persists_report(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "_get_vault", return_value=None):
            app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="ABAT",
                    environment="paper",
                    status="closed",
                    direction="long",
                    entry_price=10,
                    exit_price=11,
                    stop_price=9.5,
                    qty=5,
                    model="sweep_reversal",
                )
            )

        payload = app.api_generate_smc_paper_acceptance(
            PaperAcceptanceGenerateRequest(symbol="ABAT", persist=True)
        )
        assert payload["run_key"]
        assert payload["report"]["summary"]["conclusion"] == "failed_repeat_paper"

        reports = app.api_get_paper_acceptance_reports(symbol="ABAT")
        assert reports["count"] == 1
        assert reports["reports"][0]["run_key"] == payload["run_key"]
    finally:
        app.DB = original


def test_api_generate_smc_paper_acceptance_without_persist(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_generate_smc_paper_acceptance(
            PaperAcceptanceGenerateRequest(symbol="ABAT", persist=False)
        )
        assert payload["run_key"] is None
        assert "markdown" in payload

        reports = app.api_get_paper_acceptance_reports(symbol="ABAT")
        assert reports["count"] == 0
    finally:
        app.DB = original


def test_api_record_paper_acceptance_event(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_record_paper_acceptance_event(
            PaperAcceptanceEventCreate(
                symbol="ABAT",
                event_type="reconciliation",
                severity="warning",
                detail={"difference": "position mismatch"},
            )
        )
        assert payload["ok"] is True

        events = app.api_get_paper_acceptance_events(symbol="ABAT")
        assert events["count"] == 1
        assert events["events"][0]["event_key"] == payload["event_key"]
        assert events["events"][0]["detail"]["difference"] == "position mismatch"
    finally:
        app.DB = original


def test_api_workspace_and_check_crud(tmp_path):
    original = _temp_db(tmp_path)
    try:
        app.api_update_paper_acceptance_workspace(
            PaperAcceptanceWorkspaceUpdate(
                symbol="ABAT",
                strategy={"initial_capital": 10000, "name": "ABAT Acceptance"},
                metrics={"fees_included": True, "kill_switch_tested": True},
                prohibitions={"duplicate_orders": False},
            )
        )
        app.api_update_paper_acceptance_check(
            PaperAcceptanceCheckUpdate(
                symbol="ABAT",
                gate_id="strategy_logic",
                check_key="entry_conditions",
                value=True,
                note="有策略文件",
            )
        )

        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")
        assert workspace["symbol"] == "ABAT"
        assert workspace["strategy_overrides"]["initial_capital"] == 10000
        gate = next(item for item in workspace["catalog"] if item["section"] == "3.1")
        check = next(
            row for row in gate["gates"][0]["checks"]
            if row["key"] == "entry_conditions"
        )
        assert check["source"] == "manual"
        assert check["note"] == "有策略文件"

        resp = app.api_delete_paper_acceptance_check(
            symbol="ABAT",
            gate_id="strategy_logic",
            check_key="entry_conditions",
        )
        assert resp["ok"] is True

        workspace_after_clear = app.api_get_paper_acceptance_workspace(symbol="ABAT")
        gate_after_clear = next(item for item in workspace_after_clear["catalog"] if item["section"] == "3.1")
        check_after_clear = next(
            row for row in gate_after_clear["gates"][0]["checks"]
            if row["key"] == "entry_conditions"
        )
        assert check_after_clear["source"] != "manual"
    finally:
        app.DB = original


def test_api_runtime_metrics_reconciliation_order_audit_and_alert_delivery(tmp_path):
    original = _temp_db(tmp_path)
    try:
        app.api_record_paper_acceptance_runtime_metric(
            PaperAcceptanceRuntimeMetricCreate(symbol="ABAT", metric_name="api_request", value=12)
        )
        app.api_record_paper_acceptance_runtime_metric(
            PaperAcceptanceRuntimeMetricCreate(symbol="ABAT", metric_name="api_latency_ms", value=145)
        )
        app.api_record_paper_acceptance_reconciliation(
            PaperAcceptanceReconciliationCreate(
                symbol="ABAT",
                status="resolved",
                severity="warning",
                order_diff_count=1,
                trade_diff_count=1,
                auto_suspend_recommended=True,
            )
        )
        app.api_record_paper_acceptance_order_audit(
            PaperAcceptanceOrderAuditCreate(
                symbol="ABAT",
                side="buy",
                order_type="market",
                state="filled",
                requested_qty=5,
                filled_qty=5,
                signal_price=10.0,
                avg_price=10.1,
                notional=50.5,
                fee=0.1,
                slippage_bps=100.0,
                market_impact_bps=18.0,
                execution_latency_ms=120,
                strategy_version="v1",
                parameter_version="p1",
                signal_source="smc",
            )
        )
        app.api_record_paper_acceptance_alert_deliveries(
            PaperAcceptanceAlertDeliveryCreate(
                symbol="ABAT",
                event_type="api_error",
                severity="warning",
                payload_complete=True,
            )
        )

        metrics = app.api_get_paper_acceptance_runtime_metrics(symbol="ABAT")
        reconciliation = app.api_get_paper_acceptance_reconciliation(symbol="ABAT")
        orders = app.api_get_paper_acceptance_order_audit(symbol="ABAT")
        alerts = app.api_get_paper_acceptance_alert_deliveries(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert metrics["count"] >= 2
        assert reconciliation["count"] == 1
        assert orders["count"] == 1
        assert alerts["count"] == 1
        assert workspace["report"]["metrics"]["fees_included"] is True
        assert workspace["report"]["metrics"]["reconciliation_implemented"] is True
        assert len(workspace["runtime_metrics"]) >= 2
    finally:
        app.DB = original


def test_api_capital_stage_and_deviation_snapshot_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        stage_payload = app.api_record_paper_acceptance_capital_stage(
            PaperAcceptanceCapitalStageCreate(
                symbol="ABAT",
                stage_name="stage3_25_50",
                capital_ratio=0.3,
                capital_range_label="stage 3 25%-50%",
                trade_count=48,
                observation_days=25,
                slippage_bps=16.0,
                fill_rate=0.95,
                drawdown=-0.07,
                note="manual stage promotion",
            )
        )
        deviation_payload = app.api_record_paper_acceptance_deviation_snapshot(
            PaperAcceptanceDeviationSnapshotCreate(
                symbol="ABAT",
                baseline_source="paper",
                comparison_source="live",
                win_rate_delta=0.06,
                fill_rate_delta=0.03,
                slippage_delta_bps=8.0,
                drawdown_delta=0.02,
                holding_time_delta_minutes=35.0,
                trade_frequency_delta=0.12,
                deviation_score=0.04,
                detail={"origin": "manual"},
            )
        )

        stages = app.api_get_paper_acceptance_capital_stages(symbol="ABAT")
        deviations = app.api_get_paper_acceptance_deviation_snapshots(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert stage_payload["ok"] is True
        assert deviation_payload["ok"] is True
        assert stages["count"] >= 1
        assert deviations["count"] >= 1
        assert any(row["stage_name"] == "stage3_25_50" for row in workspace["capital_stages"])
        assert any(row["detail"]["origin"] == "manual" for row in workspace["deviation_snapshots"])
    finally:
        app.DB = original


def test_api_shadow_parity_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_record_paper_acceptance_shadow_parity(
            PaperAcceptanceShadowParityCreate(
                symbol="ABAT",
                market_timestamp="2026-06-05T09:00:00Z",
                signal_timestamp="2026-06-05T09:00:01Z",
                risk_timestamp="2026-06-05T09:00:02Z",
                order_intent_timestamp="2026-06-05T09:00:03Z",
                adapter_timestamp="2026-06-05T09:00:03.200000Z",
                adapter_name="shadow_adapter_v1",
                side="buy",
                order_type="limit",
                requested_qty=5,
                signal_price=10.0,
                expected_price=10.04,
                execution_latency_ms=200,
                market_data_source_shared=True,
                signal_process_shared=True,
                risk_module_shared=True,
                order_generation_shared=True,
                logging_alerting_shared=True,
                no_exchange_submission=True,
                order_book_snapshot_recorded=True,
                likely_execution_price_recorded=True,
                post_order_price_behavior_recorded=True,
                detail={"origin": "manual"},
            )
        )

        rows = app.api_get_paper_acceptance_shadow_parity(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert payload["ok"] is True
        assert rows["count"] == 1
        assert rows["summary"]["shared_module_ratio"] == 1.0
        assert workspace["shadow_parity_summary"]["trace_count"] == 1
        assert workspace["shadow_parity_traces"][0]["adapter_name"] == "shadow_adapter_v1"
    finally:
        app.DB = original


def test_api_run_acceptance_scenario(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_run_paper_acceptance_scenario(
            PaperAcceptanceScenarioRunRequest(symbol="ABAT", scenario_id="kill_switch_blocks_orders")
        )
        rows = app.api_get_paper_acceptance_scenarios(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert payload["ok"] is True
        assert rows["count"] == 1
        gate = next(item for item in workspace["catalog"] if item["section"] == "10.4")
        check = next(
            row for row in gate["gates"][0]["checks"]
            if row["key"] == "new_orders_blocked_after_shutdown"
        )
        assert check["value"] is True
    finally:
        app.DB = original


def test_api_review_governance_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        app.api_update_paper_acceptance_review(
            PaperAcceptanceReviewUpdate(
                symbol="ABAT",
                reviewer="qa",
                review_status="approved",
                fixed_in_version="v3",
                retest_required=False,
                can_promote_to_live=True,
                note="條件達成",
            )
        )
        review = app.api_get_paper_acceptance_review(symbol="ABAT")
        promotion = app.api_get_paper_acceptance_promotion(symbol="ABAT")
        promotion_check = app.api_get_paper_acceptance_promotion_check(symbol="ABAT")
        changes = app.api_get_paper_acceptance_change_log(symbol="ABAT")
        coverage = app.api_get_paper_acceptance_coverage(symbol="ABAT")
        security = app.api_get_paper_acceptance_security_scan(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert review["review_status"] == "approved"
        assert review["can_promote_to_live"] is True
        assert workspace["review"]["reviewer"] == "qa"
        assert "policy" in promotion
        assert promotion_check["decision"] in {"allow", "conditional", "deny"}
        assert changes["count"] >= 1
        assert "coverage" in coverage
        assert isinstance(coverage["coverage"]["sections"], list)
        assert isinstance(coverage["coverage"]["missing_details"], list)
        assert "security_scan" in security
    finally:
        app.DB = original
