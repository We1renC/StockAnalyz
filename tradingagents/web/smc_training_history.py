"""Per-tick training history + adaptive throttling.

Records every ``/api/smc-crypto/auto-learn-tick`` call so users can:

  • inspect each tick's marginal effect (Δsample, ΔE[R], indicator
    change, weights changed, action taken)
  • watch performance over time (expected_R / win_rate / sharpe trend)
  • understand throttling decisions (why the loop slowed down or sped up)

Throttling policy (no max-tick-count, but anti-saturation):

  • Active (any of: Δsample > 0, weights drifted, indicator improved)
        → next interval = base (30s)
  • Plateau (3 consecutive ticks with Δsample=0 AND no drift)
        → next interval = base × 4 (= 2 min)
  • Saturated (6 consecutive ticks plateau AND indicator
    in {stagnant, insufficient_data})
        → next interval = base × 16 (= 8 min)
  • Anything OK → stays at active rate

The UI polls ``/api/smc-crypto/training-throttle`` to get the
recommended next-tick interval.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_STARTING_EQUITY_USDT = 100_000.0


def ensure_training_history_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smc_training_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            state TEXT NOT NULL,
            ledger_size INTEGER NOT NULL DEFAULT 0,
            ledger_delta INTEGER NOT NULL DEFAULT 0,
            validation_passed INTEGER NOT NULL DEFAULT 0,
            validation_total INTEGER NOT NULL DEFAULT 5,
            learning_indicator TEXT,
            expected_R REAL,
            expected_R_delta REAL,
            win_rate REAL,
            sharpe REAL,
            weights_changed_count INTEGER NOT NULL DEFAULT 0,
            weights_changed TEXT NOT NULL DEFAULT '[]',
            order_placed INTEGER NOT NULL DEFAULT 0,
            order_id TEXT,
            trades_added INTEGER NOT NULL DEFAULT 0,
            tick_elapsed_seconds REAL,
            next_interval_seconds INTEGER NOT NULL DEFAULT 30,
            -- Real simulated-market P&L (USDT)
            equity_usdt REAL,
            equity_delta_usdt REAL,
            realized_pnl_usdt REAL,
            unrealized_pnl_usdt REAL,
            total_fills INTEGER NOT NULL DEFAULT 0,
            winning_fills INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    # Add columns to existing table (idempotent migration)
    for col_def in [
        "equity_usdt REAL",
        "equity_delta_usdt REAL",
        "realized_pnl_usdt REAL",
        "unrealized_pnl_usdt REAL",
        "total_fills INTEGER NOT NULL DEFAULT 0",
        "winning_fills INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(f"ALTER TABLE smc_training_history ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_smc_training_history_symbol_time
        ON smc_training_history(symbol, tick_time DESC)
    """)
    conn.commit()


def compute_pnl_snapshot(api) -> dict:
    """Query the crypto-api for current equity + realized P&L.

    equity_usdt = USDT cash + sum(other_asset_qty × current_market_price)
    realized_pnl = sum of (sell_proceeds - buy_cost) inferred from fill history
    unrealized_pnl = current value of open positions - cost basis
    """
    try:
        # Pull balances
        b = api.balances()
        bals = (b.get("payload") or {}).get("balances") or b.get("payload") or []
        if isinstance(bals, dict):
            bals = bals.get("data") or list(bals.values())
        positions: dict[str, float] = {}
        for row in (bals or []):
            if not isinstance(row, dict):
                continue
            try:
                asset = row.get("asset")
                qty = float(row.get("total") or row.get("available") or 0)
                if asset:
                    positions[asset] = qty
            except (TypeError, ValueError):
                pass

        # Compute equity in USDT (mark-to-market every non-USDT asset via ticker)
        usdt = positions.pop("USDT", 0.0)
        equity = float(usdt)
        for asset, qty in positions.items():
            if qty <= 0:
                continue
            symbol = f"{asset}-USDT"
            try:
                t = api.ticker(symbol)
                p = (t.get("payload") or {}).get("price") \
                    or (t.get("payload") or {}).get("last_price")
                if p:
                    equity += qty * float(p)
            except Exception:
                pass

        # Pull fills + compute realized P&L
        f = api._request("GET", "/fills")
        fills = (f.get("payload") or {}).get("fills") or f.get("payload") or []
        if isinstance(fills, dict):
            fills = fills.get("data") or []
        total_fills = len(fills) if isinstance(fills, list) else 0
        # Per-symbol cost-basis tracker — simple FIFO weighted-avg
        cost: dict[str, dict] = {}     # asset → {qty, avg_cost}
        realized = 0.0
        winning = 0
        for fil in (fills or []):
            if not isinstance(fil, dict):
                continue
            try:
                sym = fil.get("symbol", "")
                side = fil.get("side", "")
                qty = float(fil.get("quantity") or 0)
                price = float(fil.get("price") or 0)
                fee = float(fil.get("fee") or 0)
                if not sym or qty <= 0 or price <= 0:
                    continue
                base = sym.split("-")[0]
                pos = cost.setdefault(base, {"qty": 0.0, "avg": 0.0})
                if side == "buy":
                    new_qty = pos["qty"] + qty
                    pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / new_qty if new_qty else price
                    pos["qty"] = new_qty
                else:  # sell
                    if pos["qty"] > 0:
                        sell_pnl = (price - pos["avg"]) * min(qty, pos["qty"]) - fee
                        realized += sell_pnl
                        if sell_pnl > 0:
                            winning += 1
                        pos["qty"] = max(0, pos["qty"] - qty)
            except (TypeError, ValueError):
                continue

        equity_delta = equity - _STARTING_EQUITY_USDT
        unrealized = equity_delta - realized
        return {
            "equity_usdt": round(equity, 2),
            "equity_delta_usdt": round(equity_delta, 2),
            "realized_pnl_usdt": round(realized, 4),
            "unrealized_pnl_usdt": round(unrealized, 4),
            "total_fills": total_fills,
            "winning_fills": winning,
        }
    except Exception:
        return {
            "equity_usdt": None, "equity_delta_usdt": None,
            "realized_pnl_usdt": None, "unrealized_pnl_usdt": None,
            "total_fills": 0, "winning_fills": 0,
        }


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@dataclass
class TickRecord:
    tick_time: str
    symbol: str
    state: str
    ledger_size: int = 0
    ledger_delta: int = 0
    validation_passed: int = 0
    validation_total: int = 5
    learning_indicator: Optional[str] = None
    expected_R: Optional[float] = None
    expected_R_delta: Optional[float] = None
    win_rate: Optional[float] = None
    sharpe: Optional[float] = None
    weights_changed_count: int = 0
    weights_changed: list = field(default_factory=list)
    order_placed: bool = False
    order_id: Optional[str] = None
    trades_added: int = 0
    tick_elapsed_seconds: float = 0.0
    next_interval_seconds: int = 30


def _previous_row(conn: sqlite3.Connection, symbol: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM smc_training_history WHERE symbol=? ORDER BY id DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def record_tick(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    state: str,
    tick_payload: dict,
    learning_report: Optional[dict] = None,
    training_summary: Optional[dict] = None,
    elapsed: float = 0.0,
    pnl_snapshot: Optional[dict] = None,
) -> TickRecord:
    """Persist one tick row, computing deltas vs. previous tick."""
    ensure_training_history_schema(conn)

    prev = _previous_row(conn, symbol)

    progress = tick_payload.get("progress") or {}
    ledger_size = int(progress.get("ledger_size") or 0)
    prev_ledger = int((prev or {}).get("ledger_size") or 0)
    ledger_delta = ledger_size - prev_ledger
    val_passed = int(progress.get("validation_passed") or 0)
    val_total = int(progress.get("validation_total") or 5)
    indicator = progress.get("learning_indicator")

    # Pull stats from learning report if provided
    expected_R = None
    win_rate = None
    sharpe = None
    if learning_report:
        l1 = (learning_report.get("layer_1_statistics") or {})
        if isinstance(l1, dict):
            expect = (l1.get("expectancy") or {})
            expected_R = expect.get("expected_R")
            win_rate = expect.get("win_rate")
            sr = (l1.get("sharpe") or {})
            sharpe = sr.get("sharpe")

    prev_expected = (prev or {}).get("expected_R")
    expected_delta = None
    if expected_R is not None and prev_expected is not None:
        try:
            expected_delta = float(expected_R) - float(prev_expected)
        except (TypeError, ValueError):
            expected_delta = None

    # Training summary
    trades_added = int((training_summary or {}).get("trades_added") or 0) if training_summary else 0

    # Weight drift detection — compare current report's suggested weights to last
    weights_changed_list: list[str] = []
    if learning_report and prev:
        cur_l3 = ((learning_report.get("layer_3_calibration") or {}).get("suggested_weights") or {})
        try:
            prev_payload = json.loads(prev.get("payload_json") or "{}")
            prev_l3 = ((prev_payload.get("learning_report") or {}).get("layer_3_calibration") or {}).get("suggested_weights") or {}
        except Exception:
            prev_l3 = {}
        for k, v in (cur_l3 or {}).items():
            if prev_l3.get(k) != v:
                weights_changed_list.append(k)

    # Order info from tick payload
    live = tick_payload.get("live_order") or {}
    order_id = None
    order_placed = False
    if isinstance(live, dict) and live.get("id"):
        order_id = live.get("id")
        order_placed = True

    # P&L snapshot (real simulated-market equity / realized / unrealized)
    pnl = pnl_snapshot or {}
    prev_equity = (prev or {}).get("equity_usdt") if prev else None
    equity_now = pnl.get("equity_usdt")
    equity_delta_tick = None
    if equity_now is not None and prev_equity is not None:
        try:
            equity_delta_tick = float(equity_now) - float(prev_equity)
        except (TypeError, ValueError):
            equity_delta_tick = None

    rec = TickRecord(
        tick_time=tick_payload.get("tick_time") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        symbol=symbol, state=state,
        ledger_size=ledger_size, ledger_delta=ledger_delta,
        validation_passed=val_passed, validation_total=val_total,
        learning_indicator=indicator,
        expected_R=float(expected_R) if expected_R is not None else None,
        expected_R_delta=expected_delta,
        win_rate=float(win_rate) if win_rate is not None else None,
        sharpe=float(sharpe) if sharpe is not None else None,
        weights_changed_count=len(weights_changed_list),
        weights_changed=weights_changed_list,
        order_placed=order_placed, order_id=order_id,
        trades_added=trades_added,
        tick_elapsed_seconds=round(float(elapsed), 3),
    )
    # attach P&L fields to record via attribute setting (dataclass extras)
    rec_extras = {
        "equity_usdt": pnl.get("equity_usdt"),
        "equity_delta_usdt": pnl.get("equity_delta_usdt"),
        "equity_tick_delta_usdt": equity_delta_tick,
        "realized_pnl_usdt": pnl.get("realized_pnl_usdt"),
        "unrealized_pnl_usdt": pnl.get("unrealized_pnl_usdt"),
        "total_fills": int(pnl.get("total_fills") or 0),
        "winning_fills": int(pnl.get("winning_fills") or 0),
    }
    for k, v in rec_extras.items():
        setattr(rec, k, v)

    # Decide next interval (throttling)
    next_interval = decide_next_interval(conn, symbol, rec)
    rec.next_interval_seconds = next_interval

    # Persist
    conn.execute("""
        INSERT INTO smc_training_history (
            tick_time, symbol, state, ledger_size, ledger_delta,
            validation_passed, validation_total, learning_indicator,
            expected_R, expected_R_delta, win_rate, sharpe,
            weights_changed_count, weights_changed,
            order_placed, order_id, trades_added,
            tick_elapsed_seconds, next_interval_seconds,
            equity_usdt, equity_delta_usdt, realized_pnl_usdt,
            unrealized_pnl_usdt, total_fills, winning_fills,
            payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec.tick_time, rec.symbol, rec.state, rec.ledger_size, rec.ledger_delta,
        rec.validation_passed, rec.validation_total, rec.learning_indicator,
        rec.expected_R, rec.expected_R_delta, rec.win_rate, rec.sharpe,
        rec.weights_changed_count, json.dumps(rec.weights_changed),
        1 if rec.order_placed else 0, rec.order_id, rec.trades_added,
        rec.tick_elapsed_seconds, rec.next_interval_seconds,
        getattr(rec, "equity_usdt", None),
        getattr(rec, "equity_delta_usdt", None),
        getattr(rec, "realized_pnl_usdt", None),
        getattr(rec, "unrealized_pnl_usdt", None),
        getattr(rec, "total_fills", 0),
        getattr(rec, "winning_fills", 0),
        json.dumps({
            "tick_payload": tick_payload,
            "learning_report": learning_report,
            "training_summary": training_summary,
            "pnl_snapshot": pnl_snapshot,
        }, default=str),
    ))
    conn.commit()
    return rec


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

BASE_INTERVAL = 30            # 30s
PLATEAU_INTERVAL = 120        # 2 min
SATURATED_INTERVAL = 480      # 8 min
MAX_INTERVAL = 1800           # 30 min ceiling


def decide_next_interval(conn: sqlite3.Connection, symbol: str, rec: TickRecord) -> int:
    """Active → 30s, plateau → 2min, saturated → 8min.

    The current tick (``rec``) is NOT yet persisted, so we look at the
    last N rows already in DB and combine with ``rec``.
    """
    # Active short-circuit: if anything moved this tick, reset to base.
    if rec.ledger_delta > 0 or rec.weights_changed_count > 0 or rec.order_placed:
        return BASE_INTERVAL

    # Look at the last 5 historical rows
    rows = conn.execute(
        "SELECT ledger_delta, weights_changed_count, learning_indicator "
        "FROM smc_training_history WHERE symbol=? ORDER BY id DESC LIMIT 5",
        (symbol,),
    ).fetchall()
    history = [dict(r) for r in rows]
    # combined view: [current rec] + history (most recent first)
    combined = [{
        "ledger_delta": rec.ledger_delta,
        "weights_changed_count": rec.weights_changed_count,
        "learning_indicator": rec.learning_indicator,
    }] + history

    # Plateau if last 3 ticks: no sample growth AND no weight drift
    plateau = all(
        (h.get("ledger_delta") or 0) == 0 and (h.get("weights_changed_count") or 0) == 0
        for h in combined[:3]
    )
    # Saturated if last 6 ticks plateau AND indicator stagnant/insufficient
    saturated = (
        len(combined) >= 6
        and all(
            (h.get("ledger_delta") or 0) == 0 and (h.get("weights_changed_count") or 0) == 0
            for h in combined[:6]
        )
        and all(
            (h.get("learning_indicator") or "") in {"stagnant", "insufficient_data"}
            for h in combined[:6]
        )
    )
    if saturated:
        return SATURATED_INTERVAL
    if plateau:
        return PLATEAU_INTERVAL
    return BASE_INTERVAL


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def load_training_history(
    conn: sqlite3.Connection,
    *,
    symbol: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    ensure_training_history_schema(conn)
    where = "WHERE symbol = ?" if symbol else ""
    params: tuple = (symbol, limit) if symbol else (limit,)
    rows = conn.execute(
        f"SELECT id, tick_time, symbol, state, ledger_size, ledger_delta, "
        f"validation_passed, validation_total, learning_indicator, "
        f"expected_R, expected_R_delta, win_rate, sharpe, "
        f"weights_changed_count, weights_changed, order_placed, order_id, "
        f"trades_added, tick_elapsed_seconds, next_interval_seconds, "
        f"equity_usdt, equity_delta_usdt, realized_pnl_usdt, "
        f"unrealized_pnl_usdt, total_fills, winning_fills "
        f"FROM smc_training_history {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["weights_changed"] = json.loads(d.get("weights_changed") or "[]")
        except Exception:
            d["weights_changed"] = []
        out.append(d)
    return out


def summarize_training_history(
    conn: sqlite3.Connection,
    *,
    symbol: Optional[str] = None,
) -> dict:
    """Aggregate metrics across all ticks for the symbol."""
    ensure_training_history_schema(conn)
    where = "WHERE symbol = ?" if symbol else ""
    params: tuple = (symbol,) if symbol else ()
    row = conn.execute(
        f"SELECT COUNT(*) AS total_ticks, "
        f"SUM(ledger_delta) AS total_new_samples, "
        f"SUM(trades_added) AS total_trades_added, "
        f"SUM(order_placed) AS total_orders, "
        f"SUM(weights_changed_count) AS total_weight_changes, "
        f"AVG(tick_elapsed_seconds) AS avg_tick_elapsed, "
        f"MAX(ledger_size) AS peak_ledger_size, "
        f"AVG(expected_R) AS avg_expected_R, "
        f"MAX(expected_R) AS best_expected_R, "
        f"MIN(expected_R) AS worst_expected_R, "
        f"AVG(win_rate) AS avg_win_rate, "
        f"AVG(sharpe) AS avg_sharpe, "
        f"MAX(equity_usdt) AS peak_equity_usdt, "
        f"MIN(equity_usdt) AS trough_equity_usdt, "
        f"MAX(realized_pnl_usdt) AS best_realized_pnl, "
        f"MIN(realized_pnl_usdt) AS worst_realized_pnl, "
        f"MAX(total_fills) AS total_fills, "
        f"MAX(winning_fills) AS total_winning_fills "
        f"FROM smc_training_history {where}",
        params,
    ).fetchone()
    if not row:
        return {"total_ticks": 0}
    d = dict(row)
    # Latest row gives current snapshot
    latest = conn.execute(
        f"SELECT state, ledger_size, learning_indicator, expected_R, win_rate, "
        f"sharpe, next_interval_seconds, tick_time, "
        f"equity_usdt, equity_delta_usdt, realized_pnl_usdt, "
        f"unrealized_pnl_usdt, total_fills, winning_fills "
        f"FROM smc_training_history {where} ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if latest:
        d["latest"] = dict(latest)
    # State distribution
    state_rows = conn.execute(
        f"SELECT state, COUNT(*) AS n FROM smc_training_history {where} GROUP BY state",
        params,
    ).fetchall()
    d["state_distribution"] = {r["state"]: r["n"] for r in state_rows}
    # First/last tick times
    span = conn.execute(
        f"SELECT MIN(tick_time) AS first_tick, MAX(tick_time) AS last_tick "
        f"FROM smc_training_history {where}",
        params,
    ).fetchone()
    if span:
        d["first_tick"] = span["first_tick"]
        d["last_tick"] = span["last_tick"]
    return d
