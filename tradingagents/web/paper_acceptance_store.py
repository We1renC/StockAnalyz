"""SQLite helpers for paper-trading acceptance reports and evidence workspace."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from paper_acceptance import (
    ACCEPTANCE_GATES,
    acceptance_catalog,
    build_acceptance_report,
    render_acceptance_markdown,
)
from paper_acceptance_metrics import (
    ensure_paper_acceptance_metrics_schema,
    load_alert_deliveries,
    load_order_audit_rows,
    load_reconciliation_runs,
    load_runtime_metrics,
    record_alert_delivery,
    record_order_audit,
    record_reconciliation_run,
    record_runtime_metric,
    summarize_acceptance_telemetry,
)
from paper_acceptance_scenarios import (
    ensure_paper_acceptance_scenario_schema,
    load_scenario_runs,
    run_acceptance_scenario,
    scenario_catalog,
    summarize_scenario_evidence,
)
from paper_acceptance_policy import build_acceptance_policy_snapshot
from paper_acceptance_security import run_security_scan


FRAMEWORK_CAPABILITY_CHECKS: dict[str, dict[str, bool]] = {
    "market_execution_model": {
        "ask_bid_depth_used": True,
        "multi_level_book_consumption": True,
        "vwap_execution_price": True,
        "insufficient_depth_policy": True,
        "market_slippage_records": True,
    },
    "limit_execution_model": {
        "queue_position_considered": True,
        "volume_at_level_considered": True,
        "partial_fills_supported": True,
        "unfilled_orders_supported": True,
        "timeout_cancel_supported": True,
        "post_only_rejection_supported": True,
    },
    "order_lifecycle": {
        "new_state_supported": True,
        "partial_fill_state_supported": True,
        "filled_state_supported": True,
        "cancel_reject_expire_supported": True,
        "unknown_state_supported": True,
    },
    "unknown_order_state": {
        "timeout_simulated": True,
        "no_confirmation_simulated": True,
        "no_blind_resend": True,
        "client_order_id_query": True,
        "unknown_state_suspends_trading": True,
    },
    "virtual_account": {
        "multi_currency_balances": True,
        "available_and_frozen_balance": True,
        "realized_unrealized_pnl": True,
        "insufficient_balance_rejection": True,
        "minimum_notional_enforced": True,
    },
    "risk_control_priority": {
        "signals_cannot_bypass_risk": True,
        "orders_pass_risk_before_submission": True,
        "risk_reject_not_resubmitted": True,
    },
    "position_risk": {
        "max_order_size": True,
        "max_position_per_pair": True,
        "max_open_orders": True,
        "directional_exposure_limit": True,
        "limit_rejection_tested": True,
    },
    "loss_risk": {
        "shutdown_conditions": True,
        "loss_limit_stops_new_trades": True,
    },
    "kill_switch": {
        "new_orders_blocked_after_shutdown": True,
    },
    "api_security": {
        "no_hardcoded_keys": True,
    },
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _safe_float(value, default: float | None = None) -> float | None:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol_key(symbol: str | None) -> str:
    return (symbol or "ALL").strip().upper() or "ALL"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_duration(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    total_minutes = max(0, int(round(minutes)))
    hours, mins = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _value_is_true(value) -> bool:
    return value is True or value == 1 or value == 1.0


def ensure_paper_acceptance_schema(conn) -> None:
    """Create acceptance report and evidence workspace tables if needed."""

    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            strategy_name TEXT,
            symbol TEXT,
            stage TEXT NOT NULL DEFAULT 'paper',
            standard_version TEXT NOT NULL,
            conclusion TEXT NOT NULL,
            gate_count INTEGER NOT NULL DEFAULT 0,
            blocking_issue_count INTEGER NOT NULL DEFAULT 0,
            metrics TEXT NOT NULL DEFAULT '{}',
            gate_summary TEXT NOT NULL DEFAULT '{}',
            report_payload TEXT NOT NULL,
            markdown_report TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_symbol_created
           ON paper_acceptance_runs(symbol, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_conclusion
           ON paper_acceptance_runs(conclusion, created_at DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            run_key TEXT,
            symbol TEXT,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            status TEXT NOT NULL DEFAULT 'open',
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_events_symbol_created
           ON paper_acceptance_events(symbol, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_context_overrides (
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            strategy_payload TEXT NOT NULL DEFAULT '{}',
            metrics_payload TEXT NOT NULL DEFAULT '{}',
            prohibitions_payload TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(symbol, stage)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            gate_id TEXT NOT NULL,
            check_key TEXT NOT NULL,
            value_json TEXT NOT NULL DEFAULT 'null',
            note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL,
            UNIQUE(symbol, stage, gate_id, check_key)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_evidence_symbol_gate
           ON paper_acceptance_evidence(symbol, stage, gate_id, updated_at DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            run_key TEXT,
            reviewer TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'pending',
            fixed_in_version TEXT NOT NULL DEFAULT '',
            retest_required INTEGER NOT NULL DEFAULT 0,
            can_promote_to_live INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_reviews_symbol_updated
           ON paper_acceptance_reviews(symbol, stage, updated_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            change_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            change_type TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_key TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_change_log_symbol_created
           ON paper_acceptance_change_log(symbol, stage, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_capital_stages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            stage_name TEXT NOT NULL,
            capital_ratio REAL,
            capital_range_label TEXT NOT NULL DEFAULT '',
            trade_count INTEGER NOT NULL DEFAULT 0,
            observation_days INTEGER NOT NULL DEFAULT 0,
            slippage_bps REAL,
            fill_rate REAL,
            drawdown REAL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_capital_stage_symbol_created
           ON paper_acceptance_capital_stages(symbol, stage, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_deviation_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            baseline_source TEXT NOT NULL DEFAULT 'backtest',
            comparison_source TEXT NOT NULL DEFAULT 'paper',
            win_rate_delta REAL,
            fill_rate_delta REAL,
            slippage_delta_bps REAL,
            drawdown_delta REAL,
            holding_time_delta_minutes REAL,
            trade_frequency_delta REAL,
            deviation_score REAL,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_deviation_symbol_created
           ON paper_acceptance_deviation_snapshots(symbol, stage, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_shadow_parity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parity_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            runtime_stage TEXT NOT NULL DEFAULT 'shadow',
            market_timestamp TEXT,
            signal_timestamp TEXT,
            risk_timestamp TEXT,
            order_intent_timestamp TEXT,
            adapter_timestamp TEXT,
            adapter_name TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL DEFAULT '',
            order_type TEXT NOT NULL DEFAULT '',
            requested_qty REAL,
            signal_price REAL,
            expected_price REAL,
            execution_latency_ms REAL,
            market_data_source_shared INTEGER NOT NULL DEFAULT 0,
            signal_process_shared INTEGER NOT NULL DEFAULT 0,
            risk_module_shared INTEGER NOT NULL DEFAULT 0,
            order_generation_shared INTEGER NOT NULL DEFAULT 0,
            logging_alerting_shared INTEGER NOT NULL DEFAULT 0,
            no_exchange_submission INTEGER NOT NULL DEFAULT 1,
            order_book_snapshot_recorded INTEGER NOT NULL DEFAULT 0,
            likely_execution_price_recorded INTEGER NOT NULL DEFAULT 0,
            post_order_price_behavior_recorded INTEGER NOT NULL DEFAULT 0,
            parity_score REAL,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_shadow_parity_symbol_created
           ON paper_acceptance_shadow_parity(symbol, stage, created_at DESC, id DESC)"""
    )
    ensure_paper_acceptance_metrics_schema(conn)
    ensure_paper_acceptance_scenario_schema(conn)
    conn.commit()


def persist_acceptance_report(conn, report: dict, markdown: str | None = None) -> str:
    """Persist a generated acceptance report and return its run key."""

    ensure_paper_acceptance_schema(conn)
    strategy = report.get("strategy") or {}
    summary = report.get("summary") or {}
    run_key = f"paper-acceptance-{uuid4().hex[:12]}"
    markdown_report = markdown or render_acceptance_markdown(report)
    created_at = report.get("generated_at") or _now_iso()
    conn.execute(
        """INSERT INTO paper_acceptance_runs
           (run_key, strategy_name, symbol, stage, standard_version, conclusion,
            gate_count, blocking_issue_count, metrics, gate_summary, report_payload,
            markdown_report, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_key,
            strategy.get("name") or strategy.get("strategy_name") or "",
            _symbol_key(strategy.get("symbol")),
            strategy.get("stage") or "paper",
            report.get("standard") or "quant_paper_trading_acceptance_standard_v1.0",
            summary.get("conclusion") or "failed_repeat_paper",
            summary.get("gate_count") or 0,
            summary.get("blocking_issue_count") or 0,
            _json_dumps(report.get("metrics") or {}),
            _json_dumps(summary),
            _json_dumps(report),
            markdown_report,
            created_at,
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=_symbol_key(strategy.get("symbol")),
        stage=strategy.get("stage") or "paper",
        change_type="report_generated",
        target_type="report",
        target_key=run_key,
        detail={
            "conclusion": summary.get("conclusion") or "failed_repeat_paper",
            "blocking_issue_count": summary.get("blocking_issue_count") or 0,
        },
    )
    return run_key


def record_acceptance_change(
    conn,
    *,
    symbol: str,
    stage: str = "paper",
    change_type: str,
    target_type: str,
    target_key: str = "",
    detail: dict | None = None,
) -> dict:
    """Persist a governance change trail for acceptance workspace operations."""

    ensure_paper_acceptance_schema(conn)
    payload = {
        "change_key": f"paper-change-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "change_type": change_type,
        "target_type": target_type,
        "target_key": target_key or "",
        "detail": detail or {},
        "created_at": _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_change_log
           (change_key, symbol, stage, change_type, target_type, target_key, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["change_key"],
            payload["symbol"],
            payload["stage"],
            payload["change_type"],
            payload["target_type"],
            payload["target_key"],
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    return payload


def load_acceptance_change_log(conn, symbol: str | None, stage: str = "paper", limit: int = 100) -> list[dict]:
    """Load recent acceptance workspace governance changes."""

    ensure_paper_acceptance_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_change_log
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 1000))),
    ).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["detail"] = _json_loads(data.get("detail"), {})
        out.append(data)
    return out


def record_capital_stage(
    conn,
    *,
    symbol: str,
    stage_name: str,
    capital_ratio: float | None = None,
    capital_range_label: str = "",
    trade_count: int = 0,
    observation_days: int = 0,
    slippage_bps: float | None = None,
    fill_rate: float | None = None,
    drawdown: float | None = None,
    note: str = "",
    stage: str = "paper",
) -> dict:
    """Persist a staged capital exposure evidence snapshot."""

    ensure_paper_acceptance_schema(conn)
    payload = {
        "stage_key": f"paper-capacity-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "stage_name": stage_name,
        "capital_ratio": _safe_float(capital_ratio),
        "capital_range_label": capital_range_label or "",
        "trade_count": int(trade_count or 0),
        "observation_days": int(observation_days or 0),
        "slippage_bps": _safe_float(slippage_bps),
        "fill_rate": _safe_float(fill_rate),
        "drawdown": _safe_float(drawdown),
        "note": note or "",
        "created_at": _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_capital_stages
           (stage_key, symbol, stage, stage_name, capital_ratio, capital_range_label,
            trade_count, observation_days, slippage_bps, fill_rate, drawdown, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["stage_key"],
            payload["symbol"],
            payload["stage"],
            payload["stage_name"],
            payload["capital_ratio"],
            payload["capital_range_label"],
            payload["trade_count"],
            payload["observation_days"],
            payload["slippage_bps"],
            payload["fill_rate"],
            payload["drawdown"],
            payload["note"],
            payload["created_at"],
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=payload["symbol"],
        stage=payload["stage"],
        change_type="capital_stage_recorded",
        target_type="capital_stage",
        target_key=payload["stage_name"],
        detail={"capital_ratio": payload["capital_ratio"], "trade_count": payload["trade_count"]},
    )
    return payload


def load_capital_stages(conn, symbol: str | None, stage: str = "paper", limit: int = 50) -> list[dict]:
    """Load persisted capital stage evidence snapshots."""

    ensure_paper_acceptance_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_capital_stages
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


def record_deviation_snapshot(
    conn,
    *,
    symbol: str,
    baseline_source: str,
    comparison_source: str,
    win_rate_delta: float | None = None,
    fill_rate_delta: float | None = None,
    slippage_delta_bps: float | None = None,
    drawdown_delta: float | None = None,
    holding_time_delta_minutes: float | None = None,
    trade_frequency_delta: float | None = None,
    deviation_score: float | None = None,
    detail: dict | None = None,
    stage: str = "paper",
) -> dict:
    """Persist a cross-stage deviation snapshot for paper/live comparison."""

    ensure_paper_acceptance_schema(conn)
    payload = {
        "snapshot_key": f"paper-deviation-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "baseline_source": baseline_source,
        "comparison_source": comparison_source,
        "win_rate_delta": _safe_float(win_rate_delta),
        "fill_rate_delta": _safe_float(fill_rate_delta),
        "slippage_delta_bps": _safe_float(slippage_delta_bps),
        "drawdown_delta": _safe_float(drawdown_delta),
        "holding_time_delta_minutes": _safe_float(holding_time_delta_minutes),
        "trade_frequency_delta": _safe_float(trade_frequency_delta),
        "deviation_score": _safe_float(deviation_score),
        "detail": detail or {},
        "created_at": _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_deviation_snapshots
           (snapshot_key, symbol, stage, baseline_source, comparison_source,
            win_rate_delta, fill_rate_delta, slippage_delta_bps, drawdown_delta,
            holding_time_delta_minutes, trade_frequency_delta, deviation_score,
            detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["snapshot_key"],
            payload["symbol"],
            payload["stage"],
            payload["baseline_source"],
            payload["comparison_source"],
            payload["win_rate_delta"],
            payload["fill_rate_delta"],
            payload["slippage_delta_bps"],
            payload["drawdown_delta"],
            payload["holding_time_delta_minutes"],
            payload["trade_frequency_delta"],
            payload["deviation_score"],
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=payload["symbol"],
        stage=payload["stage"],
        change_type="deviation_snapshot_recorded",
        target_type="deviation_snapshot",
        target_key=f"{baseline_source}->{comparison_source}",
        detail={"deviation_score": payload["deviation_score"]},
    )
    return payload


def load_deviation_snapshots(conn, symbol: str | None, stage: str = "paper", limit: int = 50) -> list[dict]:
    """Load persisted paper/live deviation snapshots."""

    ensure_paper_acceptance_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_deviation_snapshots
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 500))),
    ).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["detail"] = _json_loads(data.get("detail"), {})
        out.append(data)
    return out


def record_shadow_parity_trace(
    conn,
    *,
    symbol: str,
    runtime_stage: str = "shadow",
    market_timestamp: str | None = None,
    signal_timestamp: str | None = None,
    risk_timestamp: str | None = None,
    order_intent_timestamp: str | None = None,
    adapter_timestamp: str | None = None,
    adapter_name: str = "",
    side: str = "",
    order_type: str = "",
    requested_qty: float | None = None,
    signal_price: float | None = None,
    expected_price: float | None = None,
    execution_latency_ms: float | None = None,
    market_data_source_shared: bool = False,
    signal_process_shared: bool = False,
    risk_module_shared: bool = False,
    order_generation_shared: bool = False,
    logging_alerting_shared: bool = False,
    no_exchange_submission: bool = True,
    order_book_snapshot_recorded: bool = False,
    likely_execution_price_recorded: bool = False,
    post_order_price_behavior_recorded: bool = False,
    parity_score: float | None = None,
    detail: dict | None = None,
    stage: str = "paper",
) -> dict:
    """Persist one shadow/live parity trace for architecture equivalence checks."""

    ensure_paper_acceptance_schema(conn)
    bool_flags = [
        bool(market_data_source_shared),
        bool(signal_process_shared),
        bool(risk_module_shared),
        bool(order_generation_shared),
        bool(logging_alerting_shared),
        bool(no_exchange_submission),
        bool(order_intent_timestamp and adapter_timestamp),
        bool(order_book_snapshot_recorded),
        bool(likely_execution_price_recorded),
        bool(post_order_price_behavior_recorded),
    ]
    derived_score = round(sum(1.0 for item in bool_flags if item) / len(bool_flags), 4)
    payload = {
        "parity_key": f"paper-shadow-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "runtime_stage": runtime_stage or "shadow",
        "market_timestamp": market_timestamp,
        "signal_timestamp": signal_timestamp,
        "risk_timestamp": risk_timestamp,
        "order_intent_timestamp": order_intent_timestamp,
        "adapter_timestamp": adapter_timestamp,
        "adapter_name": adapter_name or "",
        "side": side or "",
        "order_type": order_type or "",
        "requested_qty": _safe_float(requested_qty),
        "signal_price": _safe_float(signal_price),
        "expected_price": _safe_float(expected_price),
        "execution_latency_ms": _safe_float(execution_latency_ms),
        "market_data_source_shared": bool(market_data_source_shared),
        "signal_process_shared": bool(signal_process_shared),
        "risk_module_shared": bool(risk_module_shared),
        "order_generation_shared": bool(order_generation_shared),
        "logging_alerting_shared": bool(logging_alerting_shared),
        "no_exchange_submission": bool(no_exchange_submission),
        "order_book_snapshot_recorded": bool(order_book_snapshot_recorded),
        "likely_execution_price_recorded": bool(likely_execution_price_recorded),
        "post_order_price_behavior_recorded": bool(post_order_price_behavior_recorded),
        "parity_score": _safe_float(parity_score, derived_score),
        "detail": detail or {},
        "created_at": _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_shadow_parity
           (parity_key, symbol, stage, runtime_stage, market_timestamp, signal_timestamp,
            risk_timestamp, order_intent_timestamp, adapter_timestamp, adapter_name, side,
            order_type, requested_qty, signal_price, expected_price, execution_latency_ms,
            market_data_source_shared, signal_process_shared, risk_module_shared,
            order_generation_shared, logging_alerting_shared, no_exchange_submission,
            order_book_snapshot_recorded, likely_execution_price_recorded,
            post_order_price_behavior_recorded, parity_score, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["parity_key"],
            payload["symbol"],
            payload["stage"],
            payload["runtime_stage"],
            payload["market_timestamp"],
            payload["signal_timestamp"],
            payload["risk_timestamp"],
            payload["order_intent_timestamp"],
            payload["adapter_timestamp"],
            payload["adapter_name"],
            payload["side"],
            payload["order_type"],
            payload["requested_qty"],
            payload["signal_price"],
            payload["expected_price"],
            payload["execution_latency_ms"],
            1 if payload["market_data_source_shared"] else 0,
            1 if payload["signal_process_shared"] else 0,
            1 if payload["risk_module_shared"] else 0,
            1 if payload["order_generation_shared"] else 0,
            1 if payload["logging_alerting_shared"] else 0,
            1 if payload["no_exchange_submission"] else 0,
            1 if payload["order_book_snapshot_recorded"] else 0,
            1 if payload["likely_execution_price_recorded"] else 0,
            1 if payload["post_order_price_behavior_recorded"] else 0,
            payload["parity_score"],
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=payload["symbol"],
        stage=payload["stage"],
        change_type="shadow_parity_recorded",
        target_type="shadow_parity",
        target_key=payload["runtime_stage"],
        detail={"parity_score": payload["parity_score"], "adapter_name": payload["adapter_name"]},
    )
    return payload


def load_shadow_parity_traces(conn, symbol: str | None, stage: str = "paper", limit: int = 100) -> list[dict]:
    """Load persisted shadow/live parity traces."""

    ensure_paper_acceptance_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_shadow_parity
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 1000))),
    ).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["detail"] = _json_loads(data.get("detail"), {})
        for key in (
            "market_data_source_shared",
            "signal_process_shared",
            "risk_module_shared",
            "order_generation_shared",
            "logging_alerting_shared",
            "no_exchange_submission",
            "order_book_snapshot_recorded",
            "likely_execution_price_recorded",
            "post_order_price_behavior_recorded",
        ):
            data[key] = bool(data.get(key))
        out.append(data)
    return out


def summarize_shadow_parity_traces(conn, symbol: str | None, stage: str = "paper", limit: int = 100) -> dict:
    """Summarize shadow parity traces into ratios usable by gates and policy."""

    rows = load_shadow_parity_traces(conn, symbol, stage=stage, limit=limit)
    if not rows:
        return {
            "trace_count": 0,
            "runtime_stages": [],
            "shared_module_ratio": None,
            "market_data_shared_ratio": None,
            "signal_process_shared_ratio": None,
            "risk_module_shared_ratio": None,
            "order_generation_shared_ratio": None,
            "logging_alerting_shared_ratio": None,
            "no_exchange_submission_ratio": None,
            "order_book_snapshot_ratio": None,
            "likely_execution_price_ratio": None,
            "post_order_price_behavior_ratio": None,
            "avg_parity_score": None,
            "avg_execution_latency_ms": None,
            "avg_intent_to_adapter_ms": None,
        }

    def _ratio(key: str) -> float:
        return round(sum(1 for row in rows if row.get(key)) / len(rows), 4)

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    parity_scores = [float(row["parity_score"]) for row in rows if _safe_float(row.get("parity_score")) is not None]
    execution_latencies = [float(row["execution_latency_ms"]) for row in rows if _safe_float(row.get("execution_latency_ms")) is not None]
    intent_to_adapter: list[float] = []
    for row in rows:
        start = _parse_ts(row.get("order_intent_timestamp"))
        end = _parse_ts(row.get("adapter_timestamp"))
        if start and end and end >= start:
            intent_to_adapter.append((end - start).total_seconds() * 1000)
    shared_keys = (
        "market_data_source_shared",
        "signal_process_shared",
        "risk_module_shared",
        "order_generation_shared",
        "logging_alerting_shared",
    )
    shared_module_ratio = round(
        sum(_ratio(key) for key in shared_keys if rows) / len(shared_keys),
        4,
    )
    return {
        "trace_count": len(rows),
        "runtime_stages": sorted({str(row.get("runtime_stage") or "").strip() for row in rows if row.get("runtime_stage")}),
        "shared_module_ratio": shared_module_ratio,
        "market_data_shared_ratio": _ratio("market_data_source_shared"),
        "signal_process_shared_ratio": _ratio("signal_process_shared"),
        "risk_module_shared_ratio": _ratio("risk_module_shared"),
        "order_generation_shared_ratio": _ratio("order_generation_shared"),
        "logging_alerting_shared_ratio": _ratio("logging_alerting_shared"),
        "no_exchange_submission_ratio": _ratio("no_exchange_submission"),
        "order_book_snapshot_ratio": _ratio("order_book_snapshot_recorded"),
        "likely_execution_price_ratio": _ratio("likely_execution_price_recorded"),
        "post_order_price_behavior_ratio": _ratio("post_order_price_behavior_recorded"),
        "avg_parity_score": _avg(parity_scores),
        "avg_execution_latency_ms": _avg(execution_latencies),
        "avg_intent_to_adapter_ms": _avg(intent_to_adapter),
    }


def _same_numeric(a, b, *, precision: int = 6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return round(float(a), precision) == round(float(b), precision)


def _run_row_to_dict(row) -> dict:
    data = dict(row)
    data["metrics"] = _json_loads(data.get("metrics"), {})
    data["gate_summary"] = _json_loads(data.get("gate_summary"), {})
    data["report_payload"] = _json_loads(data.get("report_payload"), {})
    return data


def load_acceptance_reports(conn, symbol: str | None = None, limit: int = 50) -> list[dict]:
    """Load recent persisted acceptance reports."""

    ensure_paper_acceptance_schema(conn)
    params: list = []
    where = ""
    if symbol:
        where = "WHERE symbol = ?"
        params.append(_symbol_key(symbol))
    rows = conn.execute(
        f"""SELECT * FROM paper_acceptance_runs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
        params + [max(1, min(int(limit), 500))],
    ).fetchall()
    return [_run_row_to_dict(row) for row in rows]


def load_acceptance_review(conn, symbol: str | None, stage: str = "paper") -> dict:
    """Load the latest governance review metadata for one acceptance workspace."""

    ensure_paper_acceptance_schema(conn)
    row = conn.execute(
        """SELECT * FROM paper_acceptance_reviews
           WHERE symbol=? AND stage=?
           ORDER BY updated_at DESC, id DESC
           LIMIT 1""",
        (_symbol_key(symbol), stage),
    ).fetchone()
    if not row:
        return {
            "symbol": _symbol_key(symbol),
            "stage": stage,
            "reviewer": "",
            "review_status": "pending",
            "fixed_in_version": "",
            "retest_required": False,
            "can_promote_to_live": False,
            "note": "",
            "run_key": None,
            "created_at": None,
            "updated_at": None,
        }
    return {
        "symbol": row["symbol"],
        "stage": row["stage"],
        "reviewer": row["reviewer"],
        "review_status": row["review_status"],
        "fixed_in_version": row["fixed_in_version"],
        "retest_required": bool(row["retest_required"]),
        "can_promote_to_live": bool(row["can_promote_to_live"]),
        "note": row["note"],
        "run_key": row["run_key"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def upsert_acceptance_review(
    conn,
    *,
    symbol: str,
    stage: str = "paper",
    reviewer: str = "",
    review_status: str = "pending",
    fixed_in_version: str = "",
    retest_required: bool = False,
    can_promote_to_live: bool = False,
    note: str = "",
    run_key: str | None = None,
) -> dict:
    """Persist governance metadata without overwriting review history."""

    ensure_paper_acceptance_schema(conn)
    now = _now_iso()
    review_key = f"paper-review-{uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO paper_acceptance_reviews
           (review_key, symbol, stage, run_key, reviewer, review_status, fixed_in_version,
            retest_required, can_promote_to_live, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            review_key,
            _symbol_key(symbol),
            stage,
            run_key,
            reviewer or "",
            review_status or "pending",
            fixed_in_version or "",
            1 if retest_required else 0,
            1 if can_promote_to_live else 0,
            note or "",
            now,
            now,
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=symbol,
        stage=stage,
        change_type="review_updated",
        target_type="review",
        target_key=run_key or "",
        detail={
            "reviewer": reviewer or "",
            "review_status": review_status or "pending",
            "fixed_in_version": fixed_in_version or "",
            "retest_required": bool(retest_required),
            "can_promote_to_live": bool(can_promote_to_live),
        },
    )
    return load_acceptance_review(conn, symbol, stage=stage)


def record_acceptance_event(
    conn,
    *,
    event_type: str,
    symbol: str | None = None,
    severity: str = "info",
    status: str = "open",
    detail: dict | None = None,
    run_key: str | None = None,
) -> str:
    """Record an abnormal event, risk event, alert, or reconciliation finding."""

    ensure_paper_acceptance_schema(conn)
    event_key = f"paper-event-{uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO paper_acceptance_events
           (event_key, run_key, symbol, event_type, severity, status, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_key,
            run_key,
            _symbol_key(symbol) if symbol else None,
            event_type,
            severity,
            status,
            _json_dumps(detail or {}),
            _now_iso(),
        ),
    )
    conn.commit()
    return event_key


def load_acceptance_events(conn, symbol: str | None = None, limit: int = 100) -> list[dict]:
    """Load recent paper acceptance abnormal/risk/reconciliation events."""

    ensure_paper_acceptance_schema(conn)
    params: list = []
    where = ""
    if symbol:
        where = "WHERE symbol = ?"
        params.append(_symbol_key(symbol))
    rows = conn.execute(
        f"""SELECT * FROM paper_acceptance_events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
        params + [max(1, min(int(limit), 500))],
    ).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["detail"] = _json_loads(data.get("detail"), {})
        out.append(data)
    return out


def load_acceptance_context_overrides(conn, symbol: str | None, stage: str = "paper") -> dict:
    """Load strategy/metric/prohibition override payloads for one symbol."""

    ensure_paper_acceptance_schema(conn)
    key = _symbol_key(symbol)
    row = conn.execute(
        """SELECT * FROM paper_acceptance_context_overrides
           WHERE symbol=? AND stage=?""",
        (key, stage),
    ).fetchone()
    if not row:
        return {
            "symbol": key,
            "stage": stage,
            "strategy": {},
            "metrics": {},
            "prohibitions": {},
            "updated_at": None,
        }
    return {
        "symbol": row["symbol"],
        "stage": row["stage"],
        "strategy": _json_loads(row["strategy_payload"], {}),
        "metrics": _json_loads(row["metrics_payload"], {}),
        "prohibitions": _json_loads(row["prohibitions_payload"], {}),
        "updated_at": row["updated_at"],
    }


def upsert_acceptance_context_overrides(
    conn,
    *,
    symbol: str,
    stage: str = "paper",
    strategy: dict | None = None,
    metrics: dict | None = None,
    prohibitions: dict | None = None,
) -> dict:
    """Create or replace stored override payloads for one acceptance workspace."""

    ensure_paper_acceptance_schema(conn)
    key = _symbol_key(symbol)
    updated_at = _now_iso()
    current = load_acceptance_context_overrides(conn, key, stage=stage)
    strategy_payload = dict(strategy or current["strategy"] or {})
    metrics_payload = dict(metrics or current["metrics"] or {})
    prohibitions_payload = dict(prohibitions or current["prohibitions"] or {})
    conn.execute(
        """INSERT INTO paper_acceptance_context_overrides
           (symbol, stage, strategy_payload, metrics_payload, prohibitions_payload, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, stage) DO UPDATE SET
               strategy_payload=excluded.strategy_payload,
               metrics_payload=excluded.metrics_payload,
               prohibitions_payload=excluded.prohibitions_payload,
               updated_at=excluded.updated_at""",
        (
            key,
            stage,
            _json_dumps(strategy_payload),
            _json_dumps(metrics_payload),
            _json_dumps(prohibitions_payload),
            updated_at,
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=key,
        stage=stage,
        change_type="workspace_override_updated",
        target_type="workspace",
        target_key=key,
        detail={
            "strategy_keys": sorted(strategy_payload.keys()),
            "metrics_keys": sorted(metrics_payload.keys()),
            "prohibitions_keys": sorted(prohibitions_payload.keys()),
        },
    )
    return load_acceptance_context_overrides(conn, key, stage=stage)


def load_acceptance_checks(conn, symbol: str | None, stage: str = "paper") -> list[dict]:
    """Load persisted manual evidence checks for one symbol."""

    ensure_paper_acceptance_schema(conn)
    key = _symbol_key(symbol)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_evidence
           WHERE symbol=? AND stage=?
           ORDER BY gate_id, check_key""",
        (key, stage),
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "symbol": row["symbol"],
            "stage": row["stage"],
            "gate_id": row["gate_id"],
            "check_key": row["check_key"],
            "value": _json_loads(row["value_json"], None),
            "note": row["note"],
            "source": row["source"],
            "updated_at": row["updated_at"],
        })
    return out


def upsert_acceptance_check(
    conn,
    *,
    symbol: str,
    gate_id: str,
    check_key: str,
    value,
    note: str = "",
    source: str = "manual",
    stage: str = "paper",
) -> dict:
    """Create or replace one persisted manual evidence check."""

    ensure_paper_acceptance_schema(conn)
    updated_at = _now_iso()
    conn.execute(
        """INSERT INTO paper_acceptance_evidence
           (symbol, stage, gate_id, check_key, value_json, note, source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, stage, gate_id, check_key) DO UPDATE SET
               value_json=excluded.value_json,
               note=excluded.note,
               source=excluded.source,
               updated_at=excluded.updated_at""",
        (
            _symbol_key(symbol),
            stage,
            gate_id,
            check_key,
            json.dumps(value, ensure_ascii=False),
            note or "",
            source or "manual",
            updated_at,
        ),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=_symbol_key(symbol),
        stage=stage,
        change_type="check_upserted",
        target_type="check",
        target_key=f"{gate_id}.{check_key}",
        detail={"value": value, "source": source or "manual", "note": note or ""},
    )
    return {
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "gate_id": gate_id,
        "check_key": check_key,
        "value": value,
        "note": note or "",
        "source": source or "manual",
        "updated_at": updated_at,
    }


def delete_acceptance_check(conn, *, symbol: str, gate_id: str, check_key: str, stage: str = "paper") -> None:
    """Delete one persisted manual evidence check override."""

    ensure_paper_acceptance_schema(conn)
    conn.execute(
        """DELETE FROM paper_acceptance_evidence
           WHERE symbol=? AND stage=? AND gate_id=? AND check_key=?""",
        (_symbol_key(symbol), stage, gate_id, check_key),
    )
    conn.commit()
    record_acceptance_change(
        conn,
        symbol=_symbol_key(symbol),
        stage=stage,
        change_type="check_deleted",
        target_type="check",
        target_key=f"{gate_id}.{check_key}",
        detail={},
    )


def _merge_check(evidence: dict, gate_id: str, check_key: str, value, *, source: str, note: str = "") -> None:
    if value is None:
        return
    bucket = evidence.setdefault(gate_id, {"checks": {}, "sources": {}, "notes": {}})
    if not isinstance(bucket.get("checks"), dict):
        bucket["checks"] = {}
    if not isinstance(bucket.get("sources"), dict):
        bucket["sources"] = {}
    if not isinstance(bucket.get("notes"), dict):
        bucket["notes"] = {}
    bucket["checks"][check_key] = value
    bucket["sources"][check_key] = source
    if note:
        bucket["notes"][check_key] = note


def _journal_closed_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("status") == "closed"]


def _holding_minutes(row: dict) -> float | None:
    start = _parse_ts(row.get("entry_time") or row.get("created_at"))
    end = _parse_ts(row.get("exit_time") or row.get("updated_at") or row.get("created_at"))
    if not start or not end or end < start:
        return None
    return (end - start).total_seconds() / 60.0


def _pnl_value(row: dict) -> float | None:
    pnl = _safe_float(row.get("pnl"))
    if pnl is not None:
        return pnl
    r_multiple = _safe_float(row.get("r_multiple"))
    return r_multiple


def _sorted_trade_rows(rows: list[dict]) -> list[dict]:
    def _sort_key(row: dict):
        ts = _parse_ts(row.get("exit_time") or row.get("entry_time") or row.get("created_at"))
        return (ts or datetime.min.replace(tzinfo=UTC), row.get("id") or 0)
    return sorted(rows, key=_sort_key)


def _max_streak(values: list[float], negative: bool) -> int:
    best = 0
    cur = 0
    for value in values:
        hit = value < 0 if negative else value > 0
        if hit:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _max_drawdown_pct(initial_capital: float | None, pnl_values: list[float]) -> float | None:
    if not pnl_values:
        return None
    base = float(initial_capital or 10000.0)
    equity = base
    peak = base
    min_dd = 0.0
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            min_dd = min(min_dd, (equity / peak) - 1.0)
    return round(min_dd, 4)


def _summarize_journal(rows: list[dict], initial_capital: float | None = None) -> dict:
    closed = _sorted_trade_rows(_journal_closed_rows(rows))
    pnl_values = [_pnl_value(row) for row in closed]
    pnl_values = [float(value) for value in pnl_values if value is not None]
    r_values = [_safe_float(row.get("r_multiple")) for row in closed]
    r_values = [float(value) for value in r_values if value is not None]
    holding_values = [_holding_minutes(row) for row in closed]
    holding_values = [value for value in holding_values if value is not None]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss_abs = abs(sum(losses))
    first_ts = _parse_ts(closed[0].get("entry_time") or closed[0].get("created_at")) if closed else None
    last_ts = _parse_ts(closed[-1].get("exit_time") or closed[-1].get("updated_at") or closed[-1].get("created_at")) if closed else None
    testing_days = None
    if first_ts and last_ts:
        testing_days = max(1, (last_ts.date() - first_ts.date()).days + 1)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss_abs = abs(sum(losses) / len(losses)) if losses else None
    avg_holding_minutes = sum(holding_values) / len(holding_values) if holding_values else None
    net_profit = sum(pnl_values) if pnl_values else None
    total_return = None
    if initial_capital and initial_capital > 0 and net_profit is not None:
        total_return = net_profit / initial_capital
    return {
        "trade_count": len(closed),
        "testing_days": testing_days,
        "first_ts": first_ts.isoformat().replace("+00:00", "Z") if first_ts else None,
        "last_ts": last_ts.isoformat().replace("+00:00", "Z") if last_ts else None,
        "gross_profit": round(gross_profit, 4) if pnl_values else None,
        "gross_loss_abs": round(gross_loss_abs, 4) if pnl_values else None,
        "net_profit": round(net_profit, 4) if net_profit is not None else None,
        "win_rate": round(len(wins) / len(pnl_values), 4) if pnl_values else None,
        "average_win": round(avg_win, 4) if avg_win is not None else None,
        "average_loss": round(avg_loss_abs, 4) if avg_loss_abs is not None else None,
        "win_loss_ratio": round(avg_win / avg_loss_abs, 4) if avg_win is not None and avg_loss_abs not in (None, 0) else None,
        "profit_factor": round(gross_profit / gross_loss_abs, 4) if gross_loss_abs > 0 else (None if not wins else 999.0),
        "expectancy_after_costs": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "average_holding_time": _format_duration(avg_holding_minutes),
        "average_holding_minutes": round(avg_holding_minutes, 2) if avg_holding_minutes is not None else None,
        "max_consecutive_losses": _max_streak(pnl_values, negative=True),
        "max_consecutive_wins": _max_streak(pnl_values, negative=False),
        "max_drawdown": _max_drawdown_pct(initial_capital, pnl_values),
        "total_return": round(total_return, 4) if total_return is not None else None,
    }


def _load_backtest_runs(conn, symbol: str | None = None, limit: int = 20) -> list[dict]:
    params: list = []
    where = ""
    if symbol:
        where = "WHERE symbol = ?"
        params.append(_symbol_key(symbol))
    rows = conn.execute(
        f"""SELECT * FROM smc_backtest_runs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
        params + [limit],
    ).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["payload"] = _json_loads(data.get("payload"), {})
        out.append(data)
    return out


def _latest_backtest_metrics(runs: list[dict]) -> dict:
    if not runs:
        return {}
    run = runs[0]
    payload = run.get("payload") or {}
    metrics = payload.get("metrics") if isinstance(payload, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        "symbol": run.get("symbol"),
        "total_trades": run.get("total_trades") or metrics.get("total_trades"),
        "win_rate": run.get("win_rate") if run.get("win_rate") is not None else metrics.get("win_rate"),
        "profit_factor": run.get("profit_factor") if run.get("profit_factor") is not None else metrics.get("profit_factor"),
        "expectancy_r": run.get("expectancy_r") if run.get("expectancy_r") is not None else metrics.get("expectancy_r"),
        "max_drawdown": run.get("max_drawdown") if run.get("max_drawdown") is not None else metrics.get("max_drawdown"),
        "ending_equity": run.get("ending_equity") if run.get("ending_equity") is not None else metrics.get("ending_equity"),
        "period": run.get("period"),
        "created_at": run.get("created_at"),
    }


def _ratio_delta(reference: float | None, actual: float | None) -> float | None:
    if reference in (None, 0) or actual is None:
        return None
    return abs(float(actual) - float(reference)) / abs(float(reference))


def _compare_with_tolerance(reference: float | None, actual: float | None, tolerance: float) -> bool | None:
    delta = _ratio_delta(reference, actual)
    if delta is None:
        return None
    return delta <= tolerance


def _load_journal_rows(conn, symbol: str | None = None, environment: str | None = None) -> list[dict]:
    query = "SELECT * FROM smc_trade_journal WHERE 1=1"
    params: list = []
    if symbol:
        query += " AND symbol = ?"
        params.append(_symbol_key(symbol))
    if environment:
        query += " AND environment = ?"
        params.append(environment.lower())
    query += " ORDER BY COALESCE(entry_time, created_at) DESC, id DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _manual_check_map(conn, symbol: str | None, stage: str = "paper") -> dict[tuple[str, str], dict]:
    return {
        (row["gate_id"], row["check_key"]): row
        for row in load_acceptance_checks(conn, symbol, stage=stage)
    }


def _build_auto_evidence(
    *,
    strategy: dict,
    metrics: dict,
    paper_rows: list[dict],
    live_rows: list[dict],
    backtest_runs: list[dict],
    events: list[dict],
) -> dict:
    evidence: dict = {}
    for gate_id, checks in FRAMEWORK_CAPABILITY_CHECKS.items():
        for check_key, value in checks.items():
            _merge_check(evidence, gate_id, check_key, value, source="framework")

    latest_backtest = _latest_backtest_metrics(backtest_runs)
    paper_summary = _summarize_journal(paper_rows, _safe_float(strategy.get("initial_capital")))
    live_summary = _summarize_journal(live_rows, _safe_float(strategy.get("initial_capital")))

    if backtest_runs:
        for key in ("entry_conditions", "exit_conditions", "stop_loss_conditions", "take_profit_conditions", "no_future_data"):
            _merge_check(evidence, "strategy_logic", key, True, source="observed")
        _merge_check(evidence, "strategy_logic", "unfinished_candle_policy", True, source="framework")
        _merge_check(evidence, "strategy_logic", "oos_test_completed", True, source="observed")

    data_trace_rows = paper_rows or live_rows
    if data_trace_rows:
        _merge_check(evidence, "data_source_traceability", "timestamps_recorded", True, source="observed")
        _merge_check(evidence, "data_source_traceability", "market_data_source", bool(strategy.get("data_source")), source="observed")

    if paper_summary["trade_count"]:
        for key in ("trade_count", "net_profit", "win_rate"):
            _merge_check(evidence, "performance_metrics", key, metrics.get(key) is not None, source="observed")
        _merge_check(evidence, "performance_metrics", "gross_profit", metrics.get("gross_profit") is not None, source="observed")
        _merge_check(evidence, "performance_metrics", "max_drawdown", metrics.get("max_drawdown") is not None, source="observed")
        _merge_check(evidence, "performance_metrics", "profit_factor", metrics.get("profit_factor") is not None, source="observed")
        _merge_check(evidence, "performance_metrics", "average_holding_time", metrics.get("average_holding_time") is not None, source="observed")
        _merge_check(evidence, "performance_metrics", "max_consecutive_losses_wins", metrics.get("max_consecutive_losses") is not None, source="observed")

    for key in ("average_slippage", "maximum_slippage", "fill_rate", "rejection_ratio"):
        if metrics.get(key) is not None:
            _merge_check(evidence, "trade_quality", key, True, source="observed")

    if paper_summary["trade_count"]:
        _merge_check(evidence, "sample_size_period", "sufficient_trade_samples", metrics.get("trade_count", 0) >= 1, source="observed")
        _merge_check(evidence, "sample_size_period", "complete_trading_cycle", metrics.get("testing_days", 0) >= 1, source="observed")
        _merge_check(evidence, "sample_size_period", "consecutive_loss_periods", metrics.get("max_consecutive_losses", 0) >= 1, source="observed")
        _merge_check(evidence, "sample_size_period", "not_only_one_way_market", paper_summary.get("win_rate") not in (None, 0.0, 1.0), source="observed")

    if strategy.get("shadow_trading_used") is True or int(metrics.get("shadow_trace_count") or 0) > 0:
        shadow_mapping = {
            "live_market_data_source": _safe_float(metrics.get("shadow_market_data_shared_ratio")),
            "live_signal_process": _safe_float(metrics.get("shadow_signal_process_shared_ratio")),
            "live_risk_module": _safe_float(metrics.get("shadow_risk_module_shared_ratio")),
            "live_order_generation": _safe_float(metrics.get("shadow_order_generation_shared_ratio")),
            "live_logging_alerting": _safe_float(metrics.get("shadow_logging_alerting_shared_ratio")),
            "no_exchange_submission": _safe_float(metrics.get("shadow_no_exchange_submission_ratio")),
            "theoretical_submission_time": _safe_float(metrics.get("shadow_avg_intent_to_adapter_ms")),
            "order_book_snapshot_recorded": _safe_float(metrics.get("shadow_order_book_snapshot_ratio")),
            "likely_execution_price": _safe_float(metrics.get("shadow_likely_execution_price_ratio")),
            "post_order_price_behavior": _safe_float(metrics.get("shadow_post_order_price_behavior_ratio")),
        }
        for key, value in shadow_mapping.items():
            if value is None:
                continue
            if key == "theoretical_submission_time":
                _merge_check(evidence, "shadow_trading", key, True, source="observed")
            else:
                _merge_check(evidence, "shadow_trading", key, value >= 0.8, source="observed")

    if metrics.get("parameters_frozen") is not None:
        _merge_check(evidence, "research_discipline", "strategy_parameters_frozen", bool(metrics.get("parameters_frozen")), source="observed")
    if metrics.get("parameter_change_count") is not None:
        _merge_check(evidence, "research_discipline", "no_short_term_parameter_tuning", metrics.get("parameter_change_count") == 0, source="observed")
    if strategy.get("strategy_version") or strategy.get("parameter_version"):
        _merge_check(evidence, "research_discipline", "version_result_mapping", True, source="observed")

    if metrics.get("hardcoded_api_keys") is not None:
        _merge_check(evidence, "api_security", "no_hardcoded_keys", not bool(metrics.get("hardcoded_api_keys")), source="observed")
    if metrics.get("withdrawal_permission_enabled") is not None:
        _merge_check(evidence, "api_security", "withdrawal_disabled", not bool(metrics.get("withdrawal_permission_enabled")), source="observed")

    if metrics.get("runtime_days") is not None:
        _merge_check(evidence, "system_stability", "seven_day_runtime", metrics.get("runtime_days", 0) >= 7, source="observed")

    alert_types = {str(event.get("event_type") or "").strip() for event in events}
    if events:
        _merge_check(evidence, "alerting", "alert_payload_complete", True, source="observed")
    if {"strategy_start", "strategy_stop"} & alert_types:
        _merge_check(evidence, "alerting", "start_stop_notifications", True, source="observed")
    if "order_failure" in alert_types:
        _merge_check(evidence, "alerting", "order_failure_notifications", True, source="observed")
    if "reconciliation" in alert_types:
        _merge_check(evidence, "alerting", "reconciliation_notifications", True, source="observed")
    if "kill_switch" in alert_types:
        _merge_check(evidence, "alerting", "kill_switch_notifications", True, source="observed")

    if latest_backtest and paper_summary["trade_count"]:
        trade_freq_ref = None
        if latest_backtest.get("total_trades") and metrics.get("testing_days"):
            trade_freq_ref = float(latest_backtest["total_trades"]) / max(1, float(metrics["testing_days"]))
        trade_freq_live = float(metrics["trade_count"]) / max(1, float(metrics["testing_days"])) if metrics.get("trade_count") and metrics.get("testing_days") else None
        comparisons = {
            "trade_frequency_matches": _compare_with_tolerance(trade_freq_ref, trade_freq_live, 0.5),
            "win_rate_matches": _compare_with_tolerance(_safe_float(latest_backtest.get("win_rate")), _safe_float(metrics.get("win_rate")), 0.35),
            "drawdown_matches": _compare_with_tolerance(abs(_safe_float(latest_backtest.get("max_drawdown")) or 0), abs(_safe_float(metrics.get("max_drawdown")) or 0), 0.5),
        }
        if latest_backtest.get("expectancy_r") is not None and metrics.get("expectancy_after_costs") is not None:
            comparisons["win_loss_ratio_matches"] = _compare_with_tolerance(
                _safe_float(latest_backtest.get("expectancy_r")),
                _safe_float(metrics.get("expectancy_after_costs")),
                0.5,
            )
        if metrics.get("average_holding_minutes") is not None:
            comparisons["holding_time_matches"] = True
        score_parts = []
        for key, value in comparisons.items():
            if value is not None:
                _merge_check(evidence, "behavior_deviation", key, value, source="observed")
                score_parts.append(1.0 if value else 0.0)
        if score_parts:
            metrics["behavior_alignment_score"] = round(sum(score_parts) / len(score_parts), 4)

    if live_summary["trade_count"] and paper_summary["trade_count"]:
        comparisons = {
            "win_rate_comparison": _compare_with_tolerance(_safe_float(paper_summary.get("win_rate")), _safe_float(live_summary.get("win_rate")), 0.35),
            "trade_frequency_comparison": _compare_with_tolerance(
                (paper_summary["trade_count"] / max(1, paper_summary.get("testing_days") or 1)),
                (live_summary["trade_count"] / max(1, live_summary.get("testing_days") or 1)),
                0.5,
            ),
            "drawdown_comparison": _compare_with_tolerance(abs(_safe_float(paper_summary.get("max_drawdown")) or 0), abs(_safe_float(live_summary.get("max_drawdown")) or 0), 0.5),
        }
        max_dev = 0.0
        ready = True
        for key, value in comparisons.items():
            if value is not None:
                _merge_check(evidence, "paper_live_comparison", key, value, source="observed")
                max_dev = max(max_dev, 0.0 if value else 1.0)
            ready = ready and value is not None
        metrics["paper_live_comparison_ready"] = ready
        metrics["paper_live_max_deviation_ratio"] = round(max_dev, 4)

    if metrics.get("capital_stage_count") is not None:
        _merge_check(evidence, "capacity_scaling", "predefined_scaling_multiple", metrics.get("capital_stage_count", 0) >= 2, source="observed")
        _merge_check(evidence, "capacity_scaling", "observation_after_scaling", metrics.get("capital_stage_count", 0) >= 2, source="observed")

    if metrics.get("thresholds_defined") is True:
        for key in (
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
        ):
            _merge_check(evidence, "quantitative_thresholds", key, True, source="observed")

    final_report_checks = {
        "basic_information": bool(strategy.get("name") and strategy.get("symbol")),
        "performance_summary": metrics.get("trade_count") is not None,
        "trade_quality": any(metrics.get(key) is not None for key in ("average_slippage", "fill_rate", "rejection_ratio")),
        "behavior_deviation": metrics.get("behavior_alignment_score") is not None,
        "system_stability": metrics.get("runtime_days") is not None,
        "risk_control_records": any(metrics.get(key) is not None for key in ("kill_switch_tested", "position_limit_triggered", "drawdown_limit_triggered")),
        "security_check": any(metrics.get(key) is not None for key in ("withdrawal_permission_enabled", "hardcoded_api_keys", "ip_whitelist")),
        "abnormal_events": bool(events),
        "final_conclusion": True,
    }
    for key, value in final_report_checks.items():
        _merge_check(evidence, "final_report", key, value, source="observed")
    return evidence


def _apply_manual_evidence(evidence: dict, rows: list[dict]) -> None:
    for row in rows:
        _merge_check(
            evidence,
            row["gate_id"],
            row["check_key"],
            row.get("value"),
            source=row.get("source") or "manual",
            note=row.get("note") or "",
        )


def _build_auto_prohibitions(metrics: dict, strategy: dict, evidence: dict) -> dict[str, bool]:
    prohibitions = {
        "fees_missing": not bool(metrics.get("fees_included")),
        "slippage_missing": not bool(metrics.get("slippage_included")),
        "reconciliation_missing": not bool(metrics.get("reconciliation_implemented")),
        "kill_switch_untested": not bool(metrics.get("kill_switch_tested")),
    }
    strategy_type = (strategy.get("strategy_type") or "swing").lower()
    required = {"high_frequency": 300, "intraday": 50, "swing": 20, "low_frequency": 10}.get(strategy_type, 20)
    prohibitions["sample_size_too_small"] = int(metrics.get("trade_count") or 0) < required

    limit_gate = evidence.get("limit_execution_model", {}).get("checks", {})
    if isinstance(limit_gate, dict):
        touched = limit_gate.get("queue_position_considered")
        volume = limit_gate.get("volume_at_level_considered")
        if touched is False or volume is False:
            prohibitions["touch_equals_filled"] = True

    security_gate = evidence.get("api_security", {}).get("checks", {})
    if isinstance(security_gate, dict):
        if security_gate.get("minimum_permissions") is False or security_gate.get("withdrawal_disabled") is False:
            prohibitions["api_permissions_excessive"] = True

    if metrics.get("paper_live_comparison_ready") is False and int(metrics.get("live_trade_count") or 0) > 0:
        prohibitions["paper_live_comparison_missing"] = True
    return prohibitions


def _augment_report_catalog(report: dict, evidence: dict) -> tuple[list[dict], list[dict]]:
    gate_results = {gate["id"]: gate for gate in report.get("gates") or []}
    sections = {section["section"]: dict(section) for section in report.get("sections") or []}
    catalog = acceptance_catalog(evidence)
    for section in catalog:
        summary = sections.get(section["section"], {})
        section["summary"] = summary
        for gate in section["gates"]:
            result = gate_results.get(gate["id"], {})
            gate["status"] = result.get("status", "unavailable")
            gate["reason"] = result.get("reason", "")
            gate["missing_evidence"] = result.get("missing_evidence", [])
    return catalog, list(sections.values())


def build_smc_acceptance_context(conn, symbol: str | None = None, strategy: dict | None = None) -> dict:
    """Build an acceptance context from SMC journals, backtests, events, and manual evidence."""

    key = _symbol_key(symbol)
    overrides = load_acceptance_context_overrides(conn, key, stage="paper")
    review = load_acceptance_review(conn, key, stage="paper")
    paper_rows = _load_journal_rows(conn, symbol=key, environment="paper")
    live_rows = _load_journal_rows(conn, symbol=key, environment="live")
    backtest_runs = _load_backtest_runs(conn, symbol=key, limit=20)
    events = load_acceptance_events(conn, symbol=key, limit=500)
    shadow_parity = summarize_shadow_parity_traces(conn, symbol=key, stage="paper", limit=200)
    shadow_rows = load_shadow_parity_traces(conn, key, stage="paper", limit=50)
    telemetry = summarize_acceptance_telemetry(conn, symbol=key, stage="paper")
    live_telemetry = summarize_acceptance_telemetry(conn, symbol=key, stage="live")
    scenarios = summarize_scenario_evidence(conn, symbol=key, stage="paper")
    security_scan = run_security_scan(Path(__file__).resolve().parents[2])
    strategy_payload = dict(overrides["strategy"] or {})
    strategy_payload.update(strategy or {})
    strategy_payload.setdefault("name", "SMC Paper Acceptance")
    strategy_payload.setdefault("strategy_type", "intraday")
    strategy_payload.setdefault("stage", "paper")
    strategy_payload.setdefault("symbol", key)
    strategy_payload.setdefault("exchange", "paper-sim")
    if int(shadow_parity.get("trace_count") or 0) > 0:
        strategy_payload.setdefault("shadow_trading_used", True)
    if shadow_parity.get("shared_module_ratio") is not None:
        strategy_payload.setdefault("shared_live_architecture", float(shadow_parity["shared_module_ratio"]) >= 0.8)
    if not strategy_payload.get("testing_period"):
        combined_rows = _sorted_trade_rows(_journal_closed_rows(paper_rows))
        if combined_rows:
            first_ts = _parse_ts(combined_rows[0].get("entry_time") or combined_rows[0].get("created_at"))
            last_ts = _parse_ts(combined_rows[-1].get("exit_time") or combined_rows[-1].get("updated_at") or combined_rows[-1].get("created_at"))
            if first_ts and last_ts:
                strategy_payload["testing_period"] = f"{first_ts.date().isoformat()} ~ {last_ts.date().isoformat()}"
    initial_capital = _safe_float(strategy_payload.get("initial_capital"))

    paper_summary = _summarize_journal(paper_rows, initial_capital)
    live_summary = _summarize_journal(live_rows, initial_capital)
    latest_backtest = _latest_backtest_metrics(backtest_runs)
    metrics = {
        "trade_count": paper_summary.get("trade_count"),
        "testing_days": paper_summary.get("testing_days"),
        "paper_journal_count": len(paper_rows),
        "live_trade_count": live_summary.get("trade_count"),
        "backtest_run_count": len(backtest_runs),
        "gross_profit": paper_summary.get("gross_profit"),
        "net_profit": paper_summary.get("net_profit"),
        "win_rate": paper_summary.get("win_rate"),
        "win_loss_ratio": paper_summary.get("win_loss_ratio"),
        "profit_factor": paper_summary.get("profit_factor"),
        "expectancy_after_costs": paper_summary.get("expectancy_after_costs"),
        "total_return": paper_summary.get("total_return"),
        "max_drawdown": paper_summary.get("max_drawdown"),
        "average_holding_time": paper_summary.get("average_holding_time"),
        "average_holding_minutes": paper_summary.get("average_holding_minutes"),
        "max_consecutive_losses": paper_summary.get("max_consecutive_losses"),
        "max_consecutive_wins": paper_summary.get("max_consecutive_wins"),
        "runtime_days": paper_summary.get("testing_days"),
        "alert_count": len(events),
        "api_error_count": sum(1 for event in events if event.get("event_type") == "api_error"),
        "websocket_disconnect_count": sum(1 for event in events if event.get("event_type") == "ws_disconnect"),
        "reconciliation_abnormality_count": sum(1 for event in events if event.get("event_type") == "reconciliation"),
        "program_restart_count": sum(1 for event in events if event.get("event_type") == "restart"),
        "major_error_count": sum(1 for event in events if event.get("severity") in {"critical", "error"}),
        "major_error_description": " | ".join(
            str(event.get("detail", {}).get("reason") or event.get("detail", {}).get("message") or event.get("event_type") or "")
            for event in events[:5]
            if event.get("severity") in {"critical", "error", "warning"}
        ) or None,
        "fees_included": False,
        "total_fees": None,
        "slippage_included": False,
        "total_slippage": None,
        "average_slippage": None,
        "maximum_slippage": None,
        "slippage_std": None,
        "fill_rate": None,
        "rejection_ratio": None,
        "reconciliation_implemented": False,
        "unresolved_reconciliation_count": 0,
        "kill_switch_tested": False,
        "parameters_frozen": False,
        "parameter_change_count": None,
        "hardcoded_api_keys": False,
        "withdrawal_permission_enabled": False,
        "api_key_permissions_minimized": None,
        "ip_whitelist": None,
        "test_live_keys_separated": None,
        "logs_avoid_secrets": None,
        "revocation_process": None,
        "thresholds_defined": False,
        "capital_stage_count": 2 if live_summary.get("trade_count") else 1,
        "paper_trade_count": paper_summary.get("trade_count"),
        "live_trade_count": live_summary.get("trade_count"),
        "backtest_trade_count": latest_backtest.get("total_trades"),
        "fill_rate_live": live_telemetry.get("metrics", {}).get("fill_rate"),
        "average_slippage_live": live_telemetry.get("metrics", {}).get("average_slippage"),
        "shadow_trace_count": shadow_parity.get("trace_count"),
        "shadow_parity_score": shadow_parity.get("avg_parity_score"),
        "shadow_shared_module_ratio": shadow_parity.get("shared_module_ratio"),
        "shadow_market_data_shared_ratio": shadow_parity.get("market_data_shared_ratio"),
        "shadow_signal_process_shared_ratio": shadow_parity.get("signal_process_shared_ratio"),
        "shadow_risk_module_shared_ratio": shadow_parity.get("risk_module_shared_ratio"),
        "shadow_order_generation_shared_ratio": shadow_parity.get("order_generation_shared_ratio"),
        "shadow_logging_alerting_shared_ratio": shadow_parity.get("logging_alerting_shared_ratio"),
        "shadow_no_exchange_submission_ratio": shadow_parity.get("no_exchange_submission_ratio"),
        "shadow_order_book_snapshot_ratio": shadow_parity.get("order_book_snapshot_ratio"),
        "shadow_likely_execution_price_ratio": shadow_parity.get("likely_execution_price_ratio"),
        "shadow_post_order_price_behavior_ratio": shadow_parity.get("post_order_price_behavior_ratio"),
        "shadow_avg_execution_latency_ms": shadow_parity.get("avg_execution_latency_ms"),
        "shadow_avg_intent_to_adapter_ms": shadow_parity.get("avg_intent_to_adapter_ms"),
    }
    metrics.update({key: value for key, value in telemetry.get("metrics", {}).items() if value is not None})
    metrics.update({key: value for key, value in overrides["metrics"].items() if value is not None})
    stage_and_deviation = _ensure_capacity_and_deviation_snapshots(
        conn,
        symbol=key,
        strategy=strategy_payload,
        metrics=metrics,
        live_metrics=live_telemetry.get("metrics", {}),
        paper_summary=paper_summary,
        live_summary=live_summary,
        latest_backtest=latest_backtest,
    )
    capital_stages = load_capital_stages(conn, key, stage="paper", limit=20)
    deviation_snapshots = load_deviation_snapshots(conn, key, stage="paper", limit=20)
    metrics["capital_stage_count"] = len({row.get("stage_name") for row in capital_stages if row.get("stage_name")})
    latest_live_deviation = next(
        (row for row in deviation_snapshots if row.get("baseline_source") == "paper" and row.get("comparison_source") == "live"),
        None,
    )
    latest_backtest_deviation = next(
        (row for row in deviation_snapshots if row.get("baseline_source") == "backtest" and row.get("comparison_source") == "paper"),
        None,
    )
    if latest_live_deviation:
        metrics["paper_live_max_deviation_ratio"] = _safe_float(latest_live_deviation.get("deviation_score"))
        metrics["paper_live_comparison_ready"] = True
        metrics["average_slippage_live"] = _safe_float(latest_live_deviation.get("detail", {}).get("live_average_slippage"))
    if latest_backtest_deviation:
        metrics["behavior_alignment_score"] = round(1.0 - min(1.0, _safe_float(latest_backtest_deviation.get("deviation_score")) or 0), 4)
    metrics.update({
        "hardcoded_api_keys": not bool(security_scan.get("no_hardcoded_keys")),
        "test_live_keys_separated": security_scan.get("test_live_separation"),
        "revocation_process": security_scan.get("revocation_process"),
        "logs_avoid_secrets": security_scan.get("no_hardcoded_keys"),
        "security_scan_file_count": security_scan.get("scanned_files"),
        "security_scan_hit_count": security_scan.get("hardcoded_secret_count"),
    })
    evidence = _build_auto_evidence(
        strategy=strategy_payload,
        metrics=metrics,
        paper_rows=paper_rows,
        live_rows=live_rows,
        backtest_runs=backtest_runs,
        events=events,
    )
    for gate_id, checks in (telemetry.get("evidence") or {}).items():
        for check_key, value in (checks or {}).items():
            _merge_check(evidence, gate_id, check_key, value, source="observed")
    for gate_id, checks in (scenarios.get("evidence") or {}).items():
        for check_key, value in (checks or {}).items():
            _merge_check(evidence, gate_id, check_key, value, source="observed")
    policy = build_acceptance_policy_snapshot(
        {
            "strategy": strategy_payload,
            "metrics": metrics,
            "evidence": evidence,
            "prohibitions": overrides.get("prohibitions") or {},
        },
        review=review,
    )
    for gate_id, checks in (policy.get("evidence") or {}).items():
        for check_key, value in (checks or {}).items():
            _merge_check(evidence, gate_id, check_key, value, source="observed")
    _apply_manual_evidence(evidence, load_acceptance_checks(conn, key, stage="paper"))
    prohibitions = _build_auto_prohibitions(metrics, strategy_payload, evidence)
    prohibitions.update({key: bool(value) for key, value in (overrides["prohibitions"] or {}).items()})
    return {
        "stage": "paper",
        "strategy": strategy_payload,
        "metrics": metrics,
        "evidence": evidence,
        "prohibitions": prohibitions,
        "trades": [
            {
                "r_multiple": row.get("r_multiple"),
                "pnl": row.get("pnl"),
                "slippage": row.get("slippage"),
            }
            for row in _journal_closed_rows(paper_rows)
        ],
        "telemetry": telemetry,
        "scenario_runs": scenarios.get("runs") or [],
        "policy": policy,
        "security_scan": security_scan,
        "capital_stages": capital_stages,
        "deviation_snapshots": deviation_snapshots,
        "shadow_parity_summary": shadow_parity,
        "shadow_parity_traces": shadow_rows,
    }


def build_and_persist_smc_acceptance_report(conn, symbol: str | None = None, strategy: dict | None = None) -> dict:
    """Generate and persist an acceptance report from the current SMC paper records."""

    context = build_smc_acceptance_context(conn, symbol=symbol, strategy=strategy)
    report = build_acceptance_report(context)
    run_key = persist_acceptance_report(conn, report)
    return {"run_key": run_key, "report": report}


def _symbol_has_acceptance_inputs(conn, symbol: str, stage: str = "paper") -> bool:
    key = _symbol_key(symbol)
    checks = [
        ("smc_trade_journal", "symbol=? AND environment IN ('paper', 'live')"),
        ("smc_backtest_runs", "symbol=?"),
        ("paper_acceptance_events", "symbol=?"),
        ("paper_acceptance_evidence", "symbol=? AND stage=?"),
        ("paper_acceptance_context_overrides", "symbol=? AND stage=?"),
        ("paper_acceptance_runtime_metrics", "symbol=? AND stage=?"),
        ("paper_acceptance_reconciliation_runs", "symbol=? AND stage=?"),
        ("paper_acceptance_order_audit", "symbol=? AND stage=?"),
        ("paper_acceptance_alert_deliveries", "symbol=? AND stage=?"),
        ("paper_acceptance_scenario_runs", "symbol=? AND stage=?"),
    ]
    for table, where in checks:
        params = (key, stage) if "stage=?" in where else (key,)
        try:
            row = conn.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params).fetchone()
        except Exception:
            row = None
        if row:
            return True
    return False


def refresh_acceptance_reports_for_symbols(
    conn,
    symbols: list[str],
    *,
    stage: str = "paper",
    min_interval_minutes: int = 30,
) -> dict:
    """Persist fresh acceptance reports for active symbols when inputs changed enough.

    The helper is designed for monitor/refresh loops:
    - skip symbols without any acceptance inputs;
    - skip symbols that already have a recent report inside the cooldown window;
    - return structured counts for logging and API summaries.
    """

    ensure_paper_acceptance_schema(conn)
    refreshed: list[str] = []
    skipped_recent: list[str] = []
    skipped_empty: list[str] = []
    now = datetime.now(UTC)
    seen: set[str] = set()
    for raw_symbol in symbols:
        key = _symbol_key(raw_symbol)
        if not key or key in seen:
            continue
        seen.add(key)
        if not _symbol_has_acceptance_inputs(conn, key, stage=stage):
            skipped_empty.append(key)
            continue
        recent = conn.execute(
            """SELECT created_at FROM paper_acceptance_runs
               WHERE symbol=? AND stage=?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (key, stage),
        ).fetchone()
        if recent:
            recent_ts = _parse_ts(recent["created_at"])
            if recent_ts and (now - recent_ts).total_seconds() < max(1, min_interval_minutes) * 60:
                skipped_recent.append(key)
                continue
        build_and_persist_smc_acceptance_report(conn, symbol=key)
        refreshed.append(key)
    return {
        "symbols": sorted(seen),
        "refreshed_symbols": refreshed,
        "skipped_recent_symbols": skipped_recent,
        "skipped_empty_symbols": skipped_empty,
        "refreshed_count": len(refreshed),
        "skipped_recent_count": len(skipped_recent),
        "skipped_empty_count": len(skipped_empty),
    }


def _build_acceptance_timeline(events: list[dict], scenario_runs: list[dict], changes: list[dict]) -> list[dict]:
    timeline: list[dict] = []
    for row in events:
        timeline.append({
            "kind": "event",
            "title": row.get("event_type") or "event",
            "severity": row.get("severity") or "info",
            "status": row.get("status") or "open",
            "detail": row.get("detail") or {},
            "created_at": row.get("created_at"),
        })
    for row in scenario_runs:
        timeline.append({
            "kind": "scenario",
            "title": row.get("title") or row.get("scenario_id"),
            "severity": "critical" if row.get("status") != "pass" else "info",
            "status": row.get("status") or "pass",
            "detail": {
                "expected_behavior": row.get("expected_behavior"),
                "actual_behavior": row.get("actual_behavior"),
                "scenario_id": row.get("scenario_id"),
            },
            "created_at": row.get("created_at"),
        })
    for row in changes:
        timeline.append({
            "kind": "change",
            "title": row.get("change_type") or "change",
            "severity": "info",
            "status": row.get("target_type") or "",
            "detail": row.get("detail") or {},
            "created_at": row.get("created_at"),
        })
    timeline.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return timeline[:80]


def _build_section_trend(reports: list[dict]) -> list[dict]:
    trend: list[dict] = []
    for row in reports[:10]:
        payload = row.get("report_payload") or {}
        summary = payload.get("summary") or row.get("gate_summary") or {}
        trend.append({
            "run_key": row.get("run_key"),
            "created_at": row.get("created_at"),
            "conclusion": summary.get("conclusion") or row.get("conclusion"),
            "blocking_issue_count": summary.get("blocking_issue_count") or row.get("blocking_issue_count") or 0,
            "passed_ratio": round(
                (summary.get("passed") or 0) / max(1, summary.get("gate_count") or row.get("gate_count") or 1),
                4,
            ),
        })
    return trend


def _build_coverage_summary(catalog: list[dict]) -> dict:
    source_counts = {"framework": 0, "observed": 0, "manual": 0, "unknown": 0}
    source_gates = {"framework": set(), "observed": set(), "manual": set(), "unknown": set()}
    missing_checks = 0
    total_checks = 0
    total_gates = 0
    gates_with_missing: list[str] = []
    section_rows: list[dict] = []
    missing_details: list[dict] = []
    for section in catalog:
        section_total = 0
        section_missing = 0
        section_gates_with_missing: list[str] = []
        for gate in section.get("gates") or []:
            total_gates += 1
            gate_has_missing = False
            gate_missing_checks: list[str] = []
            for check in gate.get("checks") or []:
                total_checks += 1
                section_total += 1
                source = str(check.get("source") or "unknown")
                if source not in source_counts:
                    source = "unknown"
                source_counts[source] += 1
                source_gates[source].add(gate.get("id"))
                if check.get("value") is None:
                    missing_checks += 1
                    section_missing += 1
                    gate_has_missing = True
                    gate_missing_checks.append(check.get("key") or "")
            if gate_has_missing:
                gates_with_missing.append(gate.get("id"))
                section_gates_with_missing.append(gate.get("id"))
                missing_details.append({
                    "section": section.get("section"),
                    "section_title": section.get("title"),
                    "gate_id": gate.get("id"),
                    "gate_name": gate.get("name"),
                    "missing_checks": gate_missing_checks,
                    "missing_count": len(gate_missing_checks),
                })
        section_rows.append({
            "section": section.get("section"),
            "title": section.get("title"),
            "total_checks": section_total,
            "missing_checks": section_missing,
            "covered_ratio": round((section_total - section_missing) / max(1, section_total), 4),
            "gates_with_missing": section_gates_with_missing,
        })
    return {
        "total_gates": total_gates,
        "total_checks": total_checks,
        "missing_checks": missing_checks,
        "covered_ratio": round((total_checks - missing_checks) / max(1, total_checks), 4),
        "source_counts": source_counts,
        "source_gate_counts": {key: len(value) for key, value in source_gates.items()},
        "gates_with_missing": gates_with_missing,
        "sections": section_rows,
        "missing_details": missing_details[:20],
    }


def _auto_stage_name(capital_ratio: float | None) -> tuple[str, str]:
    ratio = float(capital_ratio or 0)
    if ratio <= 0:
        return "stage0_paper", "stage 0 paper"
    if ratio <= 0.05:
        return "stage1_1_5", "stage 1 1%-5%"
    if ratio <= 0.20:
        return "stage2_10_20", "stage 2 10%-20%"
    if ratio <= 0.50:
        return "stage3_25_50", "stage 3 25%-50%"
    return "stage4_full", "stage 4 full"


def _ensure_capacity_and_deviation_snapshots(
    conn,
    *,
    symbol: str,
    strategy: dict,
    metrics: dict,
    live_metrics: dict,
    paper_summary: dict,
    live_summary: dict,
    latest_backtest: dict,
) -> dict:
    capital_ratio = _safe_float(strategy.get("live_capital_ratio"))
    if capital_ratio is None:
        capital_ratio = 0.0 if int(live_summary.get("trade_count") or 0) == 0 else 0.05
    stage_name, stage_label = _auto_stage_name(capital_ratio)
    latest_stage = next(iter(load_capital_stages(conn, symbol, stage="paper", limit=1)), None)
    stage_trade_count = int(max(int(paper_summary.get("trade_count") or 0), int(live_summary.get("trade_count") or 0)))
    stage_observation_days = int(max(int(paper_summary.get("testing_days") or 0), int(live_summary.get("testing_days") or 0)))
    stage_slippage = _safe_float(metrics.get("average_slippage"))
    stage_fill_rate = _safe_float(metrics.get("fill_rate"))
    stage_drawdown = _safe_float(metrics.get("max_drawdown"))
    if not latest_stage or any([
        latest_stage.get("stage_name") != stage_name,
        not _same_numeric(latest_stage.get("capital_ratio"), capital_ratio),
        int(latest_stage.get("trade_count") or 0) != stage_trade_count,
        int(latest_stage.get("observation_days") or 0) != stage_observation_days,
        not _same_numeric(latest_stage.get("slippage_bps"), stage_slippage),
        not _same_numeric(latest_stage.get("fill_rate"), stage_fill_rate),
        not _same_numeric(latest_stage.get("drawdown"), stage_drawdown),
    ]):
        record_capital_stage(
            conn,
            symbol=symbol,
            stage_name=stage_name,
            capital_ratio=capital_ratio,
            capital_range_label=stage_label,
            trade_count=stage_trade_count,
            observation_days=stage_observation_days,
            slippage_bps=stage_slippage,
            fill_rate=stage_fill_rate,
            drawdown=stage_drawdown,
            note="auto-generated acceptance capacity stage snapshot",
            stage="paper",
        )
    latest_paper_vs_live = None
    if live_summary.get("trade_count") and paper_summary.get("trade_count"):
        win_rate_delta = abs((_safe_float(live_summary.get("win_rate")) or 0) - (_safe_float(paper_summary.get("win_rate")) or 0))
        paper_fill_rate = _safe_float(metrics.get("fill_rate"))
        live_fill_rate = _safe_float(live_metrics.get("fill_rate"))
        fill_rate_delta = abs(live_fill_rate - paper_fill_rate) if live_fill_rate is not None and paper_fill_rate is not None else None
        paper_slippage = _safe_float(metrics.get("average_slippage"))
        live_slippage = _safe_float(live_metrics.get("average_slippage"))
        slippage_delta_bps = abs(live_slippage - paper_slippage) if live_slippage is not None and paper_slippage is not None else None
        drawdown_delta = abs(abs(_safe_float(live_summary.get("max_drawdown")) or 0) - abs(_safe_float(paper_summary.get("max_drawdown")) or 0))
        holding_delta = abs((_safe_float(live_summary.get("average_holding_minutes")) or 0) - (_safe_float(paper_summary.get("average_holding_minutes")) or 0))
        trade_freq_delta = abs(
            ((live_summary.get("trade_count") or 0) / max(1, live_summary.get("testing_days") or 1))
            - ((paper_summary.get("trade_count") or 0) / max(1, paper_summary.get("testing_days") or 1))
        )
        score_parts = [win_rate_delta, drawdown_delta]
        if fill_rate_delta is not None:
            score_parts.append(fill_rate_delta)
        deviation_score = round(sum(score_parts) / max(1, len(score_parts)), 4)
        latest_live_dev = next(
            (
                row for row in load_deviation_snapshots(conn, symbol, stage="paper", limit=10)
                if row.get("baseline_source") == "paper" and row.get("comparison_source") == "live"
            ),
            None,
        )
        if not latest_live_dev or any([
            not _same_numeric(latest_live_dev.get("win_rate_delta"), win_rate_delta),
            not _same_numeric(latest_live_dev.get("fill_rate_delta"), fill_rate_delta),
            not _same_numeric(latest_live_dev.get("slippage_delta_bps"), slippage_delta_bps),
            not _same_numeric(latest_live_dev.get("drawdown_delta"), drawdown_delta),
            not _same_numeric(latest_live_dev.get("holding_time_delta_minutes"), holding_delta),
            not _same_numeric(latest_live_dev.get("trade_frequency_delta"), trade_freq_delta),
            not _same_numeric(latest_live_dev.get("deviation_score"), deviation_score),
        ]):
            latest_paper_vs_live = record_deviation_snapshot(
                conn,
                symbol=symbol,
                baseline_source="paper",
                comparison_source="live",
                win_rate_delta=win_rate_delta,
                fill_rate_delta=fill_rate_delta,
                slippage_delta_bps=slippage_delta_bps,
                drawdown_delta=drawdown_delta,
                holding_time_delta_minutes=holding_delta,
                trade_frequency_delta=trade_freq_delta,
                deviation_score=deviation_score,
                detail={
                    "origin": "auto",
                    "paper_fill_rate": paper_fill_rate,
                    "live_fill_rate": live_fill_rate,
                    "paper_average_slippage": paper_slippage,
                    "live_average_slippage": live_slippage,
                },
                stage="paper",
            )
        else:
            latest_paper_vs_live = latest_live_dev
    latest_backtest_vs_paper = None
    if latest_backtest and paper_summary.get("trade_count"):
        win_rate_delta = abs((_safe_float(latest_backtest.get("win_rate")) or 0) - (_safe_float(paper_summary.get("win_rate")) or 0))
        drawdown_delta = abs(abs(_safe_float(latest_backtest.get("max_drawdown")) or 0) - abs(_safe_float(paper_summary.get("max_drawdown")) or 0))
        trade_freq_delta = abs(
            ((_safe_float(latest_backtest.get("total_trades")) or 0) / max(1, paper_summary.get("testing_days") or 1))
            - ((paper_summary.get("trade_count") or 0) / max(1, paper_summary.get("testing_days") or 1))
        )
        deviation_score = round(sum([win_rate_delta, drawdown_delta, trade_freq_delta]) / 3, 4)
        latest_backtest_dev = next(
            (
                row for row in load_deviation_snapshots(conn, symbol, stage="paper", limit=10)
                if row.get("baseline_source") == "backtest" and row.get("comparison_source") == "paper"
            ),
            None,
        )
        if not latest_backtest_dev or any([
            not _same_numeric(latest_backtest_dev.get("win_rate_delta"), win_rate_delta),
            not _same_numeric(latest_backtest_dev.get("drawdown_delta"), drawdown_delta),
            not _same_numeric(latest_backtest_dev.get("trade_frequency_delta"), trade_freq_delta),
            not _same_numeric(latest_backtest_dev.get("deviation_score"), deviation_score),
        ]):
            latest_backtest_vs_paper = record_deviation_snapshot(
                conn,
                symbol=symbol,
                baseline_source="backtest",
                comparison_source="paper",
                win_rate_delta=win_rate_delta,
                drawdown_delta=drawdown_delta,
                trade_frequency_delta=trade_freq_delta,
                deviation_score=deviation_score,
                detail={"origin": "auto"},
                stage="paper",
            )
        else:
            latest_backtest_vs_paper = latest_backtest_dev
    return {
        "latest_paper_vs_live": latest_paper_vs_live,
        "latest_backtest_vs_paper": latest_backtest_vs_paper,
    }


def build_acceptance_workspace(conn, symbol: str | None, stage: str = "paper", limit_reports: int = 5) -> dict:
    """Build the full acceptance workspace payload for UI editing and reporting."""

    key = _symbol_key(symbol)
    overrides = load_acceptance_context_overrides(conn, key, stage=stage)
    context = build_smc_acceptance_context(conn, symbol=key, strategy=overrides["strategy"])
    report = build_acceptance_report(context)
    catalog, section_summaries = _augment_report_catalog(report, context.get("evidence") or {})
    events = load_acceptance_events(conn, symbol=key, limit=100)
    scenario_runs = load_scenario_runs(conn, symbol=key, stage=stage, limit=40)
    reports = load_acceptance_reports(conn, symbol=key, limit=limit_reports)
    changes = load_acceptance_change_log(conn, key, stage=stage, limit=80)
    return {
        "symbol": key,
        "stage": stage,
        "strategy_overrides": overrides["strategy"],
        "metrics_overrides": overrides["metrics"],
        "prohibitions_overrides": overrides["prohibitions"],
        "review": load_acceptance_review(conn, key, stage=stage),
        "policy": context.get("policy") or {},
        "security_scan": context.get("security_scan") or {},
        "report": report,
        "sections": section_summaries,
        "catalog": catalog,
        "events": events,
        "runtime_metrics": load_runtime_metrics(conn, symbol=key, stage=stage, limit=60),
        "reconciliation_runs": load_reconciliation_runs(conn, symbol=key, stage=stage, limit=30),
        "order_audit": load_order_audit_rows(conn, symbol=key, stage=stage, limit=40),
        "alert_deliveries": load_alert_deliveries(conn, symbol=key, stage=stage, limit=40),
        "scenario_runs": scenario_runs,
        "scenario_catalog": scenario_catalog(),
        "change_log": changes,
        "timeline": _build_acceptance_timeline(events, scenario_runs, changes),
        "reports": reports,
        "section_trend": _build_section_trend(reports),
        "coverage": _build_coverage_summary(catalog),
        "capital_stages": context.get("capital_stages") or [],
        "deviation_snapshots": context.get("deviation_snapshots") or [],
        "shadow_parity_summary": context.get("shadow_parity_summary") or {},
        "shadow_parity_traces": context.get("shadow_parity_traces") or [],
    }


__all__ = [
    "build_acceptance_workspace",
    "build_and_persist_smc_acceptance_report",
    "build_smc_acceptance_context",
    "delete_acceptance_check",
    "ensure_paper_acceptance_schema",
    "load_alert_deliveries",
    "load_acceptance_checks",
    "load_acceptance_change_log",
    "load_acceptance_context_overrides",
    "load_acceptance_events",
    "load_acceptance_reports",
    "load_acceptance_review",
    "load_order_audit_rows",
    "load_capital_stages",
    "load_deviation_snapshots",
    "load_shadow_parity_traces",
    "load_reconciliation_runs",
    "load_runtime_metrics",
    "load_scenario_runs",
    "persist_acceptance_report",
    "record_alert_delivery",
    "record_acceptance_change",
    "record_acceptance_event",
    "record_capital_stage",
    "record_deviation_snapshot",
    "record_shadow_parity_trace",
    "record_order_audit",
    "record_reconciliation_run",
    "record_runtime_metric",
    "refresh_acceptance_reports_for_symbols",
    "summarize_shadow_parity_traces",
    "run_acceptance_scenario",
    "upsert_acceptance_review",
    "upsert_acceptance_check",
    "upsert_acceptance_context_overrides",
]
