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
    load_acceptance_context_overrides,
    load_acceptance_events,
    load_acceptance_reports,
    load_order_audit_rows,
    load_reconciliation_runs,
    load_runtime_metrics,
    persist_acceptance_report,
    record_alert_delivery,
    record_acceptance_event,
    record_order_audit,
    record_reconciliation_run,
    record_runtime_metric,
    run_acceptance_scenario,
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
        detail={"volatility_bps": 18},
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
        detail={"adverse_selection_bps": 12, "post_order_price_move_bps": 30},
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
    assert workspace["timeline"][0]["kind"] in {"event", "scenario"}
    assert workspace["section_trend"][0]["run_key"] == payload["run_key"]
