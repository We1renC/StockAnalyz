"""Reconcile missed-signals jsonl with actual subsequent price action.

Audit fix P2-15+. ``smc_paper_runner._log_missed_signal`` stamps every
rejected SMC candidate (qualified but below threshold) into
``missed_signals_<symbol>.jsonl`` with empty outcome fields. Without
filling those fields the data never becomes a feedback signal: we know
the runner rejected score=7 but we never check whether score=7 would
have hit target or stop.

This module polls the crypto-api klines, finds the bars that came AFTER
each missed signal's logged timestamp, and computes:

  • outcome_at_5_bars   — did target / stop hit within 5 bars?
  • outcome_at_20_bars  — same window extended to 20 bars
  • max_favorable_R     — best run vs stop distance (R units)
  • max_adverse_R       — worst dip vs stop distance

Rows that already have non-null outcomes are skipped. Rows whose log
timestamp is younger than the test window are also skipped.

Usage:
    reconcile_missed_signals(api, "missed_signals_BTC-USDT.jsonl",
                              interval="15m")
    -> {"matched": N, "skipped_young": M, "skipped_done": K}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


_INTERVAL_TO_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480,
    "1d": 1440, "1w": 10080,
}


def _interval_minutes(interval: str) -> int:
    return _INTERVAL_TO_MINUTES.get(interval, 15)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    from learning.file_lock import locked_read
    with locked_read(str(path)):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    # Audit fix A1: hold exclusive lock around read-modify-write rewrite
    # so concurrent appenders can't interleave half a line.
    from learning.file_lock import locked_rewrite
    with locked_rewrite(str(path)):
        path.write_text(
            "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )


def _is_resolved(row: dict) -> bool:
    return row.get("outcome_at_20_bars") is not None


def _candidate_outcome(
    row: dict,
    future_bars: list[dict],
    horizon: int,
) -> dict:
    """Return outcome stats for the first ``horizon`` future bars."""
    direction = int(row.get("direction") or 0)
    entry = float(row.get("entry") or 0)
    stop = float(row.get("stop") or 0)
    target = float(row.get("target") or 0)
    if direction == 0 or entry <= 0 or stop <= 0 or target <= 0:
        return {"outcome": "invalid_plan"}
    risk = abs(entry - stop)
    if risk <= 0:
        return {"outcome": "zero_risk"}

    bars = future_bars[:horizon]
    if not bars:
        return {"outcome": "no_future_bars"}

    max_fav = 0.0
    max_adv = 0.0
    outcome = "open"
    for b in bars:
        try:
            high = float(b.get("high") or 0)
            low = float(b.get("low") or 0)
        except (TypeError, ValueError):
            continue
        if direction == 1:
            fav = (high - entry) / risk
            adv = (entry - low) / risk
            if low <= stop:
                outcome = "stop"
                break
            if high >= target:
                outcome = "target"
                break
        else:
            fav = (entry - low) / risk
            adv = (high - entry) / risk
            if high >= stop:
                outcome = "stop"
                break
            if low <= target:
                outcome = "target"
                break
        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)
    return {
        "outcome": outcome,
        "max_favorable_R": round(max_fav, 3),
        "max_adverse_R": round(max_adv, 3),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class MissedSignalsReconcileResult:
    started_at: str
    total_rows: int = 0
    matched: int = 0
    skipped_done: int = 0
    skipped_too_young: int = 0
    skipped_no_kline: int = 0
    errors: list[str] = field(default_factory=list)


def reconcile_missed_signals(
    api,
    missed_path: str,
    *,
    interval: str = "15m",
    horizon_short: int = 5,
    horizon_long: int = 20,
    kline_pull_bars: int = 500,
) -> MissedSignalsReconcileResult:
    """Walk missed_signals jsonl, fill outcome_at_5/20_bars + MAE/MFE_R."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = MissedSignalsReconcileResult(started_at=started_at)
    path = Path(missed_path)
    rows = _read_jsonl(path)
    result.total_rows = len(rows)
    if not rows:
        return result

    interval_min = _interval_minutes(interval)
    # Group by symbol so each /klines call covers many rows
    by_symbol: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        sym = r.get("symbol")
        if sym:
            by_symbol.setdefault(sym, []).append(i)

    klines_by_symbol: dict[str, list[dict]] = {}
    for sym in by_symbol:
        try:
            resp = api.klines(sym, interval=interval, limit=kline_pull_bars)
            if resp.get("status") != 200:
                result.errors.append(f"klines_{sym}_status_{resp.get('status')}")
                continue
            data = (resp.get("payload") or {}).get("data") or []
            klines_by_symbol[sym] = data
        except Exception as exc:
            result.errors.append(f"klines_{sym}:{exc}")

    now_utc = datetime.now(timezone.utc)
    for i, row in enumerate(rows):
        if _is_resolved(row):
            result.skipped_done += 1
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        logged_at = _parse_ts(row.get("logged_at"))
        if logged_at is None:
            continue
        # Need at least horizon_long bars of future data before we judge.
        min_future = horizon_long * interval_min
        if logged_at > now_utc - timedelta(minutes=min_future):
            result.skipped_too_young += 1
            continue
        bars = klines_by_symbol.get(sym) or []
        if not bars:
            result.skipped_no_kline += 1
            continue
        # Find first bar whose open_time >= logged_at
        future_bars = []
        for b in bars:
            ts = _parse_ts(b.get("open_time"))
            if ts and ts >= logged_at:
                future_bars.append(b)
        if not future_bars:
            result.skipped_no_kline += 1
            continue
        short = _candidate_outcome(row, future_bars, horizon_short)
        long_ = _candidate_outcome(row, future_bars, horizon_long)
        row["outcome_at_5_bars"] = short.get("outcome")
        row["outcome_at_20_bars"] = long_.get("outcome")
        row["max_favorable_R"] = long_.get("max_favorable_R")
        row["max_adverse_R"] = long_.get("max_adverse_R")
        row["reconciled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result.matched += 1

    _write_jsonl(path, rows)
    return result
