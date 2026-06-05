"""Probe-order sizing and kill-switch helpers."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Optional

from learning.adaptive_store import ensure_adaptive_calibration_schema


def compute_probe_notional(
    equity_usdt: float,
    base_risk_pct: float,
    risk_multiplier: float,
    stop_distance_pct: float,
    probe_notional_cap_usdt: float = 5.0,
    min_notional_usdt: float = 1.0,
) -> float:
    if risk_multiplier <= 0:
        return 0.0
    stop_distance_pct = max(float(stop_distance_pct), 1e-4)
    risk_budget = float(equity_usdt) * float(base_risk_pct) * float(risk_multiplier)
    risk_based_notional = risk_budget / stop_distance_pct
    notional = min(risk_based_notional, float(probe_notional_cap_usdt))
    if notional < float(min_notional_usdt):
        return 0.0
    return float(notional)


def _trade_day(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return text[:10]


def probe_daily_stats(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    trade_date: Optional[str] = None,
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    current_day = trade_date or datetime.now(UTC).date().isoformat()
    rows = conn.execute(
        """SELECT entry_time, pnl_usdt
             FROM smc_adaptive_trade_ledger
            WHERE symbol=? AND probe=1
         ORDER BY entry_time DESC, id DESC""",
        (symbol.upper(),),
    ).fetchall()
    probe_rows = [dict(row) if not isinstance(row, dict) else row for row in rows]
    today_rows = [row for row in probe_rows if _trade_day(row.get("entry_time")) == current_day]
    consecutive_losses = 0
    for row in probe_rows:
        pnl = float(row.get("pnl_usdt") or 0.0)
        if pnl < 0:
            consecutive_losses += 1
            continue
        break
    return {
        "trade_date": current_day,
        "orders_today": len(today_rows),
        "daily_loss_usdt": abs(sum(float(row.get("pnl_usdt") or 0.0) for row in today_rows if float(row.get("pnl_usdt") or 0.0) < 0)),
        "consecutive_losses": consecutive_losses,
    }


def plan_probe_order(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    risk_multiplier: float,
    account_equity: float,
    base_risk_pct: float,
    stop_distance_pct: float,
    probe_notional_cap_usdt: float = 5.0,
    min_probe_notional_usdt: float = 1.0,
    max_probe_orders_per_day: int = 5,
    max_probe_daily_loss_usdt: float = 10.0,
    max_consecutive_probe_losses: int = 3,
    cooldown_minutes_after_probe_loss_streak: int = 240,
    trade_time: Optional[str] = None,
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    now = datetime.now(UTC)
    current_trade_time = trade_time or now.isoformat().replace("+00:00", "Z")
    stats = probe_daily_stats(
        conn,
        symbol=symbol,
        trade_date=_trade_day(current_trade_time),
    )
    if stats["orders_today"] >= max_probe_orders_per_day:
        return {
            "allow_order": False,
            "notional_usdt": 0.0,
            "order_mode": "DRY_RUN",
            "reason": "probe_daily_order_cap_reached",
            "state_hint": "DRY_RUN",
            "stats": stats,
        }
    if stats["daily_loss_usdt"] >= max_probe_daily_loss_usdt:
        return {
            "allow_order": False,
            "notional_usdt": 0.0,
            "order_mode": "DRY_RUN",
            "reason": "probe_daily_loss_cap_reached",
            "state_hint": "DRY_RUN",
            "stats": stats,
        }
    if stats["consecutive_losses"] >= max_consecutive_probe_losses:
        last_loss_row = conn.execute(
            """SELECT entry_time
                 FROM smc_adaptive_trade_ledger
                WHERE symbol=? AND probe=1 AND pnl_usdt < 0
             ORDER BY entry_time DESC, id DESC
                LIMIT 1""",
            (symbol.upper(),),
        ).fetchone()
        last_loss_at = None
        if last_loss_row is not None:
            last_loss_at = dict(last_loss_row)["entry_time"]
        cooldown_until = None
        if last_loss_at:
            text = last_loss_at
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                cooldown_until = datetime.fromisoformat(text) + timedelta(minutes=cooldown_minutes_after_probe_loss_streak)
            except ValueError:
                cooldown_until = None
        if cooldown_until and now < cooldown_until:
            return {
                "allow_order": False,
                "notional_usdt": 0.0,
                "order_mode": "DRY_RUN",
                "reason": "probe_loss_streak_cooldown",
                "state_hint": "LOCKED",
                "stats": {**stats, "cooldown_until": cooldown_until.isoformat()},
            }

    notional = compute_probe_notional(
        equity_usdt=account_equity,
        base_risk_pct=base_risk_pct,
        risk_multiplier=risk_multiplier,
        stop_distance_pct=stop_distance_pct,
        probe_notional_cap_usdt=probe_notional_cap_usdt,
        min_notional_usdt=min_probe_notional_usdt,
    )
    if notional <= 0:
        return {
            "allow_order": False,
            "notional_usdt": 0.0,
            "order_mode": "DRY_RUN",
            "reason": "probe_notional_below_minimum",
            "state_hint": "DRY_RUN",
            "stats": stats,
        }
    return {
        "allow_order": True,
        "notional_usdt": notional,
        "order_mode": "PROBE",
        "reason": "VALIDATING_PROBE sizing",
        "state_hint": "VALIDATING_PROBE",
        "stats": stats,
    }
