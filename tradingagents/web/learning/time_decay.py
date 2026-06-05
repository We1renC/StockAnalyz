"""Exponential time-decay weighting for ledger learning.

Audit fix P3-21. Today every trade in the ledger contributes equally
to expectancy / weight calibration. A year-old trade and yesterday's
trade carry the same weight — but in a non-stationary market (regime
shifts) the stale data drags the model away from current reality.

Solution: weight each trade by ``exp(-(now - entry_time) / half_life)``.
With half_life = 30 days, a trade from 30d ago counts 50%, 60d → 25%,
90d → 12.5%. This is the "Bayesian forgetting" trick from
robotics / online learning.

Public API:
  • compute_decay_weights(records, half_life_days=30, now=None)
      → returns list of weights aligned to input records
  • weighted_expectancy(records, **kw) → time-weighted E[R]
  • effective_sample_size(weights) → Kish ESS for variance correction
  • split_active_vs_stale(records, half_life_days, decay_threshold=0.1)
      → trades whose weight < threshold are "effectively forgotten"
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
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


def compute_decay_weights(
    records: list[dict],
    *,
    half_life_days: float = 30.0,
    now: Optional[datetime] = None,
    time_field: str = "entry_time",
) -> list[float]:
    """Return one weight per record using exp(-age / half_life)·ln(2).

    Records without a parseable timestamp get weight = 1.0 (neutral —
    don't punish, but don't reward).
    """
    if not records:
        return []
    if half_life_days <= 0:
        return [1.0] * len(records)
    ref = now or datetime.now(timezone.utc)
    decay_constant = math.log(2) / (float(half_life_days) * 86_400.0)
    weights: list[float] = []
    for r in records:
        ts = _parse_ts(r.get(time_field))
        if ts is None:
            weights.append(1.0)
            continue
        age_s = max(0.0, (ref - ts).total_seconds())
        w = math.exp(-decay_constant * age_s)
        weights.append(round(w, 6))
    return weights


def weighted_expectancy(
    records: list[dict],
    *,
    half_life_days: float = 30.0,
    now: Optional[datetime] = None,
    r_field: str = "r_multiple",
) -> dict:
    """Time-weighted E[R] + ESS.

    Pending trades (no r_multiple or outcome == "pending") are skipped.
    """
    valid_records = []
    for r in records or []:
        outcome = r.get("outcome")
        rm = r.get(r_field)
        if outcome in (None, "pending") or rm is None:
            continue
        try:
            float(rm)
        except (TypeError, ValueError):
            continue
        valid_records.append(r)
    if not valid_records:
        return {
            "n": 0, "weighted_expectancy": 0.0,
            "effective_sample_size": 0.0,
            "naive_expectancy": 0.0,
            "half_life_days": half_life_days,
        }
    weights = compute_decay_weights(
        valid_records, half_life_days=half_life_days, now=now,
    )
    total_w = sum(weights)
    if total_w <= 0:
        return {
            "n": len(valid_records), "weighted_expectancy": 0.0,
            "effective_sample_size": 0.0,
            "naive_expectancy": 0.0,
            "half_life_days": half_life_days,
        }
    weighted_sum = sum(w * float(r[r_field]) for w, r in zip(weights, valid_records))
    naive_sum = sum(float(r[r_field]) for r in valid_records)
    ess = effective_sample_size(weights)
    return {
        "n": len(valid_records),
        "weighted_expectancy": round(weighted_sum / total_w, 4),
        "naive_expectancy": round(naive_sum / len(valid_records), 4),
        "effective_sample_size": round(ess, 2),
        "half_life_days": half_life_days,
        "total_weight": round(total_w, 4),
    }


def effective_sample_size(weights: list[float]) -> float:
    """Kish's effective sample size:  (Σw)² / Σ(w²).

    With uniform weights ESS = N. With heavily skewed weights ESS ≪ N
    — used to warn "your 200 trades effectively only contain 25 trades
    of fresh information".
    """
    if not weights:
        return 0.0
    s1 = sum(weights)
    s2 = sum(w * w for w in weights)
    if s2 <= 0:
        return 0.0
    return (s1 * s1) / s2


def split_active_vs_stale(
    records: list[dict],
    *,
    half_life_days: float = 30.0,
    decay_threshold: float = 0.10,
    now: Optional[datetime] = None,
) -> dict:
    """Split records into "still influencing learning" vs "effectively
    forgotten". Threshold 0.10 ≈ trade older than ~3.3 half-lives."""
    weights = compute_decay_weights(
        records, half_life_days=half_life_days, now=now,
    )
    active = []
    stale = []
    for r, w in zip(records or [], weights):
        if w >= decay_threshold:
            active.append({"weight": w, **r})
        else:
            stale.append({"weight": w, **r})
    return {
        "active": active,
        "stale": stale,
        "active_count": len(active),
        "stale_count": len(stale),
        "decay_threshold": decay_threshold,
        "half_life_days": half_life_days,
    }
