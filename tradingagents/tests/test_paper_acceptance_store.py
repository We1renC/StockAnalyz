"""Tests for paper acceptance SQLite persistence helpers."""

import sqlite3

from paper_acceptance import ACCEPTANCE_GATES, build_acceptance_report
from paper_acceptance_store import (
    build_acceptance_workspace,
    build_and_persist_smc_acceptance_report,
    build_smc_acceptance_context,
    delete_acceptance_check,
    ensure_paper_acceptance_schema,
    load_alert_deliveries,
    load_acceptance_checks,
    load_acceptance_change_log,
    load_acceptance_context_overrides,
    load_acceptance_events,
    load_acceptance_reports,
    load_capital_stages,
    load_deviation_snapshots,
    load_order_audit_rows,
    load_reconciliation_runs,
    load_runtime_metrics,
    load_shadow_parity_traces,
    persist_acceptance_report,
    record_alert_delivery,
    record_acceptance_event,
    record_capital_stage,
    record_deviation_snapshot,
    record_shadow_parity_trace,
    record_order_audit,
    record_reconciliation_run,
    record_runtime_metric,
    refresh_acceptance_reports_for_symbols,
    run_acceptance_scenario,
    summarize_shadow_parity_traces,
    upsert_acceptance_review,
    upsert_acceptance_check,
    upsert_acceptance_context_overrides,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _minimal_passing_report():
    evidence = {
        gate.gate_id: {"checks": {key: True for key in gate.evidence_keys}}
        for gate in ACCEPTANCE_GATES
    }
    return build_acceptance_report({
        "stage": "paper",
        "strategy": {
            "name": "SMC Acceptance",
            "symbol": "ABAT",
            "strategy_type": "intraday",
            "instrument_type": "spot",
            "stage": "paper",
        },
        "metrics": {
            "trade_count": 60,
            "testing_days": 35,
            "fees_included": True,
            "total_fees": 12.5,
            "slippage_included": True,
            "expectancy_after_costs": 0.31,
            "gross_profit": 300.0,
            "net_profit": 240.0,
            "max_drawdown": -0.04,
            "win_rate": 0.53,
            "profit_factor": 1.7,
            "average_slippage": 0.001,
            "maximum_slippage": 0.004,
            "fill_rate": 0.93,
            "rejection_ratio": 0.0,
            "reconciliation_implemented": True,
            "unresolved_reconciliation_count": 0,
            "kill_switch_tested": True,
            "parameters_frozen": True,
            "parameter_change_count": 0,
            "hardcoded_api_keys": False,
            "withdrawal_permission_enabled": False,
        },
        "evidence": evidence,
    })


def _create_smc_source_tables(conn):
    conn.execute(
        """CREATE TABLE smc_trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            environment TEXT,
            status TEXT,
            entry_time TEXT,
            created_at TEXT,
            pnl REAL,
            r_multiple REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE smc_backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            created_at TEXT
        )"""
    )
    conn.executemany(
        """INSERT INTO smc_trade_journal
           (symbol, environment, status, entry_time, created_at, pnl, r_multiple)
           VALUES (?, 'paper', 'closed', ?, ?, ?, ?)""",
        [
            ("ABAT", "2026-06-01T09:00:00Z", "2026-06-01T09:00:00Z", 10.0, 1.0),
            ("ABAT", "2026-06-02T09:00:00Z", "2026-06-02T09:00:00Z", -5.0, -0.5),
        ],
    )
    conn.execute(
        "INSERT INTO smc_backtest_runs (symbol, created_at) VALUES ('ABAT', '2026-06-01T00:00:00Z')"
    )
    conn.commit()


def test_persist_and_load_acceptance_report_round_trip():
    conn = _conn()
    report = _minimal_passing_report()
    run_key = persist_acceptance_report(conn, report)

    rows = load_acceptance_reports(conn, symbol="ABAT")
    assert rows[0]["run_key"] == run_key
    assert rows[0]["symbol"] == "ABAT"
    assert rows[0]["gate_summary"]["conclusion"] == "passed"
    assert rows[0]["report_payload"]["schema_version"] == "paper_acceptance.v1"


def test_record_and_load_acceptance_event():
    conn = _conn()
    ensure_paper_acceptance_schema(conn)
    event_key = record_acceptance_event(
        conn,
        symbol="ABAT",
        event_type="kill_switch",
        severity="critical",
        detail={"reason": "manual test"},
    )

    rows = load_acceptance_events(conn, symbol="ABAT")
    assert rows[0]["event_key"] == event_key
    assert rows[0]["severity"] == "critical"
    assert rows[0]["detail"]["reason"] == "manual test"


def test_build_smc_context_flags_missing_acceptance_infrastructure():
    conn = _conn()
    _create_smc_source_tables(conn)

    context = build_smc_acceptance_context(conn, symbol="ABAT")
    assert context["metrics"]["trade_count"] == 2
    assert context["metrics"]["backtest_run_count"] == 1
    assert context["metrics"]["fees_included"] is False
    assert context["prohibitions"]["fees_missing"] is True
    assert context["prohibitions"]["kill_switch_untested"] is True

    report = build_acceptance_report(context)
    assert report["summary"]["conclusion"] == "failed_repeat_paper"
    blockers = {item["id"] for item in report["blocking_issues"]}
    assert "fees" in blockers
    assert "kill_switch_untested" in blockers


def test_build_and_persist_smc_acceptance_report():
    conn = _conn()
    _create_smc_source_tables(conn)

    payload = build_and_persist_smc_acceptance_report(conn, symbol="ABAT")
    rows = load_acceptance_reports(conn, symbol="ABAT")
    assert rows[0]["run_key"] == payload["run_key"]
    assert rows[0]["gate_summary"]["conclusion"] == "failed_repeat_paper"


def test_workspace_overrides_and_manual_checks_are_reflected_in_workspace():
    conn = _conn()
    _create_smc_source_tables(conn)

    upsert_acceptance_context_overrides(
        conn,
        symbol="ABAT",
        strategy={"initial_capital": 10000, "name": "ABAT Acceptance"},
        metrics={
            "fees_included": True,
            "total_fees": 4.5,
            "slippage_included": True,
            "average_slippage": 0.001,
            "maximum_slippage": 0.002,
            "fill_rate": 0.92,
            "rejection_ratio": 0.0,
            "kill_switch_tested": True,
            "reconciliation_implemented": True,
            "parameters_frozen": True,
            "parameter_change_count": 0,
        },
        prohibitions={"duplicate_orders": False},
    )
    upsert_acceptance_check(
        conn,
        symbol="ABAT",
        gate_id="strategy_logic",
        check_key="entry_conditions",
        value=True,
        note="已有書面定義",
    )

    overrides = load_acceptance_context_overrides(conn, "ABAT")
    checks = load_acceptance_checks(conn, "ABAT")
    context = build_smc_acceptance_context(conn, symbol="ABAT")
    workspace = build_acceptance_workspace(conn, symbol="ABAT")

    assert overrides["strategy"]["initial_capital"] == 10000
    assert checks[0]["check_key"] == "entry_conditions"
    assert context["metrics"]["fees_included"] is True
    assert context["metrics"]["kill_switch_tested"] is True
    assert len(workspace["catalog"]) >= 5

    gate = next(item for item in workspace["catalog"] if item["section"] == "3.1")
    check = next(
        row for row in gate["gates"][0]["checks"]
        if row["key"] == "entry_conditions"
    )
    assert check["source"] == "manual"
    assert check["note"] == "已有書面定義"

    delete_acceptance_check(conn, symbol="ABAT", gate_id="strategy_logic", check_key="entry_conditions")
    workspace_after_clear = build_acceptance_workspace(conn, symbol="ABAT")
    gate_after_clear = next(item for item in workspace_after_clear["catalog"] if item["section"] == "3.1")
    check_after_clear = next(
        row for row in gate_after_clear["gates"][0]["checks"]
        if row["key"] == "entry_conditions"
    )
    assert check_after_clear["source"] != "manual"


def test_telemetry_and_order_audit_are_aggregated_into_acceptance_context():
    conn = _conn()
    _create_smc_source_tables(conn)
    ensure_paper_acceptance_schema(conn)

    record_runtime_metric(conn, symbol="ABAT", metric_name="api_request", value=10)
    record_runtime_metric(conn, symbol="ABAT", metric_name="api_error", value=1, severity="error")
    record_runtime_metric(conn, symbol="ABAT", metric_name="api_latency_ms", value=120)
    record_runtime_metric(conn, symbol="ABAT", metric_name="api_latency_ms", value=240)
    record_runtime_metric(conn, symbol="ABAT", metric_name="market_data_latency_ms", value=80)
    record_runtime_metric(conn, symbol="ABAT", metric_name="signal_compute_time_ms", value=15)
    record_runtime_metric(conn, symbol="ABAT", metric_name="order_request_latency_ms", value=55)
    record_runtime_metric(conn, symbol="ABAT", metric_name="exchange_response_latency_ms", value=65)
    record_runtime_metric(conn, symbol="ABAT", metric_name="database_write_latency_ms", value=25)
    record_runtime_metric(conn, symbol="ABAT", metric_name="loop_runtime_ms", value=90)
    record_runtime_metric(conn, symbol="ABAT", metric_name="memory_pct", value=40)
    record_runtime_metric(conn, symbol="ABAT", metric_name="cpu_pct", value=35)
    record_runtime_metric(conn, symbol="ABAT", metric_name="disk_free_gb", value=18)
    record_runtime_metric(conn, symbol="ABAT", metric_name="clock_offset_ms", value=1200)
    record_runtime_metric(conn, symbol="ABAT", metric_name="log_size_mb", value=30)
    record_runtime_metric(conn, symbol="ABAT", metric_name="db_connection_count", value=4)
    record_runtime_metric(conn, symbol="ABAT", metric_name="request_weight", value=50)
    record_runtime_metric(conn, symbol="ABAT", metric_name="order_count", value=5)
    record_runtime_metric(conn, symbol="ABAT", metric_name="shared_api_budget", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="rate_limit_backoff", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="bounded_retry", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="request_priority_rule", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="latency_pause", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="risk_status", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="scheduled_task_ok", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="restart_state_recovery", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="missing_data_handled", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="duplicate_data_handled", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="out_of_order_data_handled", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="reconnect_backfill", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="real_time_equity", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="available_balance", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="current_positions", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="open_orders", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="daily_total_pnl", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="max_drawdown_displayed", value=1)

    record_reconciliation_run(
        conn,
        symbol="ABAT",
        status="resolved",
        severity="warning",
        order_diff_count=1,
        position_diff_count=0,
        balance_diff_count=0,
        trade_diff_count=1,
        auto_suspend_recommended=True,
        restoration_result="recovered",
    )
    record_order_audit(
        conn,
        symbol="ABAT",
        side="buy",
        order_type="market",
        state="filled",
        requested_qty=5,
        filled_qty=5,
        signal_price=10.0,
        avg_price=10.15,
        notional=50.75,
        fee=0.1,
        slippage_bps=150.0,
        market_impact_bps=25.0,
        execution_latency_ms=180,
        strategy_version="v1",
        parameter_version="p1",
        signal_source="smc",
        detail={
            "volatility_bps": 18,
            "maker_taker": "taker",
            "spread_bps": 12,
            "recent_volume_ratio": 0.08,
            "book_depth_ratio": 1.6,
            "expected_edge_bps": 280,
            "liquidity_regime": "normal",
            "market_data_source": "exchange_ws",
        },
    )
    record_order_audit(
        conn,
        symbol="ABAT",
        side="buy",
        order_type="limit",
        state="partially_filled",
        requested_qty=10,
        filled_qty=6,
        unfilled_qty=4,
        signal_price=10.0,
        limit_price=10.05,
        avg_price=10.05,
        notional=60.3,
        fee=0.08,
        slippage_bps=50.0,
        market_impact_bps=10.0,
        execution_latency_ms=420,
        client_order_id="cli-1",
        strategy_version="v1",
        parameter_version="p1",
        signal_source="smc",
        submitted_at="2026-06-01T09:00:00Z",
        fill_at="2026-06-01T09:00:03Z",
        detail={
            "adverse_selection_bps": 12,
            "post_order_price_move_bps": 30,
            "maker_taker": "maker",
            "spread_bps": 18,
            "recent_volume_ratio": 0.12,
            "book_depth_ratio": 1.3,
            "expected_edge_bps": 190,
            "liquidity_regime": "normal",
            "market_data_source": "exchange_ws",
        },
    )
    record_alert_delivery(conn, symbol="ABAT", event_type="api_error", severity="warning")
    record_alert_delivery(conn, symbol="ABAT", event_type="reconciliation", severity="warning")
    record_alert_delivery(conn, symbol="ABAT", event_type="kill_switch", severity="critical")

    context = build_smc_acceptance_context(conn, symbol="ABAT")
    workspace = build_acceptance_workspace(conn, symbol="ABAT")

    assert context["metrics"]["fees_included"] is True
    assert context["metrics"]["slippage_included"] is True
    assert context["metrics"]["reconciliation_implemented"] is True
    assert context["metrics"]["fill_rate"] == 1.0
    assert context["metrics"]["partial_fill_ratio"] == 0.5
    assert context["metrics"]["latency_p95"] is not None
    assert len(workspace["runtime_metrics"]) >= 5
    assert len(workspace["reconciliation_runs"]) == 1
    assert len(workspace["order_audit"]) == 2
    assert len(workspace["alert_deliveries"]) == 3

    alert_gate = next(item for item in workspace["catalog"] if item["section"] == "11.2")
    kill_switch_check = next(
        row for row in alert_gate["gates"][0]["checks"]
        if row["key"] == "kill_switch_notifications"
    )
    assert kill_switch_check["value"] is True
    liquidity_gate = next(item for item in workspace["catalog"] if item["section"] == "3.2")
    spread_check = next(row for row in liquidity_gate["gates"][0]["checks"] if row["key"] == "spread_acceptable")
    assert spread_check["value"] is True
    trace_gate = next(item for item in workspace["catalog"] if item["section"] == "3.3")
    duplicate_check = next(row for row in trace_gate["gates"][0]["checks"] if row["key"] == "duplicate_data_handled")
    assert duplicate_check["value"] is True

    assert load_runtime_metrics(conn, symbol="ABAT")
    assert load_reconciliation_runs(conn, symbol="ABAT")
    assert load_order_audit_rows(conn, symbol="ABAT")
    assert load_alert_deliveries(conn, symbol="ABAT")


def test_workspace_includes_review_timeline_and_trend():
    conn = _conn()
    _create_smc_source_tables(conn)

    run_acceptance_scenario(conn, symbol="ABAT", scenario_id="kill_switch_blocks_orders")
    record_acceptance_event(conn, symbol="ABAT", event_type="kill_switch", severity="critical", detail={"reason": "manual test"})
    payload = build_and_persist_smc_acceptance_report(conn, symbol="ABAT")
    review = upsert_acceptance_review(
        conn,
        symbol="ABAT",
        reviewer="qa",
        review_status="changes_required",
        fixed_in_version="v2",
        retest_required=True,
        can_promote_to_live=False,
        note="需要補成本證據",
        run_key=payload["run_key"],
    )
    workspace = build_acceptance_workspace(conn, symbol="ABAT")

    assert review["review_status"] == "changes_required"
    assert workspace["review"]["reviewer"] == "qa"
    assert workspace["review"]["retest_required"] is True
    assert workspace["timeline"][0]["kind"] in {"event", "scenario", "change"}
    assert workspace["section_trend"][0]["run_key"] == payload["run_key"]
    assert "recommendation" in workspace["policy"]
    assert "promotion_ladder" in workspace["policy"]
    assert "covered_ratio" in workspace["coverage"]
    assert isinstance(workspace["coverage"]["sections"], list)
    assert isinstance(workspace["coverage"]["missing_details"], list)
    assert isinstance(workspace["capital_stages"], list)
    assert isinstance(workspace["deviation_snapshots"], list)
    changes = load_acceptance_change_log(conn, "ABAT")
    assert any(row["change_type"] == "review_updated" for row in changes)
    assert any(item["kind"] == "change" for item in workspace["timeline"])


def test_capital_stage_and_deviation_snapshot_round_trip():
    conn = _conn()

    stage = record_capital_stage(
        conn,
        symbol="ABAT",
        stage_name="stage2_10_20",
        capital_ratio=0.12,
        capital_range_label="stage 2 10%-20%",
        trade_count=32,
        observation_days=18,
        slippage_bps=22.5,
        fill_rate=0.91,
        drawdown=-0.08,
        note="manual capacity review",
    )
    deviation = record_deviation_snapshot(
        conn,
        symbol="ABAT",
        baseline_source="paper",
        comparison_source="live",
        win_rate_delta=0.08,
        fill_rate_delta=0.04,
        slippage_delta_bps=12.0,
        drawdown_delta=0.03,
        holding_time_delta_minutes=45.0,
        trade_frequency_delta=0.2,
        deviation_score=0.05,
        detail={"origin": "manual"},
    )

    stages = load_capital_stages(conn, "ABAT")
    deviations = load_deviation_snapshots(conn, "ABAT")

    assert stages[0]["stage_name"] == stage["stage_name"]
    assert stages[0]["capital_ratio"] == stage["capital_ratio"]
    assert stages[0]["note"] == "manual capacity review"
    assert deviations[0]["baseline_source"] == deviation["baseline_source"]
    assert deviations[0]["comparison_source"] == deviation["comparison_source"]
    assert deviations[0]["detail"]["origin"] == "manual"


def test_shadow_parity_round_trip_and_context_mapping():
    conn = _conn()
    _create_smc_source_tables(conn)

    trace = record_shadow_parity_trace(
        conn,
        symbol="ABAT",
        market_timestamp="2026-06-05T09:00:00Z",
        signal_timestamp="2026-06-05T09:00:01Z",
        risk_timestamp="2026-06-05T09:00:02Z",
        order_intent_timestamp="2026-06-05T09:00:03Z",
        adapter_timestamp="2026-06-05T09:00:03.150000Z",
        adapter_name="shadow_adapter_v1",
        side="buy",
        order_type="limit",
        requested_qty=5,
        signal_price=10.0,
        expected_price=10.05,
        execution_latency_ms=150,
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

    rows = load_shadow_parity_traces(conn, "ABAT")
    summary = summarize_shadow_parity_traces(conn, "ABAT")
    context = build_smc_acceptance_context(conn, symbol="ABAT")

    assert rows[0]["parity_key"] == trace["parity_key"]
    assert summary["trace_count"] == 1
    assert summary["shared_module_ratio"] == 1.0
    assert context["strategy"]["shadow_trading_used"] is True
    assert context["strategy"]["shared_live_architecture"] is True
    assert context["metrics"]["shadow_parity_score"] == 1.0
    shadow_gate = context["evidence"]["shadow_trading"]["checks"]
    assert shadow_gate["live_market_data_source"] is True
    assert shadow_gate["no_exchange_submission"] is True
    assert context["policy"]["promotion_ladder"]["shadow_trace_count"] == 1


def test_context_maps_regime_coverage_metrics_into_policy():
    conn = _conn()
    _create_smc_source_tables(conn)
    ensure_paper_acceptance_schema(conn)
    conn.executemany(
        """INSERT INTO smc_trade_journal
           (symbol, environment, status, entry_time, created_at, pnl, r_multiple)
           VALUES (?, 'paper', 'closed', ?, ?, ?, ?)""",
        [
            ("ABAT", "2026-06-01T01:00:00Z", "2026-06-01T01:00:00Z", 12.0, 1.1),
            ("ABAT", "2026-06-03T10:00:00Z", "2026-06-03T10:00:00Z", -8.0, -0.7),
            ("ABAT", "2026-06-05T18:30:00Z", "2026-06-05T18:30:00Z", 6.0, 0.6),
        ],
    )
    conn.commit()

    rows = [
        ("2026-06-01T01:00:00Z", {"volatility_bps": 20, "liquidity_regime": "normal", "spread_bps": 12, "recent_volume_ratio": 0.08, "book_depth_ratio": 1.8}),
        ("2026-06-03T10:00:00Z", {"volatility_bps": 145, "liquidity_regime": "thin", "spread_bps": 96, "recent_volume_ratio": 0.24, "book_depth_ratio": 0.7}),
        ("2026-06-05T18:30:00Z", {"volatility_bps": 70, "liquidity_regime": "deep", "spread_bps": 8, "recent_volume_ratio": 0.04, "book_depth_ratio": 2.5}),
    ]
    for submitted_at, detail in rows:
        record_order_audit(
            conn,
            symbol="ABAT",
            side="buy",
            order_type="limit",
            state="filled",
            requested_qty=5,
            filled_qty=5,
            signal_price=10.0,
            avg_price=10.05,
            notional=50.25,
            fee=0.1,
            slippage_bps=15.0,
            execution_latency_ms=90,
            submitted_at=submitted_at,
            detail=detail,
            stage="paper",
        )

    context = build_smc_acceptance_context(conn, symbol="ABAT", strategy={"strategy_type": "intraday"})

    assert context["metrics"]["regime_coverage_score"] >= 0.8
    assert context["metrics"]["session_bucket_count"] == 3
    sample_gate = context["policy"]["evidence"]["sample_size_period"]
    assert sample_gate["enough_market_conditions"] is True
    assert sample_gate["weak_liquidity_periods"] is True


def test_workspace_auto_generates_stage_and_deviation_from_live_telemetry():
    conn = _conn()
    _create_smc_source_tables(conn)
    ensure_paper_acceptance_schema(conn)
    conn.executemany(
        """INSERT INTO smc_trade_journal
           (symbol, environment, status, entry_time, created_at, pnl, r_multiple)
           VALUES (?, 'live', 'closed', ?, ?, ?, ?)""",
        [
            ("ABAT", "2026-06-03T09:00:00Z", "2026-06-03T09:00:00Z", 12.0, 1.2),
            ("ABAT", "2026-06-04T09:00:00Z", "2026-06-04T09:00:00Z", -4.0, -0.4),
        ],
    )
    conn.commit()
    record_order_audit(
        conn,
        symbol="ABAT",
        side="buy",
        order_type="market",
        state="filled",
        requested_qty=5,
        filled_qty=5,
        signal_price=10.0,
        avg_price=10.12,
        notional=50.6,
        fee=0.1,
        slippage_bps=12.0,
        execution_latency_ms=100,
        stage="paper",
    )
    record_order_audit(
        conn,
        symbol="ABAT",
        side="buy",
        order_type="market",
        state="filled",
        requested_qty=4,
        filled_qty=4,
        signal_price=10.0,
        avg_price=10.18,
        notional=40.72,
        fee=0.09,
        slippage_bps=18.0,
        execution_latency_ms=90,
        stage="live",
    )

    workspace = build_acceptance_workspace(conn, symbol="ABAT")

    assert workspace["capital_stages"]
    assert workspace["deviation_snapshots"]
    live_deviation = next(
        row for row in workspace["deviation_snapshots"]
        if row["baseline_source"] == "paper" and row["comparison_source"] == "live"
    )
    assert live_deviation["fill_rate_delta"] == 0.0
    assert live_deviation["slippage_delta_bps"] == 6.0
    assert live_deviation["detail"]["live_average_slippage"] == 18.0


def test_refresh_acceptance_reports_for_symbols_skips_recent_and_empty():
    conn = _conn()
    _create_smc_source_tables(conn)

    first = refresh_acceptance_reports_for_symbols(conn, ["ABAT", "TEST.TW"], min_interval_minutes=30)
    rows = load_acceptance_reports(conn, symbol="ABAT")
    second = refresh_acceptance_reports_for_symbols(conn, ["ABAT", "EMPTY"], min_interval_minutes=30)

    assert first["refreshed_symbols"] == ["ABAT"]
    assert "TEST.TW" in first["skipped_empty_symbols"]
    assert rows[0]["symbol"] == "ABAT"
    assert second["refreshed_count"] == 0
    assert second["skipped_recent_symbols"] == ["ABAT"]
    assert "EMPTY" in second["skipped_empty_symbols"]
