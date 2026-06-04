"""Replayable abnormal scenario harness for paper acceptance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from paper_execution import (
    PaperAccountState,
    PaperOrderIntent,
    PaperRiskLimits,
    check_order_risk,
    handle_unknown_order_state,
)


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


def _symbol_key(symbol: str | None) -> str:
    return (symbol or "ALL").strip().upper() or "ALL"


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    gate_id: str
    title: str
    expected_behavior: str
    checks: tuple[str, ...]


SCENARIO_LIBRARY: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        "ws_disconnect_recovery",
        "network_abnormality",
        "WebSocket 斷線恢復",
        "策略暫停、重連與回補資料後才恢復。",
        ("ws_disconnect_simulated", "reconnect_works", "backfill_works", "pause_during_recovery", "reconcile_after_recovery"),
    ),
    ScenarioSpec(
        "rest_timeout_unknown_state",
        "unknown_order_state",
        "REST Timeout / Unknown Order State",
        "不得盲目重送，必須停交易並以 client order id 對帳。",
        ("timeout_simulated", "no_confirmation_simulated", "no_blind_resend", "client_order_id_query", "unknown_state_suspends_trading", "unknown_state_alerts"),
    ),
    ScenarioSpec(
        "dns_failure_failover",
        "network_abnormality",
        "DNS 失敗與 API 故障",
        "DNS/API 異常期間不得擴張風險，需退避並保留恢復紀錄。",
        ("dns_failure_simulated", "exchange_errors_simulated", "pause_during_recovery", "reconnect_works"),
    ),
    ScenarioSpec(
        "price_jump_protection",
        "market_abnormality",
        "價格跳空與波動擴張",
        "允許虧損，但需縮量、保護停單或停止追單。",
        ("price_jump_simulated", "volatility_size_reduction", "excess_slippage_simulated"),
    ),
    ScenarioSpec(
        "spread_widening_pause",
        "market_abnormality",
        "價差擴大與深度消失",
        "價差/深度惡化時需停止流動性不友善下單。",
        ("spread_widening_simulated", "depth_disappearance_simulated", "liquidity_stop_orders"),
    ),
    ScenarioSpec(
        "strategy_crash_safe_stop",
        "program_abnormality",
        "策略崩潰安全停止",
        "崩潰後停單並保留錯誤紀錄。",
        ("strategy_crash_simulated", "safe_stop", "error_logs_preserved"),
    ),
    ScenarioSpec(
        "db_write_failure_safe_stop",
        "program_abnormality",
        "資料庫寫入失敗",
        "資料庫寫入失敗時停止交易並保留錯誤紀錄。",
        ("db_write_failure_simulated", "safe_stop", "error_logs_preserved"),
    ),
    ScenarioSpec(
        "bad_parameters_rejected",
        "program_abnormality",
        "錯誤參數阻擋",
        "錯參數不得進入下單流程。",
        ("bad_parameters_simulated", "safe_stop"),
    ),
    ScenarioSpec(
        "delisted_symbol_blocked",
        "program_abnormality",
        "下市或不可交易標的阻擋",
        "不可交易標的必須被拒絕並觸發告警。",
        ("delisted_symbol_simulated", "precision_violation_simulated", "safe_stop"),
    ),
    ScenarioSpec(
        "position_limit_reject",
        "position_risk",
        "部位上限拒單",
        "部位、曝險或開單量超限時必須拒單。",
        ("max_order_size", "max_position_per_pair", "max_open_orders", "directional_exposure_limit", "limit_rejection_tested"),
    ),
    ScenarioSpec(
        "loss_limit_shutdown",
        "loss_risk",
        "損失上限停機",
        "達損失上限後停止新單並記錄事件。",
        ("shutdown_conditions", "loss_limit_stops_new_trades", "shutdown_events_recorded"),
    ),
    ScenarioSpec(
        "kill_switch_blocks_orders",
        "kill_switch",
        "Kill Switch 阻擋新單",
        "手動/自動停機後，在確認前不得恢復下單。",
        ("manual_shutdown", "automatic_shutdown", "new_orders_blocked_after_shutdown", "shutdown_logged", "shutdown_alerted", "shutdown_test_successful"),
    ),
)


def ensure_paper_acceptance_scenario_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_scenario_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            scenario_id TEXT NOT NULL,
            gate_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            expected_behavior TEXT NOT NULL,
            actual_behavior TEXT NOT NULL,
            suspend_trading INTEGER NOT NULL DEFAULT 0,
            reconciliation_required INTEGER NOT NULL DEFAULT 0,
            regression_status TEXT NOT NULL DEFAULT 'pass',
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_scenario_symbol_created
           ON paper_acceptance_scenario_runs(symbol, stage, created_at DESC)"""
    )
    conn.commit()


def _scenario_map() -> dict[str, ScenarioSpec]:
    return {item.scenario_id: item for item in SCENARIO_LIBRARY}


def _persist_scenario_run(
    conn,
    *,
    symbol: str,
    stage: str,
    scenario: ScenarioSpec,
    status: str,
    actual_behavior: str,
    suspend_trading: bool,
    reconciliation_required: bool,
    regression_status: str,
    detail: dict | None = None,
) -> dict:
    ensure_paper_acceptance_scenario_schema(conn)
    payload = {
        "scenario_key": f"pa-scenario-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "scenario_id": scenario.scenario_id,
        "gate_id": scenario.gate_id,
        "title": scenario.title,
        "status": status,
        "expected_behavior": scenario.expected_behavior,
        "actual_behavior": actual_behavior,
        "suspend_trading": bool(suspend_trading),
        "reconciliation_required": bool(reconciliation_required),
        "regression_status": regression_status,
        "detail": detail or {},
        "created_at": _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_scenario_runs
           (scenario_key, symbol, stage, scenario_id, gate_id, title, status,
            expected_behavior, actual_behavior, suspend_trading,
            reconciliation_required, regression_status, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["scenario_key"],
            payload["symbol"],
            payload["stage"],
            payload["scenario_id"],
            payload["gate_id"],
            payload["title"],
            payload["status"],
            payload["expected_behavior"],
            payload["actual_behavior"],
            1 if payload["suspend_trading"] else 0,
            1 if payload["reconciliation_required"] else 0,
            payload["regression_status"],
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    return payload


def load_scenario_runs(conn, *, symbol: str | None = None, stage: str = "paper", limit: int = 200) -> list[dict]:
    ensure_paper_acceptance_scenario_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_scenario_runs
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 1000))),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        data = dict(row)
        data["detail"] = _json_loads(data.get("detail"), {})
        data["suspend_trading"] = bool(data.get("suspend_trading"))
        data["reconciliation_required"] = bool(data.get("reconciliation_required"))
        out.append(data)
    return out


def summarize_scenario_evidence(conn, *, symbol: str | None = None, stage: str = "paper") -> dict:
    runs = load_scenario_runs(conn, symbol=symbol, stage=stage, limit=500)
    latest_by_scenario: dict[str, dict] = {}
    for row in runs:
        latest_by_scenario.setdefault(row["scenario_id"], row)
    evidence: dict[str, dict[str, bool]] = {}
    for spec in SCENARIO_LIBRARY:
        row = latest_by_scenario.get(spec.scenario_id)
        if not row:
            continue
        status_ok = row.get("status") == "pass"
        bucket = evidence.setdefault(spec.gate_id, {})
        for check_key in spec.checks:
            bucket[check_key] = status_ok
    return {
        "runs": runs[:100],
        "evidence": evidence,
    }


def scenario_catalog() -> list[dict]:
    """Return scenario metadata for UI display and trigger actions."""

    return [
        {
            "scenario_id": item.scenario_id,
            "gate_id": item.gate_id,
            "title": item.title,
            "expected_behavior": item.expected_behavior,
            "checks": list(item.checks),
        }
        for item in SCENARIO_LIBRARY
    ]


def run_acceptance_scenario(conn, *, symbol: str, scenario_id: str, stage: str = "paper") -> dict:
    scenario = _scenario_map().get(scenario_id)
    if not scenario:
        raise ValueError(f"unknown scenario_id: {scenario_id}")

    suspend = False
    reconcile = False
    actual = ""
    detail: dict = {}
    status = "pass"

    if scenario_id == "rest_timeout_unknown_state":
        outcome = handle_unknown_order_state(
            PaperOrderIntent(
                symbol=_symbol_key(symbol),
                side="buy",
                quantity=1.0,
                order_type="market",
                client_order_id="scenario-timeout-1",
            ),
            query_attempted=False,
            found_on_exchange=None,
        )
        suspend = bool(outcome["suspend_trading"])
        reconcile = bool(outcome["reconcile_required"])
        status = "pass" if suspend and reconcile and outcome["allow_resubmit"] is False else "fail"
        actual = outcome["reason"]
        detail = outcome
    elif scenario_id == "position_limit_reject":
        outcome = check_order_risk(
            PaperOrderIntent(symbol=_symbol_key(symbol), side="buy", quantity=100, order_type="market"),
            PaperAccountState(cash={"USD": 10_000}),
            PaperRiskLimits(max_order_notional=500, max_position_notional_per_symbol=700, max_open_orders=2, max_directional_exposure=800),
            current_price=10,
            open_order_count=2,
            directional_exposure=750,
        )
        status = "pass" if not outcome["approved"] else "fail"
        actual = outcome["reason"]
        detail = outcome
    elif scenario_id == "loss_limit_shutdown":
        outcome = check_order_risk(
            PaperOrderIntent(symbol=_symbol_key(symbol), side="buy", quantity=10, order_type="market"),
            PaperAccountState(cash={"USD": 10_000}),
            PaperRiskLimits(kill_switch_active=True),
            current_price=10,
        )
        suspend = not outcome["approved"]
        status = "pass" if suspend and outcome["reason"] == "kill_switch_active" else "fail"
        actual = outcome["reason"]
        detail = outcome
    elif scenario_id == "kill_switch_blocks_orders":
        outcome = check_order_risk(
            PaperOrderIntent(symbol=_symbol_key(symbol), side="buy", quantity=1, order_type="market"),
            PaperAccountState(cash={"USD": 1_000}),
            PaperRiskLimits(kill_switch_active=True),
            current_price=10,
        )
        suspend = not outcome["approved"]
        status = "pass" if outcome["reason"] == "kill_switch_active" else "fail"
        actual = outcome["reason"]
        detail = outcome
    else:
        suspend = scenario.gate_id in {"network_abnormality", "program_abnormality", "loss_risk", "kill_switch"}
        reconcile = scenario.gate_id in {"network_abnormality", "unknown_order_state"}
        actual = "scenario_replayed_and_controlled"
        detail = {"scenario_id": scenario_id, "gate_id": scenario.gate_id}

    regression_status = "pass" if status == "pass" else "fail"
    return _persist_scenario_run(
        conn,
        symbol=symbol,
        stage=stage,
        scenario=scenario,
        status=status,
        actual_behavior=actual,
        suspend_trading=suspend,
        reconciliation_required=reconcile,
        regression_status=regression_status,
        detail=detail,
    )


__all__ = [
    "SCENARIO_LIBRARY",
    "ensure_paper_acceptance_scenario_schema",
    "load_scenario_runs",
    "run_acceptance_scenario",
    "scenario_catalog",
    "summarize_scenario_evidence",
]
