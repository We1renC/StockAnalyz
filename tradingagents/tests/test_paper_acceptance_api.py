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
    PaperAcceptanceGovernanceEventCreate,
    PaperAcceptanceOrderAuditCreate,
    PaperAcceptanceReconciliationCreate,
    PaperAcceptanceReviewUpdate,
    PaperAcceptancePromotionDecisionCreate,
    PaperAcceptanceRuntimeMetricCreate,
    PaperAcceptanceScenarioRunRequest,
    PaperAcceptanceShadowParityCreate,
    PaperAcceptanceThresholdProfileCreate,
    PaperAcceptanceVenueProfileCreate,
    PaperAcceptanceStabilitySessionCreate,
    PaperAcceptanceWorkspaceUpdate,
    PaperAcceptanceVirtualAccountSnapshotCreate,
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


def test_api_virtual_account_stability_and_dashboard_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        virtual_payload = app.api_record_paper_acceptance_virtual_account_snapshot(
            PaperAcceptanceVirtualAccountSnapshotCreate(
                symbol="ABAT",
                account_currency="USD",
                equity=10240.0,
                available_balance=8600.0,
                frozen_balance=220.0,
                margin_used=800.0,
                unrealized_pnl=140.0,
                realized_pnl=60.0,
                open_position_count=2,
                open_order_count=1,
                detail={"minimum_notional_enforced": True},
            )
        )
        stability_payload = app.api_record_paper_acceptance_stability_session(
            PaperAcceptanceStabilitySessionCreate(
                symbol="ABAT",
                session_name="weekly-soak",
                started_at="2026-05-01T00:00:00Z",
                ended_at="2026-05-08T00:00:00Z",
                runtime_hours=168,
                restart_count=0,
                reconnect_count=1,
                max_memory_pct=55.0,
                max_cpu_pct=40.0,
                max_api_latency_ms=200.0,
                result="pass",
                detail={"restart_state_recovery": True},
            )
        )

        virtual_rows = app.api_get_paper_acceptance_virtual_account_snapshots(symbol="ABAT")
        stability_rows = app.api_get_paper_acceptance_stability_sessions(symbol="ABAT")
        dashboard = app.api_get_paper_acceptance_dashboard(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert virtual_payload["ok"] is True
        assert stability_payload["ok"] is True
        assert virtual_rows["count"] == 1
        assert stability_rows["count"] == 1
        assert dashboard["virtual_account"]["latest"]["equity"] == 10240.0
        assert dashboard["stability"]["latest"]["runtime_hours"] == 168.0
        assert workspace["virtual_account_snapshots"][0]["available_balance"] == 8600.0
        assert workspace["stability_sessions"][0]["session_name"] == "weekly-soak"
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


def test_api_venue_profiles_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_record_paper_acceptance_venue_profile(
            PaperAcceptanceVenueProfileCreate(
                symbol="ABAT",
                venue_name="NASDAQ",
                broker_name="IBKR",
                market_type="equity",
                status="approved",
                maker_fee_bps=1.5,
                taker_fee_bps=3.2,
                transaction_tax_bps=0.0,
                min_notional=1.0,
                tick_size=0.01,
                lot_size=1,
                quantity_precision=0,
                price_precision=2,
                rate_limit_per_minute=120,
                rate_limit_burst=20,
                reject_taxonomy={"precision": "reject"},
                source_summary={"source": "broker spec"},
                approved_by="qa",
                version_tag="venue-v1",
            )
        )

        rows = app.api_get_paper_acceptance_venue_profiles(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert payload["ok"] is True
        assert rows["count"] == 1
        assert rows["summary"]["venue_name"] == "NASDAQ"
        assert rows["summary"]["precision_rules_enforced"] is True
        assert workspace["venue_summary"]["active_version_tag"] == "venue-v1"
        assert workspace["venue_profiles"][0]["broker_name"] == "IBKR"
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
        assert "promotion_ladder" in promotion["policy"]
        assert promotion_check["decision"] in {"allow", "conditional", "deny"}
        assert changes["count"] >= 1
        assert "coverage" in coverage
        assert isinstance(coverage["coverage"]["sections"], list)
        assert isinstance(coverage["coverage"]["missing_details"], list)
        assert "security_scan" in security
    finally:
        app.DB = original


def test_api_governance_event_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_record_paper_acceptance_governance(
            PaperAcceptanceGovernanceEventCreate(
                symbol="ABAT",
                change_scope="parameter",
                change_class="research_override",
                version_tag="v2",
                approved_by="qa",
                requires_restart_stats=True,
                stats_restarted=False,
                freeze_window_started_at="2026-06-05T00:00:00Z",
                freeze_window_ended_at="2026-06-10T00:00:00Z",
                event_timestamp="2026-06-06T09:00:00Z",
                reason="freeze 期間修補",
                detail={"ticket": "PA-22"},
            )
        )

        rows = app.api_get_paper_acceptance_governance(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert payload["ok"] is True
        assert rows["count"] == 1
        assert rows["summary"]["freeze_violation_count"] == 1
        assert workspace["governance_summary"]["freeze_violation_count"] == 1
        assert workspace["policy"]["evidence"]["research_discipline"]["strategy_parameters_frozen"] is False
    finally:
        app.DB = original


def test_api_threshold_profile_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        payload = app.api_record_paper_acceptance_threshold_profile(
            PaperAcceptanceThresholdProfileCreate(
                symbol="ABAT",
                strategy_type="intraday",
                profile_name="q2-calibration",
                status="approved",
                thresholds={"min_trade_count": 78, "max_average_slippage_bps": 42.0},
                source_summary={"window": "2026-04~2026-06", "sources": ["paper", "live"]},
                approved_by="qa",
                version_tag="thr-2026q2",
                note="以最新 paper/live 偏差重估",
            )
        )

        rows = app.api_get_paper_acceptance_threshold_profiles(symbol="ABAT")
        promotion = app.api_get_paper_acceptance_promotion(symbol="ABAT")
        workspace = app.api_get_paper_acceptance_workspace(symbol="ABAT")

        assert payload["ok"] is True
        assert rows["count"] == 1
        assert rows["summary"]["active_thresholds"]["min_trade_count"] == 78
        assert rows["summary"]["active_version_tag"] == "thr-2026q2"
        assert promotion["policy"]["thresholds"]["max_average_slippage_bps"] == 42.0
        assert workspace["threshold_profiles"][0]["profile_name"] == "q2-calibration"
    finally:
        app.DB = original


def test_api_promotion_decision_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        app.api_record_paper_acceptance_threshold_profile(
            PaperAcceptanceThresholdProfileCreate(
                symbol="ABAT",
                strategy_type="intraday",
                profile_name="q2-calibration",
                status="approved",
                thresholds={"min_trade_count": 64, "max_average_slippage_bps": 38.0},
                approved_by="qa",
                version_tag="thr-v2",
            )
        )
        app.api_update_paper_acceptance_review(
            PaperAcceptanceReviewUpdate(
                symbol="ABAT",
                reviewer="qa",
                review_status="reviewing",
                retest_required=False,
                can_promote_to_live=False,
                note="先保留在 shadow",
            )
        )

        payload = app.api_record_paper_acceptance_promotion_decision(
            PaperAcceptancePromotionDecisionCreate(
                symbol="ABAT",
                decision="conditional",
                note="觀察一週後再決定是否升級",
            )
        )

        rows = app.api_get_paper_acceptance_promotion_decisions(symbol="ABAT")
        check = app.api_get_paper_acceptance_promotion_check(symbol="ABAT")
        closure = app.api_get_paper_acceptance_closure(symbol="ABAT")

        assert payload["ok"] is True
        assert rows["count"] == 1
        assert rows["summary"]["latest_decision"] == "conditional"
        assert rows["rows"][0]["threshold_profile_version_tag"] == "thr-v2"
        assert check["promotion_summary"]["latest_decision"] == "conditional"
        assert isinstance(check["production_checklist"], list)
        assert closure["closure_summary"]["promotion_decision_summary"]["latest_decision"] == "conditional"
        assert isinstance(closure["production_checklist"], list)
    finally:
        app.DB = original


def test_api_closure_summary_round_trip(tmp_path):
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
        app.api_update_paper_acceptance_review(
            PaperAcceptanceReviewUpdate(
                symbol="ABAT",
                reviewer="qa",
                review_status="reviewing",
                retest_required=True,
                note="待補證據",
            )
        )

        payload = app.api_get_paper_acceptance_closure(symbol="ABAT")

        assert payload["closure_summary"]["next_action"] in {"repair_and_repeat_paper", "continue_shadow", "continue_paper", "allow_small_live"}
        assert "policy" in payload
        assert "review" in payload
    finally:
        app.DB = original


def test_api_promotion_check_decision_matrix(tmp_path):
    original = _temp_db(tmp_path)
    try:
        base_metrics = {
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
        }
        base_strategy = {
            "name": "ABAT Acceptance",
            "strategy_type": "intraday",
            "shared_live_architecture": True,
            "shadow_trading_used": True,
            "strategy_version": "v1",
            "parameter_version": "p1",
        }

        with patch("paper_acceptance_store.run_security_scan", return_value={
            "no_hardcoded_keys": True,
            "test_live_separation": True,
            "revocation_process": True,
            "scanned_files": 12,
            "hardcoded_secret_count": 0,
        }):
            for symbol in ("ALLOW", "CONDITIONAL", "DENY"):
                app.api_update_paper_acceptance_workspace(
                    PaperAcceptanceWorkspaceUpdate(
                        symbol=symbol,
                        strategy=base_strategy,
                        metrics=base_metrics,
                        prohibitions={},
                    )
                )
                app.api_record_paper_acceptance_threshold_profile(
                    PaperAcceptanceThresholdProfileCreate(
                        symbol=symbol,
                        strategy_type="intraday",
                        profile_name="q2-calibration",
                        status="approved",
                        thresholds={"min_trade_count": 50, "max_average_slippage_bps": 80.0},
                        approved_by="qa",
                        version_tag="thr-v2",
                    )
                )

            app.api_update_paper_acceptance_review(
                PaperAcceptanceReviewUpdate(
                    symbol="ALLOW",
                    reviewer="qa",
                    review_status="approved",
                    retest_required=False,
                    can_promote_to_live=True,
                )
            )
            app.api_update_paper_acceptance_review(
                PaperAcceptanceReviewUpdate(
                    symbol="CONDITIONAL",
                    reviewer="qa",
                    review_status="reviewing",
                    retest_required=False,
                    can_promote_to_live=False,
                )
            )
            app.api_update_paper_acceptance_workspace(
                PaperAcceptanceWorkspaceUpdate(
                    symbol="DENY",
                    metrics={**base_metrics, "fill_rate": 0.2},
                )
            )
            app.api_update_paper_acceptance_review(
                PaperAcceptanceReviewUpdate(
                    symbol="DENY",
                    reviewer="qa",
                    review_status="approved",
                    retest_required=False,
                    can_promote_to_live=True,
                )
            )

            allow = app.api_get_paper_acceptance_promotion_check(symbol="ALLOW")
            conditional = app.api_get_paper_acceptance_promotion_check(symbol="CONDITIONAL")
            deny = app.api_get_paper_acceptance_promotion_check(symbol="DENY")

        assert allow["decision"] == "allow"
        assert conditional["decision"] == "conditional"
        assert deny["decision"] == "deny"
    finally:
        app.DB = original
