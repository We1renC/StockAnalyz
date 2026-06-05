"""Paper-trade outcome reconciler.

Audit fix P0-2 — the live paper-trading runner stamps each new trade
with ``outcome="pending"`` because it doesn't know yet whether the
order will hit target / stop / time out. This module periodically polls
``/v1/fills`` + current ticker, matches fills back to pending trades by
``broker_order_id`` / ``client_order_id``, and resolves outcomes:

  • Fully filled then price reaches plan_target → outcome="target", r=+RR
  • Fully filled then price reaches plan_stop  → outcome="stop",  r=-1
  • Partially filled, current price didn't hit either → outcome unchanged
  • No fill within ``stale_minutes`` → outcome="flat", r=0 (timed out)

The resolved trades are written back to the ledger as NEW rows with
status updated; ``persist_trade_records`` dedup (P0-1) keeps the count
honest. The original ``pending`` row is left as-is so the audit trail
preserves the open-pending → closed transition.

Usage:
    reconcile_paper_trades(api, ledger_path, stale_minutes=720)
    -> {"matched": N, "resolved": M, "stale": K}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from smc_paper_runner import CryptoApiClient
from smc_quant import persist_trade_records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_ledger(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _is_pending(rec: dict) -> bool:
    return (rec.get("outcome") or "pending") == "pending"


def _extract_fills(api: CryptoApiClient, symbol: Optional[str] = None) -> list[dict]:
    params = {"symbol": symbol} if symbol else None
    resp = api._request("GET", "/fills", params=params)
    if resp.get("status") != 200:
        return []
    payload = resp.get("payload") or {}
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("fills"), list):
            return payload["fills"]
        d = payload.get("data")
        if isinstance(d, dict):
            if isinstance(d.get("fills"), list):
                return d["fills"]
    if isinstance(payload, list):
        return payload
    return []


def _extract_ticker_price(api: CryptoApiClient, symbol: str) -> Optional[float]:
    t = api.ticker(symbol)
    if t.get("status") != 200:
        return None
    p = (t.get("payload") or {}).get("price") or (t.get("payload") or {}).get("last_price")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


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


# ---------------------------------------------------------------------------
# Outcome derivation
# ---------------------------------------------------------------------------

def _resolve_outcome(
    rec: dict,
    fills_by_order: dict[str, list[dict]],
    current_price: Optional[float],
    *,
    stale_threshold: datetime,
) -> Optional[dict]:
    """Return the patched rec with resolved outcome, or None if still pending.

    Decision tree (per audit feedback):
      1. No fills at all → if entry older than stale_threshold → outcome=flat (r=0)
      2. Order partially filled + price hit stop → outcome=stop, r=-1
      3. Order partially filled + price hit target → outcome=target, r=plan_rr
      4. Otherwise still pending
    """
    broker_id = rec.get("broker_order_id") or ""
    client_id = rec.get("client_order_id") or ""
    fills = fills_by_order.get(broker_id, []) + fills_by_order.get(client_id, [])
    plan_entry = float(rec.get("plan_entry") or rec.get("entry_price") or 0)
    plan_stop = float(rec.get("plan_stop") or rec.get("stop") or 0)
    plan_target = float(rec.get("plan_target") or rec.get("target") or 0)
    direction = int(rec.get("direction") or 0)
    rr_planned = float(rec.get("rr_planned") or 0)
    entry_time = _parse_ts(rec.get("entry_time"))

    if not fills:
        if entry_time and entry_time < stale_threshold:
            patched = dict(rec)
            patched["outcome"] = "flat"
            patched["r_multiple"] = 0.0
            patched["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            patched["resolution_reason"] = "stale_no_fill"
            return patched
        return None

    # We have at least one fill — determine fill price (avg of fills) + qty
    total_qty = sum(float(f.get("quantity") or 0) for f in fills)
    if total_qty <= 0:
        return None
    avg_fill_price = sum(
        float(f.get("price") or 0) * float(f.get("quantity") or 0) for f in fills
    ) / total_qty

    # If current price not available we can't tell if target/stop was hit
    if current_price is None or plan_stop <= 0 or plan_target <= 0 or direction == 0:
        return None

    risk = abs(plan_entry - plan_stop)
    if risk <= 0:
        return None

    patched = dict(rec)
    patched["actual_entry_price"] = round(avg_fill_price, 6)
    patched["actual_filled_qty"] = total_qty
    patched["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Long → stop pierced when current <= stop; target hit when current >= target
    # Short → reverse
    if direction == 1:
        if current_price <= plan_stop:
            patched["outcome"] = "stop"
            patched["r_multiple"] = -1.0
            patched["resolution_reason"] = "stop_pierced_long"
            return patched
        if current_price >= plan_target:
            patched["outcome"] = "target"
            patched["r_multiple"] = rr_planned if rr_planned > 0 else (plan_target - plan_entry) / risk
            patched["resolution_reason"] = "target_hit_long"
            return patched
    else:
        if current_price >= plan_stop:
            patched["outcome"] = "stop"
            patched["r_multiple"] = -1.0
            patched["resolution_reason"] = "stop_pierced_short"
            return patched
        if current_price <= plan_target:
            patched["outcome"] = "target"
            patched["r_multiple"] = rr_planned if rr_planned > 0 else (plan_entry - plan_target) / risk
            patched["resolution_reason"] = "target_hit_short"
            return patched

    # Filled but neither stop nor target hit; check staleness
    if entry_time and entry_time < stale_threshold:
        patched["outcome"] = "flat"
        # MTM PnL as r-multiple
        if direction == 1:
            patched["r_multiple"] = (current_price - plan_entry) / risk
        else:
            patched["r_multiple"] = (plan_entry - current_price) / risk
        patched["resolution_reason"] = "stale_filled_mtm"
        return patched
    return None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

@dataclass
class ReconcileResult:
    started_at: str
    pending_count: int = 0
    matched: int = 0
    resolved_target: int = 0
    resolved_stop: int = 0
    resolved_flat: int = 0
    still_pending: int = 0
    errors: list[str] = field(default_factory=list)


def reconcile_paper_trades(
    api: CryptoApiClient,
    ledger_path: str,
    *,
    symbols: Optional[list[str]] = None,
    stale_minutes: int = 720,
) -> ReconcileResult:
    """Poll fills + ticker, resolve pending trades, write resolved rows back."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = ReconcileResult(started_at=started_at)
    try:
        ledger = _load_ledger(ledger_path)
    except Exception as exc:
        result.errors.append(f"load_ledger:{exc}")
        return result

    pending = [r for r in ledger if _is_pending(r)]
    result.pending_count = len(pending)
    if not pending:
        return result

    target_symbols = symbols or list({r.get("symbol") for r in pending if r.get("symbol")})

    fills_by_order: dict[str, list[dict]] = {}
    for sym in target_symbols:
        try:
            for f in _extract_fills(api, sym):
                if not isinstance(f, dict):
                    continue
                for key in ("order_id", "broker_order_id", "client_order_id"):
                    oid = f.get(key)
                    if oid:
                        fills_by_order.setdefault(str(oid), []).append(f)
        except Exception as exc:
            result.errors.append(f"fills_{sym}:{exc}")

    current_prices: dict[str, Optional[float]] = {}
    for sym in target_symbols:
        try:
            current_prices[sym] = _extract_ticker_price(api, sym)
        except Exception as exc:
            result.errors.append(f"ticker_{sym}:{exc}")
            current_prices[sym] = None

    stale_threshold = datetime.now(timezone.utc) - timedelta(minutes=int(stale_minutes))

    resolved: list[dict] = []
    for rec in pending:
        sym = rec.get("symbol")
        patched = _resolve_outcome(
            rec, fills_by_order, current_prices.get(sym),
            stale_threshold=stale_threshold,
        )
        if patched is None:
            result.still_pending += 1
            continue
        result.matched += 1
        outcome = patched.get("outcome")
        if outcome == "target":
            result.resolved_target += 1
        elif outcome == "stop":
            result.resolved_stop += 1
        elif outcome == "flat":
            result.resolved_flat += 1
        resolved.append(patched)

    if resolved:
        # Use unique trade_id derived from broker_order_id so dedup (P0-1)
        # treats the resolved row as a distinct settled record vs the pending one.
        for r in resolved:
            r["trade_id"] = (
                f"{r.get('symbol','')}:{r.get('broker_order_id') or r.get('client_order_id','')}:resolved"
            )
        persist_trade_records(resolved, ledger_path, dedup=True)

    return result
