"""Tests for acceptance telemetry aggregation helpers."""

import sqlite3

from paper_acceptance_metrics import (
    ensure_paper_acceptance_metrics_schema,
    record_alert_delivery,
    record_order_audit,
    record_reconciliation_run,
    record_runtime_metric,
    summarize_acceptance_telemetry,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_summarize_acceptance_telemetry_builds_metrics_and_evidence():
    conn = _conn()
    ensure_paper_acceptance_metrics_schema(conn)

    record_runtime_metric(conn, symbol="ABAT", metric_name="api_request", value=20)
    record_runtime_metric(conn, symbol="ABAT", metric_name="api_error", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="api_latency_ms", value=120)
    record_runtime_metric(conn, symbol="ABAT", metric_name="market_data_latency_ms", value=80)
    record_runtime_metric(conn, symbol="ABAT", metric_name="signal_compute_time_ms", value=10)
    record_runtime_metric(conn, symbol="ABAT", metric_name="order_request_latency_ms", value=30)
    record_runtime_metric(conn, symbol="ABAT", metric_name="exchange_response_latency_ms", value=50)
    record_runtime_metric(conn, symbol="ABAT", metric_name="database_write_latency_ms", value=12)
    record_runtime_metric(conn, symbol="ABAT", metric_name="loop_runtime_ms", value=95)
    record_runtime_metric(conn, symbol="ABAT", metric_name="request_weight", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="bounded_retry", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="risk_status", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="real_time_equity", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="current_positions", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="missing_data_handled", value=1)
    record_runtime_metric(conn, symbol="ABAT", metric_name="duplicate_data_handled", value=1)

    record_reconciliation_run(conn, symbol="ABAT", status="resolved", severity="warning", order_diff_count=1)
    record_order_audit(
        conn,
        symbol="ABAT",
        side="buy",
        order_type="market",
        state="filled",
        requested_qty=5,
        filled_qty=5,
        signal_price=10.0,
        avg_price=10.2,
        notional=51.0,
        fee=0.1,
        slippage_bps=120.0,
        market_impact_bps=18.0,
        execution_latency_ms=140,
        strategy_version="v1",
        parameter_version="p1",
        signal_source="smc",
        detail={
            "maker_taker": "taker",
            "spread_bps": 14,
            "recent_volume_ratio": 0.1,
            "book_depth_ratio": 1.4,
            "expected_edge_bps": 220,
            "liquidity_regime": "normal",
            "market_data_source": "exchange_ws",
        },
    )
    record_alert_delivery(conn, symbol="ABAT", event_type="api_error", severity="warning")

    payload = summarize_acceptance_telemetry(conn, symbol="ABAT")

    assert payload["metrics"]["api_error_rate"] == 0.05
    assert payload["metrics"]["fill_rate"] == 1.0
    assert payload["metrics"]["average_spread_bps"] == 14.0
    assert payload["evidence"]["instrument_liquidity"]["spread_acceptable"] is True
    assert payload["evidence"]["data_source_traceability"]["duplicate_data_handled"] is True
    assert payload["evidence"]["monitoring_dashboard"]["real_time_equity"] is True


def test_summarize_acceptance_telemetry_tracks_regime_coverage_matrix():
    conn = _conn()
    ensure_paper_acceptance_metrics_schema(conn)

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
        submitted_at="2026-06-01T01:00:00Z",
        detail={
            "volatility_bps": 20,
            "liquidity_regime": "normal",
            "spread_bps": 12,
            "recent_volume_ratio": 0.08,
            "book_depth_ratio": 1.8,
        },
    )
    record_order_audit(
        conn,
        symbol="ABAT",
        side="sell",
        order_type="limit",
        state="filled",
        requested_qty=5,
        filled_qty=5,
        signal_price=9.7,
        avg_price=9.62,
        notional=48.1,
        fee=0.1,
        slippage_bps=35.0,
        execution_latency_ms=120,
        submitted_at="2026-06-03T10:00:00Z",
        detail={
            "volatility_bps": 145,
            "liquidity_regime": "thin",
            "spread_bps": 96,
            "recent_volume_ratio": 0.24,
            "book_depth_ratio": 0.7,
        },
    )
    record_order_audit(
        conn,
        symbol="ABAT",
        side="buy",
        order_type="market",
        state="filled",
        requested_qty=4,
        filled_qty=4,
        signal_price=10.2,
        avg_price=10.22,
        notional=40.88,
        fee=0.1,
        slippage_bps=10.0,
        execution_latency_ms=80,
        submitted_at="2026-06-05T18:30:00Z",
        detail={
            "volatility_bps": 70,
            "liquidity_regime": "deep",
            "spread_bps": 8,
            "recent_volume_ratio": 0.04,
            "book_depth_ratio": 2.5,
        },
    )

    payload = summarize_acceptance_telemetry(conn, symbol="ABAT")

    assert payload["metrics"]["volatility_bucket_count"] == 3
    assert payload["metrics"]["liquidity_bucket_count"] == 3
    assert payload["metrics"]["session_bucket_count"] == 3
    assert payload["metrics"]["idle_day_count"] >= 2
    assert payload["metrics"]["thin_liquidity_trade_count"] == 1
    assert payload["metrics"]["regime_coverage_score"] >= 0.8
    assert payload["evidence"]["sample_size_period"]["enough_market_conditions"] is True
    assert payload["evidence"]["sample_size_period"]["vol_expansion_contraction"] is True
    assert payload["regime_coverage"]["condition_combo_counts"]["high_vol:thin_liquidity"] == 1
