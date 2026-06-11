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


def _welch_p_value(active: list[float], inactive: list[float]) -> Optional[float]:
    """Two-sample Welch's t-test (unequal variances). Returns two-sided p.

    No scipy: we approximate the t→p via a tail bound that's tight enough
    for the BH-FDR pre-filter we use it for. For the precise survival
    function in tail regions we use the Hill approximation of the
    Student t CDF — accurate enough for q<0.5 in our regime.
    """
    n1, n2 = len(active), len(inactive)
    if n1 < 2 or n2 < 2:
        return None
    m1 = sum(active) / n1
    m2 = sum(inactive) / n2
    v1 = sum((x - m1) ** 2 for x in active) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in inactive) / (n2 - 1)
    if v1 <= 0 and v2 <= 0:
        return 1.0 if m1 == m2 else 0.0
    se = (v1 / n1 + v2 / n2) ** 0.5
    if se <= 0:
        return 1.0 if m1 == m2 else 0.0
    t = (m1 - m2) / se
    # Welch-Satterthwaite df
    num = (v1 / n1 + v2 / n2) ** 2
    den = ((v1 / n1) ** 2 / (n1 - 1)) + ((v2 / n2) ** 2 / (n2 - 1))
    df = num / den if den > 0 else max(n1, n2) - 1
    # Survival fn of t distribution via Hill's approximation:
    #   For large df, t→N(0,1); for small df use the closed-form
    #   tail Pr(|T|>t) ≈ 2 · (1 - F_t(|t|))
    # We use a Wilson-Hilferty-style normal approximation:
    import math
    abs_t = abs(t)
    # Inverse-df correction → z that approximates the t tail
    z = abs_t * (1.0 - 1.0 / (4.0 * df)) / math.sqrt(1.0 + abs_t * abs_t / (2.0 * df))
    # Two-sided p = 2 * Pr(Z > z)
    p = math.erfc(z / math.sqrt(2.0))
    return max(0.0, min(1.0, p))


def bh_fdr_filter(
    p_values: list[float],
    *,
    alpha: float = 0.10,
) -> list[bool]:
    """Benjamini-Hochberg FDR correction.

    Returns a list of bools aligned to input p_values: True = reject null
    (factor is significant after multiple-testing correction).

    Standard BH procedure:
      1. Sort p ascending, remember original positions.
      2. Find largest k where p_(k) ≤ k/m × alpha.
      3. Reject all p_(i) for i ≤ k.
    """
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(range(m), key=lambda i: (
        float("inf") if p_values[i] is None else p_values[i]
    ))
    threshold_k = -1
    for rank, idx in enumerate(indexed, start=1):
        p = p_values[idx]
        if p is None:
            continue
        if p <= (rank / m) * alpha:
            threshold_k = rank
    out = [False] * m
    if threshold_k < 0:
        return out
    for rank, idx in enumerate(indexed, start=1):
        if rank <= threshold_k:
            out[idx] = True
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
        # Factor presence can be in three shapes:
        #   1. factors: {"htf_bias_aligned": true, ...}   ← what the §18.2
        #      ledger (build_trade_record) ACTUALLY writes. Audit fix D3:
        #      this was missing, so 100% of ledger records matched nothing
        #      → factor lift was None everywhere → cluster ensemble never
        #      learned from a single real record.
        #   2. factors_active: ["htf_bias_aligned", ...]
        #   3. contributing_factors: [{"factor": "...", "weight": 2}, ...]
        active = False
        fd = r.get("factors")
        if isinstance(fd, dict):
            active = bool(fd.get(factor))
        if not active:
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
    # Audit fix D1: emit a raw p-value alongside the lift so the table
    # builder can BH-FDR correct over the full cluster × factor matrix.
    p_raw = _welch_p_value(active_rs, inactive_rs)
    return {
        "n_active": len(active_rs),
        "n_inactive": len(inactive_rs),
        "mean_R_active": round(mean_active, 4),
        "mean_R_inactive": round(mean_inactive, 4),
        "lift": round(mean_active - mean_inactive, 4),
        "p_value": (None if p_raw is None else round(float(p_raw), 6)),
    }


def build_cluster_weight_table(
    records: list[dict],
    *,
    factors: list[str],
    min_samples: int = 10,
    fdr_alpha: float = 0.10,
) -> dict[ClusterKey, dict]:
    """For each cluster ≥ ``min_samples``, compute per-factor lift +
    BH-FDR-corrected significance.

    Audit fix D1: with 16 clusters × 12 factors = 192 hypotheses,
    raw p<0.05 yields ~10 false positives. We run Benjamini-Hochberg
    over the full cluster × factor p-value vector at ``fdr_alpha``
    (default 0.10 — slightly looser than typical 0.05 because R-multiple
    distributions are heavy-tailed and we want some recall).

    Each factor entry gains:
      • ``p_value``: raw Welch p
      • ``fdr_significant``: bool after BH correction across the
        whole table
    """
    clusters = cluster_records(records)
    raw_table: dict[ClusterKey, dict] = {}
    # First pass: compute lifts + raw p-values
    for key, recs in clusters.items():
        if len(recs) < min_samples:
            continue
        mean_R = sum(float(r["r_multiple"]) for r in recs) / len(recs)
        factor_stats: dict[str, dict] = {}
        for f in factors:
            lift = _factor_lift(recs, f)
            if lift is not None:
                factor_stats[f] = lift
        raw_table[key] = {
            "n_total": len(recs),
            "mean_R": round(mean_R, 4),
            "factors": factor_stats,
        }
    # Second pass: collect all (cluster, factor) p-values and BH-correct
    addresses: list[tuple] = []
    p_values: list[float] = []
    for key, payload in raw_table.items():
        for fname, stats in (payload.get("factors") or {}).items():
            addresses.append((key, fname))
            p_values.append(stats.get("p_value"))
    significance = bh_fdr_filter(p_values, alpha=fdr_alpha)
    for (key, fname), sig in zip(addresses, significance):
        raw_table[key]["factors"][fname]["fdr_significant"] = bool(sig)
    return raw_table


def resolve_cluster_weights(
    cluster_table: dict[ClusterKey, dict],
    *,
    cluster: ClusterKey,
    base_weights: dict[str, int],
    lift_threshold: float = 0.15,
    min_confidence_samples: int = 30,
    nudge_step: int = 1,
    require_fdr_significant: bool = True,
) -> dict[str, int]:
    """Apply per-cluster overrides on top of ``base_weights``.

    Conservative blending (audit fix D1 adds the FDR gate):
      • cluster must have ≥ ``min_confidence_samples`` rows
      • |lift| ≥ ``lift_threshold``
      • factor must be FDR-significant after BH correction across the
        full cluster × factor table (skipped when
        ``require_fdr_significant=False`` for back-compat)
      • Otherwise → keep base_weights[factor] as-is.
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
        if require_fdr_significant and not bool(lstats.get("fdr_significant")):
            # Pre-D1 callers (tests / legacy code) that pass tables
            # without fdr_significant just see no nudge — safe default.
            if "fdr_significant" in lstats:
                continue
            # If the key is missing entirely we treat it as legacy
            # and let the nudge through (back-compat).
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
