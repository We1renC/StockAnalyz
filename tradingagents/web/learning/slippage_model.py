"""Empirical slippage model — learn slippage from real fills.

Audit fix P2-13. ``paper_execution.simulate_market_order`` currently
uses a fixed 0.05% volatility-impact heuristic for slippage. That means
the dry-run / model picture is always rosier than the live picture.

This module:

  1. Pulls real fills from the crypto-api (``/v1/fills``) joined with
     the order's submitted price (``client_order_id`` lookup).
  2. Computes per-fill slippage in basis points:
       (avg_fill - submitted_price) / submitted_price × 10_000 × side_sign
  3. Aggregates per ``(symbol, side)`` percentile distribution:
       p50 (median) / p75 / p90 / max
  4. Returns ``sample_slippage_bps(symbol, side)`` closure that draws a
     plausible slippage from the empirical distribution (defaults to
     p50 if not enough data, falls back to 5 bps if no fills at all).

The dry-run path uses ``sample_slippage_bps`` instead of the fixed
0.05% so backtest expectancy mirrors live frictions.
"""

from __future__ import annotations

import math
from typing import Callable, Optional


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if q <= 0:
        return s[0]
    if q >= 1:
        return s[-1]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def estimate_slippage_distribution(
    fills: list[dict],
    submitted_prices: dict[str, float],
) -> dict[tuple[str, str], dict]:
    """Compute per-(symbol, side) slippage bps distribution.

    Parameters
    ----------
    fills : list[dict]
        Output of crypto-api `/v1/fills`. Each row must have ``symbol``,
        ``side``, ``price``, and either ``order_id`` or
        ``client_order_id`` to join back to the submitted price.
    submitted_prices : dict
        ``{order_id_or_client_id: submitted_limit_price}``.

    Returns
    -------
    {(symbol, side): {
        "n": int,
        "p50_bps": float,
        "p75_bps": float,
        "p90_bps": float,
        "max_bps": float,
        "mean_bps": float,
    }}

    Notes
    -----
    A POSITIVE slippage bps means the fill was WORSE than expected
    (buy filled higher than submitted, sell filled lower). Negative
    means price improvement.
    """
    buckets: dict[tuple[str, str], list[float]] = {}
    for f in fills or []:
        if not isinstance(f, dict):
            continue
        sym = f.get("symbol")
        side = (f.get("side") or "").lower()
        if not sym or side not in ("buy", "sell"):
            continue
        try:
            fill_px = float(f.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if fill_px <= 0:
            continue
        sub_px = None
        for key in ("order_id", "broker_order_id", "client_order_id"):
            oid = f.get(key)
            if oid and oid in submitted_prices:
                try:
                    sub_px = float(submitted_prices[oid])
                except (TypeError, ValueError):
                    sub_px = None
                if sub_px and sub_px > 0:
                    break
        if not sub_px or sub_px <= 0:
            continue
        sign = 1 if side == "buy" else -1
        slip_bps = ((fill_px - sub_px) / sub_px) * 10_000 * sign
        buckets.setdefault((sym, side), []).append(float(slip_bps))

    out: dict[tuple[str, str], dict] = {}
    for k, vals in buckets.items():
        out[k] = {
            "n": len(vals),
            "p50_bps": round(_percentile(vals, 0.50), 3),
            "p75_bps": round(_percentile(vals, 0.75), 3),
            "p90_bps": round(_percentile(vals, 0.90), 3),
            "max_bps": round(max(vals), 3),
            "mean_bps": round(sum(vals) / len(vals), 3),
        }
    return out


def build_slippage_sampler(
    distribution: dict[tuple[str, str], dict],
    *,
    default_bps: float = 5.0,
    min_samples_for_real: int = 8,
    percentile: float = 0.75,
) -> Callable[[str, str], float]:
    """Return a ``sample_slippage_bps(symbol, side) -> float`` closure.

    Strategy:
      • If (symbol, side) has ≥ ``min_samples_for_real`` fills → return
        ``percentile`` (default P75) — pessimistic but realistic
      • Otherwise fall back to ``default_bps``

    P75 default (not P50) means we're slightly conservative on dry-run
    cost estimates — better to overestimate friction than underestimate.
    """
    cache = distribution or {}

    def sampler(symbol: str, side: str) -> float:
        side_l = (side or "").lower()
        k = (symbol, side_l)
        info = cache.get(k)
        if info and info.get("n", 0) >= min_samples_for_real:
            field = f"p{int(percentile * 100)}_bps"
            return float(info.get(field) or info.get("p75_bps") or default_bps)
        return float(default_bps)

    return sampler


def fetch_fills_and_orders(api) -> tuple[list[dict], dict[str, float]]:
    """Pull fills + order book from crypto-api into a uniform shape.

    Returns ``(fills, submitted_prices)`` ready for
    ``estimate_slippage_distribution``.
    """
    fills: list[dict] = []
    submitted: dict[str, float] = {}
    try:
        resp = api._request("GET", "/fills")
        payload = resp.get("payload") or {}
        if isinstance(payload, dict):
            raw = payload.get("fills") or payload.get("data") or []
            if isinstance(raw, dict) and isinstance(raw.get("fills"), list):
                raw = raw["fills"]
            if isinstance(raw, list):
                fills = raw
        elif isinstance(payload, list):
            fills = payload
    except Exception:
        pass

    try:
        resp = api._request("GET", "/orders", params={"limit": 200})
        payload = resp.get("payload") or {}
        rows: list = []
        if isinstance(payload, dict):
            rows = payload.get("orders") or payload.get("data") or []
            if isinstance(rows, dict) and isinstance(rows.get("orders"), list):
                rows = rows["orders"]
        elif isinstance(payload, list):
            rows = payload
        for o in rows or []:
            if not isinstance(o, dict):
                continue
            try:
                price = float(o.get("price") or 0)
            except (TypeError, ValueError):
                price = 0
            if price <= 0:
                continue
            for key in ("id", "order_id", "client_order_id"):
                oid = o.get(key)
                if oid:
                    submitted[str(oid)] = price
    except Exception:
        pass

    return fills, submitted


def build_runtime_sampler(api, **kwargs) -> Callable[[str, str], float]:
    """One-call: pull fresh fills + return sampler closure."""
    fills, subs = fetch_fills_and_orders(api)
    dist = estimate_slippage_distribution(fills, subs)
    return build_slippage_sampler(dist, **kwargs)
