"""DSR-driven confluence-threshold control."""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np


_NORMAL = NormalDist()


def dsr_probability(
    sr: float,
    sr_benchmark: float,
    n_eff: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    n_eff = max(float(n_eff), 2.0)
    denom = 1.0 - float(skew) * float(sr) + ((float(kurtosis) - 1.0) / 4.0) * (float(sr) ** 2)
    denom = math.sqrt(max(denom, 1e-12))
    z = ((float(sr) - float(sr_benchmark)) * math.sqrt(n_eff - 1.0)) / denom
    return float(_NORMAL.cdf(z))


def required_sharpe_for_dsr(
    target_prob: float,
    sr_benchmark: float,
    n_eff: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    target_prob = float(np.clip(target_prob, 1e-6, 1.0 - 1e-6))
    lo, hi = -5.0, 10.0
    for _ in range(120):
        mid = (lo + hi) / 2.0
        prob = dsr_probability(mid, sr_benchmark, n_eff, skew, kurtosis)
        if prob < target_prob:
            lo = mid
        else:
            hi = mid
    return float(hi)


def update_confluence_threshold_by_dsr(
    current_threshold: float,
    base_threshold: float,
    max_threshold: float,
    current_sr: float,
    required_sr: float,
    k: float = 0.08,
    smoothing: float = 0.20,
) -> float:
    gap = max(0.0, float(required_sr) - float(current_sr))
    proposed = float(base_threshold) + float(k) * math.tanh(gap)
    proposed = float(np.clip(proposed, float(base_threshold), float(max_threshold)))
    new_threshold = (1.0 - float(smoothing)) * float(current_threshold) + float(smoothing) * proposed
    return float(np.clip(new_threshold, float(base_threshold), float(max_threshold)))
