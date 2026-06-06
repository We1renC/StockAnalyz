"""Per-cluster ensemble — separate stats per (model, symbol, interval, regime).

Audit fix P3-19. Today the learning loop computes ONE per-factor weight
set applied to ALL contexts. Reality:

  • sweep_reversal in trending regime ≠ sweep_reversal in ranging regime
  • BTC 1h ≠ ETH 15m
  • Asia session ≠ London open

We need separate weight slices per cluster so each context can learn
its own preferences. This module:

  1. Buckets resolved ledger records into clusters keyed by
     (model, symbol, interval, regime).
  2. For each cluster with ≥ ``min_samples`` resolved trades, computes
     per-factor lift = E[R | factor active] − E[R | factor inactive].
     A positive lift means "this factor contributes when present in
     this cluster"; a negative lift means "this factor is anti-signal
     in this cluster".
  3. Returns a ``ClusterWeightTable`` keyed by cluster tuple → factor
     lift dict.

The TUNER (caller) decides how aggressively to blend per-cluster lifts
into the global weight set. Default policy: only override the global
weight for a factor if the cluster has ≥ ``min_confidence_samples``
(default 30) AND |lift| ≥ ``lift_threshold`` (default 0.15R).

This is statistical, not Bayesian — but the contract matches: more
data → tighter override; sparse clusters fall back to global weights.
"""

from __future__ import annotations

from typing import Optional


ClusterKey = tuple[str, str, str, str]   # (model, symbol, interval, regime)


def cluster_records(
    records: list[dict],
    *,
    cluster_keys: tuple[str, ...] = ("model", "symbol", "interval", "regime"),
) -> dict[ClusterKey, list[dict]]:
    """Bucket resolved ledger records into clusters."""
    out: dict[ClusterKey, list[dict]] = {}
    for r in records or []:
        if r.get("outcome") in (None, "pending") or r.get("r_multiple") is None:
            continue
        key = tuple(str(r.get(k) or "unknown") for k in cluster_keys)
        out.setdefault(key, []).append(r)
    return out


def _factor_lift(records: list[dict], factor: str) -> Optional[dict]:
    """E[R | factor active] − E[R | factor inactive] for one cluster."""
    active_rs: list[float] = []
    inactive_rs: list[float] = []
    for r in records:
        try:
            rm = float(r["r_multiple"])
        except (TypeError, ValueError):
            continue
        # Factor presence can be in two shapes:
        #   1. factors_active: ["htf_bias_aligned", ...]
        #   2. contributing_factors: [{"factor": "...", "weight": 2}, ...]
        active = False
        fa = r.get("factors_active")
        if isinstance(fa, (list, tuple)):
            active = factor in fa
        if not active:
            cf = r.get("contributing_factors") or []
            for f in cf:
                if isinstance(f, dict) and f.get("factor") == factor:
                    active = True
                    break
        if active:
            active_rs.append(rm)
        else:
            inactive_rs.append(rm)
    if not active_rs:
        return None
    mean_active = sum(active_rs) / len(active_rs)
    mean_inactive = (sum(inactive_rs) / len(inactive_rs)) if inactive_rs else 0.0
    return {
        "n_active": len(active_rs),
        "n_inactive": len(inactive_rs),
        "mean_R_active": round(mean_active, 4),
        "mean_R_inactive": round(mean_inactive, 4),
        "lift": round(mean_active - mean_inactive, 4),
    }


def build_cluster_weight_table(
    records: list[dict],
    *,
    factors: list[str],
    min_samples: int = 10,
) -> dict[ClusterKey, dict]:
    """For each cluster ≥ ``min_samples``, compute per-factor lift.

    Returns:
      {(model, symbol, interval, regime): {
         "n_total": int, "mean_R": float,
         "factors": {factor_name: {n_active, n_inactive, mean_R_active,
                                     mean_R_inactive, lift}, ...},
      }}
    """
    clusters = cluster_records(records)
    out: dict[ClusterKey, dict] = {}
    for key, recs in clusters.items():
        if len(recs) < min_samples:
            continue
        mean_R = sum(float(r["r_multiple"]) for r in recs) / len(recs)
        factor_stats: dict[str, dict] = {}
        for f in factors:
            lift = _factor_lift(recs, f)
            if lift is not None:
                factor_stats[f] = lift
        out[key] = {
            "n_total": len(recs),
            "mean_R": round(mean_R, 4),
            "factors": factor_stats,
        }
    return out


def resolve_cluster_weights(
    cluster_table: dict[ClusterKey, dict],
    *,
    cluster: ClusterKey,
    base_weights: dict[str, int],
    lift_threshold: float = 0.15,
    min_confidence_samples: int = 30,
    nudge_step: int = 1,
) -> dict[str, int]:
    """Apply per-cluster overrides on top of ``base_weights``.

    Conservative blending:
      • If cluster has ≥ ``min_confidence_samples`` AND |lift| ≥ threshold,
        nudge the weight ±``nudge_step`` (cap at [−5, 5]).
      • Otherwise fall back to base_weights[factor].
    """
    out = dict(base_weights)
    stats = cluster_table.get(cluster)
    if not stats:
        return out
    n_total = int(stats.get("n_total") or 0)
    if n_total < min_confidence_samples:
        return out
    for f, lstats in (stats.get("factors") or {}).items():
        lift = float(lstats.get("lift") or 0.0)
        if abs(lift) < lift_threshold:
            continue
        cur = int(out.get(f, 0))
        if lift > 0:
            out[f] = min(cur + nudge_step, 5)
        else:
            out[f] = max(cur - nudge_step, -5)
    return out


def cluster_summary(table: dict[ClusterKey, dict]) -> list[dict]:
    """Flat list for UI rendering."""
    rows: list[dict] = []
    for key, v in sorted(table.items(), key=lambda kv: -kv[1].get("n_total", 0)):
        rows.append({
            "model": key[0], "symbol": key[1],
            "interval": key[2], "regime": key[3],
            "n_total": v.get("n_total"),
            "mean_R": v.get("mean_R"),
            "top_positive_factors": sorted(
                (v.get("factors") or {}).items(),
                key=lambda x: -float(x[1].get("lift") or 0),
            )[:3],
            "top_negative_factors": sorted(
                (v.get("factors") or {}).items(),
                key=lambda x: float(x[1].get("lift") or 0),
            )[:3],
        })
    return rows
