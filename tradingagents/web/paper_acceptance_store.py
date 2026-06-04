"""SQLite helpers for paper-trading acceptance reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from paper_acceptance import build_acceptance_report, render_acceptance_markdown


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


def ensure_paper_acceptance_schema(conn) -> None:
    """Create acceptance report storage tables if needed."""

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
            (strategy.get("symbol") or "").upper(),
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
    return run_key


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
        params.append(symbol.upper())
    rows = conn.execute(
        f"""SELECT * FROM paper_acceptance_runs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?""",
        params + [max(1, min(int(limit), 500))],
    ).fetchall()
    return [_run_row_to_dict(row) for row in rows]


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
            symbol.upper() if symbol else None,
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
        params.append(symbol.upper())
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


def build_smc_acceptance_context(conn, symbol: str | None = None, strategy: dict | None = None) -> dict:
    """Build an acceptance context from existing SMC paper journal and backtest rows."""

    strategy = dict(strategy or {})
    if symbol:
        strategy["symbol"] = symbol.upper()
    params: list = []
    journal_where = "WHERE environment = 'paper'"
    if symbol:
        journal_where += " AND symbol = ?"
        params.append(symbol.upper())
    journal_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT * FROM smc_trade_journal
                {journal_where}
                ORDER BY COALESCE(entry_time, created_at) DESC, id DESC""",
            params,
        ).fetchall()
    ]
    closed = [row for row in journal_rows if row.get("status") == "closed"]
    wins = [row for row in closed if float(row.get("pnl") or 0) > 0 or float(row.get("r_multiple") or 0) > 0]
    r_values = [float(row.get("r_multiple")) for row in closed if row.get("r_multiple") is not None]
    pnl_values = [float(row.get("pnl")) for row in closed if row.get("pnl") is not None]
    symbols = sorted({row.get("symbol") for row in journal_rows if row.get("symbol")})
    backtest_params: list = []
    backtest_where = ""
    if symbol:
        backtest_where = "WHERE symbol = ?"
        backtest_params.append(symbol.upper())
    backtest_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT * FROM smc_backtest_runs
                {backtest_where}
                ORDER BY created_at DESC, id DESC
                LIMIT 20""",
            backtest_params,
        ).fetchall()
    ]
    trade_count = len(closed)
    expectancy = sum(r_values) / len(r_values) if r_values else None
    net_profit = sum(pnl_values) if pnl_values else None
    metrics = {
        "trade_count": trade_count,
        "paper_journal_count": len(journal_rows),
        "backtest_run_count": len(backtest_rows),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "expectancy_after_costs": round(expectancy, 4) if expectancy is not None else None,
        "net_profit": round(net_profit, 4) if net_profit is not None else None,
        "fees_included": False,
        "total_fees": None,
        "slippage_included": False,
        "total_slippage": None,
        "reconciliation_implemented": False,
        "unresolved_reconciliation_count": 0,
        "kill_switch_tested": False,
        "parameters_frozen": False,
        "parameter_change_count": None,
        "hardcoded_api_keys": False,
        "withdrawal_permission_enabled": False,
    }
    base_checks = {
        "strategy_logic": {
            "entry_conditions": bool(backtest_rows),
            "exit_conditions": bool(backtest_rows),
            "stop_loss_conditions": bool(backtest_rows),
            "take_profit_conditions": bool(backtest_rows),
            "no_future_data": bool(backtest_rows),
        },
        "performance_metrics": {
            "trade_count": trade_count > 0,
            "net_profit": net_profit is not None,
            "win_rate": metrics["win_rate"] is not None,
        },
        "final_report": {
            "basic_information": True,
            "performance_summary": True,
            "trade_quality": False,
            "final_conclusion": True,
        },
    }
    prohibitions = {
        "fees_missing": True,
        "slippage_missing": True,
        "reconciliation_missing": True,
        "kill_switch_untested": True,
        "sample_size_too_small": trade_count < 50,
    }
    strategy.setdefault("name", "SMC Paper Acceptance")
    strategy.setdefault("strategy_type", "intraday")
    strategy.setdefault("stage", "paper")
    strategy.setdefault("symbol", symbol.upper() if symbol else (symbols[0] if len(symbols) == 1 else "ALL"))
    return {
        "stage": "paper",
        "strategy": strategy,
        "metrics": metrics,
        "evidence": {key: {"checks": value} for key, value in base_checks.items()},
        "prohibitions": prohibitions,
        "trades": [
            {
                "r_multiple": row.get("r_multiple"),
                "pnl": row.get("pnl"),
                "slippage": None,
            }
            for row in closed
        ],
    }


def build_and_persist_smc_acceptance_report(conn, symbol: str | None = None, strategy: dict | None = None) -> dict:
    """Generate and persist an acceptance report from the current SMC paper records."""

    context = build_smc_acceptance_context(conn, symbol=symbol, strategy=strategy)
    report = build_acceptance_report(context)
    run_key = persist_acceptance_report(conn, report)
    return {"run_key": run_key, "report": report}


__all__ = [
    "build_and_persist_smc_acceptance_report",
    "build_smc_acceptance_context",
    "ensure_paper_acceptance_schema",
    "load_acceptance_events",
    "load_acceptance_reports",
    "persist_acceptance_report",
    "record_acceptance_event",
]
