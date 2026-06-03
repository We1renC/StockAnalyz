"""Persistence and summary helpers for SMC backtest results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Optional


def persist_backtest_run(conn, result: dict, period: str, source: str = "") -> int:
    metrics = result.get("metrics") or {}
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO smc_backtest_runs
           (symbol, market, timeframe, period, source, generated_at, bars,
            total_trades, win_rate, profit_factor, expectancy_r, max_drawdown,
            ending_equity, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result.get("symbol"),
            result.get("market"),
            result.get("timeframe"),
            period,
            source or "",
            result.get("generated_at"),
            result.get("bars"),
            metrics.get("total_trades"),
            metrics.get("win_rate"),
            metrics.get("profit_factor"),
            metrics.get("expectancy_r"),
            metrics.get("max_drawdown"),
            metrics.get("ending_equity"),
            json.dumps(result, ensure_ascii=False),
            created_at,
        ),
    )
    run_id = int(cursor.lastrowid)
    trades = result.get("trades") or []
    if trades:
        cursor.executemany(
            """INSERT INTO smc_backtest_trades
               (run_id, symbol, market, timeframe, trade_id, direction, model,
                entry_time, exit_time, entry_price, exit_price, stop_price, tp1_price,
                qty, pnl, r_multiple, score, threshold, feature_vector, dol_target,
                exit_reason, holding_bars, win, mae, mfe)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    trade.get("symbol"),
                    trade.get("market"),
                    trade.get("timeframe"),
                    trade.get("trade_id"),
                    trade.get("direction"),
                    trade.get("model"),
                    trade.get("entry_time"),
                    trade.get("exit_time"),
                    trade.get("entry"),
                    trade.get("exit"),
                    trade.get("stop"),
                    trade.get("tp1"),
                    trade.get("qty"),
                    trade.get("pnl"),
                    trade.get("r_multiple"),
                    trade.get("score"),
                    trade.get("threshold"),
                    json.dumps(trade.get("feature_vector") or {}, ensure_ascii=False),
                    json.dumps(trade.get("dol_target") or {}, ensure_ascii=False),
                    trade.get("exit_reason"),
                    trade.get("holding_bars"),
                    1 if trade.get("win") else 0,
                    trade.get("mae"),
                    trade.get("mfe"),
                )
                for trade in trades
            ],
        )
    conn.commit()
    return run_id


def summarize_backtest_report(conn, symbol: Optional[str] = None, limit_runs: int = 200) -> dict:
    params: list = []
    where = ""
    if symbol:
        where = "WHERE symbol = ?"
        params.append(symbol.upper())
    run_rows = [
        dict(r)
        for r in conn.execute(
            f"""SELECT * FROM smc_backtest_runs
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            params + [limit_runs],
        ).fetchall()
    ]
    trade_rows = [
        dict(r)
        for r in conn.execute(
            f"""SELECT * FROM smc_backtest_trades
                {where}
                ORDER BY entry_time DESC, id DESC
                LIMIT ?""",
            params + [limit_runs * 50],
        ).fetchall()
    ]
    by_symbol: dict[str, dict] = {}
    for row in trade_rows:
        bucket = by_symbol.setdefault(
            row["symbol"],
            {
                "symbol": row["symbol"],
                "market": row["market"],
                "trade_count": 0,
                "wins": 0,
                "pnl": 0.0,
                "r_total": 0.0,
                "avg_holding_bars": 0.0,
            },
        )
        bucket["trade_count"] += 1
        bucket["wins"] += int(row.get("win") or 0)
        bucket["pnl"] += float(row.get("pnl") or 0)
        bucket["r_total"] += float(row.get("r_multiple") or 0)
        bucket["avg_holding_bars"] += float(row.get("holding_bars") or 0)
    for bucket in by_symbol.values():
        count = bucket["trade_count"] or 1
        bucket["win_rate"] = round(bucket["wins"] / count, 4)
        bucket["expectancy_r"] = round(bucket["r_total"] / count, 4)
        bucket["avg_holding_bars"] = round(bucket["avg_holding_bars"] / count, 2)
        bucket["pnl"] = round(bucket["pnl"], 4)
        del bucket["r_total"]
    latest_runs = []
    for row in run_rows[:20]:
        latest_runs.append(
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "market": row["market"],
                "period": row["period"],
                "source": row["source"],
                "created_at": row["created_at"],
                "total_trades": row["total_trades"],
                "win_rate": row["win_rate"],
                "profit_factor": row["profit_factor"],
                "expectancy_r": row["expectancy_r"],
                "max_drawdown": row["max_drawdown"],
            }
        )
    return {
        "run_count": len(run_rows),
        "trade_count": len(trade_rows),
        "symbols": sorted(by_symbol.values(), key=lambda x: (x["expectancy_r"], x["pnl"]), reverse=True),
        "latest_runs": latest_runs,
    }
