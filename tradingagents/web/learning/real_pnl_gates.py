"""Hard real-PnL acceptance gates.

Audit fix P3-17. The 21 paper_acceptance gates currently rely heavily
on scenario stubs and synthetic evidence (market_crash, liquidity_sweep
fixtures). They're useful but they don't answer the simplest
"production-ready" question:

  > "Did we actually make money on real fills in the last 30 days?"

This module adds three hard, ledger-derived gates that ONLY consult
real resolved trades:

  1. recent_30d_real_pnl_gate
     Net R-multiple over the last 30 days must be ≥ ``min_total_R``.
     With min=0.5: 30 trades × 0.02R average → ~0.6R total → passes.
     This catches "system flat-lined and bled fees" without any
     synthetic gate noticing.

  2. live_vs_backtest_correlation_gate
     For each model+symbol pair, correlation between backtest-derived
     E[R] and resolved-paper-trade E[R] must be ≥ ``min_correlation``.
     Catches "model passes backtest but real fills tell a different
     story" (slippage, fee, regime drift).

  3. max_drawdown_30d_gate
     The peak-to-trough equity curve drawdown over 30d expressed in
     R-units must be ≤ ``max_drawdown_R``. With max=8R: a 30d max DD
     of 8R is the soft ceiling; 10R worth of consecutive losses
     triggers failure.

All three gates emit the same shape:
  {
    "gate_id": str,
    "passed": bool,
    "metric": float,
    "threshold": float,
    "severity": float,
    "reason": str,
    "n_samples": int,
  }

These can be merged into ``build_gate_results`` output (with
underscore-meta key isolation per the P3-fix) so they appear as
first-class gates rather than warnings.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Optional


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


def _resolved(records: list[dict]) -> list[dict]:
    out = []
    for r in records or []:
        outcome = r.get("outcome")
        rm = r.get("r_multiple")
        if outcome in (None, "pending") or rm is None:
            continue
        try:
            float(rm)
        except (TypeError, ValueError):
            continue
        out.append(r)
    return out


def _within_window(records: list[dict], days: int, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    out = []
    for r in records:
        ts = _parse_ts(r.get("entry_time"))
        if ts and ts >= cutoff:
            out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────
# Gate 1: recent 30d real PnL
# ─────────────────────────────────────────────────────────────────

def recent_30d_real_pnl_gate(
    records: list[dict],
    *,
    min_total_R: float = 0.5,
    days: int = 30,
    now: Optional[datetime] = None,
) -> dict:
    """Net R-multiple over the last 30 days must be ≥ threshold."""
    resolved = _resolved(records)
    window = _within_window(resolved, days=days, now=now)
    total_R = sum(float(r["r_multiple"]) for r in window) if window else 0.0
    passed = total_R >= float(min_total_R)
    return {
        "gate_id": f"recent_{days}d_real_pnl",
        "passed": bool(passed),
        "metric": round(total_R, 4),
        "threshold": float(min_total_R),
        "severity": 0.0 if passed else min(1.0, max(0.0, (min_total_R - total_R) / max(1.0, abs(min_total_R)))),
        "reason": (
            f"net_R_below_min" if not passed
            else "net_R_ok"
        ),
        "n_samples": len(window),
    }


# ─────────────────────────────────────────────────────────────────
# Gate 2: live vs backtest correlation
# ─────────────────────────────────────────────────────────────────

def _per_cluster_E_R(records: list[dict], cluster_keys: tuple[str, ...]) -> dict[tuple, float]:
    buckets: dict[tuple, list[float]] = {}
    for r in records:
        key = tuple(str(r.get(k) or "") for k in cluster_keys)
        try:
            buckets.setdefault(key, []).append(float(r["r_multiple"]))
        except (TypeError, ValueError):
            continue
    return {k: (sum(v) / len(v) if v else 0.0) for k, v in buckets.items()}


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return None
    return num / math.sqrt(dx * dy)


def live_vs_backtest_correlation_gate(
    records: list[dict],
    *,
    min_correlation: float = 0.3,
    cluster_keys: tuple[str, ...] = ("model", "symbol"),
) -> dict:
    """Correlation between backtest-derived and paper-derived E[R].

    Records with ``source="backtest"`` are the backtest pool;
    ``source`` in {"paper", "live"} or stamped with ``broker_order_id``
    are the live pool. We group by ``cluster_keys`` and correlate the
    two per-cluster E[R] vectors.
    """
    resolved = _resolved(records)
    backtest = [r for r in resolved if r.get("source") == "backtest"]
    live = [r for r in resolved
              if r.get("source") in {"paper", "live"} or r.get("broker_order_id")]
    if not backtest or not live:
        return {
            "gate_id": "live_vs_backtest_correlation",
            "passed": False,
            "metric": 0.0,
            "threshold": float(min_correlation),
            "severity": 1.0,
            "reason": "insufficient_pool",
            "n_samples": len(backtest) + len(live),
        }
    bt_map = _per_cluster_E_R(backtest, cluster_keys)
    live_map = _per_cluster_E_R(live, cluster_keys)
    overlap = sorted(set(bt_map.keys()) & set(live_map.keys()))
    if len(overlap) < 2:
        return {
            "gate_id": "live_vs_backtest_correlation",
            "passed": False,
            "metric": 0.0,
            "threshold": float(min_correlation),
            "severity": 1.0,
            "reason": "insufficient_overlap_clusters",
            "n_samples": len(overlap),
        }
    xs = [bt_map[k] for k in overlap]
    ys = [live_map[k] for k in overlap]
    corr = _pearson(xs, ys) or 0.0
    passed = corr >= float(min_correlation)
    return {
        "gate_id": "live_vs_backtest_correlation",
        "passed": bool(passed),
        "metric": round(float(corr), 4),
        "threshold": float(min_correlation),
        "severity": 0.0 if passed else min(1.0, max(0.0, (min_correlation - corr) / max(1e-9, min_correlation))),
        "reason": ("correlation_ok" if passed else "correlation_low"),
        "n_samples": len(overlap),
    }


# ─────────────────────────────────────────────────────────────────
# Gate 3: 30d max drawdown
# ─────────────────────────────────────────────────────────────────

def max_drawdown_30d_gate(
    records: list[dict],
    *,
    max_drawdown_R: float = 8.0,
    days: int = 30,
    now: Optional[datetime] = None,
) -> dict:
    """Peak-to-trough equity drawdown over 30 days in R-units must be ≤ threshold."""
    resolved = _resolved(records)
    window = _within_window(resolved, days=days, now=now)
    if not window:
        return {
            "gate_id": f"max_drawdown_{days}d",
            "passed": True,           # no data — fail-open here (separate
            "metric": 0.0,            # gate `recent_pnl` catches starvation)
            "threshold": float(max_drawdown_R),
            "severity": 0.0,
            "reason": "no_resolved_trades_in_window",
            "n_samples": 0,
        }
    window.sort(key=lambda r: _parse_ts(r.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in window:
        try:
            equity += float(r["r_multiple"])
        except (TypeError, ValueError):
            continue
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    passed = max_dd <= float(max_drawdown_R)
    return {
        "gate_id": f"max_drawdown_{days}d",
        "passed": bool(passed),
        "metric": round(float(max_dd), 4),
        "threshold": float(max_drawdown_R),
        "severity": 0.0 if passed else min(1.0, max(0.0, (max_dd - max_drawdown_R) / max(1e-9, max_drawdown_R))),
        "reason": (
            "drawdown_exceeded" if not passed
            else "drawdown_ok"
        ),
        "n_samples": len(window),
    }


# ─────────────────────────────────────────────────────────────────
# Convenience: run all three
# ─────────────────────────────────────────────────────────────────

def run_real_pnl_gates(
    records: list[dict],
    *,
    min_total_R: float = 0.5,
    min_correlation: float = 0.3,
    max_drawdown_R: float = 8.0,
    days: int = 30,
    now: Optional[datetime] = None,
) -> dict:
    """Run all three real-PnL gates and return a flat dict + overall pass."""
    g1 = recent_30d_real_pnl_gate(records, min_total_R=min_total_R, days=days, now=now)
    g2 = live_vs_backtest_correlation_gate(records, min_correlation=min_correlation)
    g3 = max_drawdown_30d_gate(records, max_drawdown_R=max_drawdown_R, days=days, now=now)
    gates = {g1["gate_id"]: g1, g2["gate_id"]: g2, g3["gate_id"]: g3}
    all_pass = all(g["passed"] for g in gates.values())
    return {
        "all_passed": bool(all_pass),
        "gates": gates,
        "failures": [g["gate_id"] for g in gates.values() if not g["passed"]],
    }
