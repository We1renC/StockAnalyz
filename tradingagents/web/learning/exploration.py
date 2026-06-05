"""ε-greedy exploration for boundary entry discovery.

Audit fix P2-14. Once the system is READY (5 gates green) it stops
considering candidates below ``min_confluence_score``. That's correct
exploitation, but it prevents the model from ever learning whether
``score = min_score - 1`` would have worked — every "rejected" sample
is unobserved. Without boundary data the score threshold drifts away
from optimal.

Solution: with probability ε per tick (default 5%), allow ONE
sub-threshold candidate (score ≥ min_score - 2) through with a
MUCH smaller position size (``exploration_size_multiplier``, default
0.2 × normal). Tag the trade ``source="exploration"`` so attribution
can isolate exploration P&L from exploitation P&L.

Anti-abuse:
  • ε never triggered when system in LEARNING/VALIDATING/PAUSED
  • ε exploration size capped at ``exploration_size_multiplier`` × normal
  • Per-symbol exploration_count tracked; once we have enough boundary
    samples (≥20), ε halves automatically

This is the missing "actively sample the boundary" signal — without
it the score → win_rate calibration (P2-11) only learns at the
operating point, never the alternatives.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional


_DEFAULT_EPSILON = 0.05
_BOUNDARY_OFFSET = 2          # try score within [min - BOUNDARY_OFFSET, min)
_MIN_RR = 1.2                  # exploration still requires sane RR
_SIZE_MULTIPLIER = 0.20        # 20% of normal size
_ENOUGH_BOUNDARY_SAMPLES = 20  # half ε once we hit this
_EXPLORATION_FORBIDDEN_STATES = frozenset(
    {"LEARNING", "VALIDATING_PROBE", "PAUSED", "LOCKED", "DRY_RUN"}
)


@dataclass
class ExplorationDecision:
    chosen_entry: Optional[dict]
    is_exploration: bool
    reason: str
    epsilon_used: float
    size_multiplier: float
    boundary_sample_count: int


def _deterministic_random(seed: str) -> float:
    """SHA-1 → [0, 1). Deterministic per (symbol, tick) so tests are stable
    yet behaviour varies across symbols / ticks."""
    h = hashlib.sha1(seed.encode()).digest()
    n = int.from_bytes(h[:8], "big")
    return (n % 10_000_000) / 10_000_000.0


def _boundary_candidate(
    all_entries: list[dict],
    min_score: int,
    min_rr: float = _MIN_RR,
) -> Optional[dict]:
    """Pick the highest-scoring entry whose score is in the boundary band."""
    band_lo = max(0, min_score - _BOUNDARY_OFFSET)
    band_hi = min_score   # half-open: score < min_score
    candidates = []
    for e in all_entries or []:
        s = (e.get("confluence") or {}).get("score") or 0
        if not (band_lo <= s < band_hi):
            continue
        if (e.get("rr") or 0) < min_rr:
            continue
        if e.get("dol_required") and not e.get("dol_target"):
            continue
        candidates.append(e)
    if not candidates:
        return None
    candidates.sort(
        key=lambda e: ((e.get("confluence") or {}).get("score", 0), e.get("rr", 0)),
        reverse=True,
    )
    return candidates[0]


def decide_exploration(
    *,
    all_entries: list[dict],
    min_confluence_score: int,
    state: str,
    symbol: str,
    boundary_sample_count: int,
    base_epsilon: float = _DEFAULT_EPSILON,
    rng_seed: Optional[str] = None,
) -> ExplorationDecision:
    """Decide whether THIS tick should fire an exploration probe.

    Returns ``ExplorationDecision`` with:
      • ``chosen_entry``: the sub-threshold candidate to fire, or None
      • ``is_exploration``: True only when we should fire it
      • ``size_multiplier``: caller multiplies normal position size by this
    """
    # Hard guards: never explore outside READY / TRADING
    if state in _EXPLORATION_FORBIDDEN_STATES:
        return ExplorationDecision(
            chosen_entry=None, is_exploration=False,
            reason=f"state_blocks_exploration:{state}",
            epsilon_used=0.0, size_multiplier=0.0,
            boundary_sample_count=boundary_sample_count,
        )

    # Effective ε halves once we have enough boundary samples
    epsilon = base_epsilon
    if boundary_sample_count >= _ENOUGH_BOUNDARY_SAMPLES:
        epsilon = base_epsilon * 0.5

    # Deterministic random per (symbol, current minute) — gives stable
    # behaviour within the same tick but varies across ticks.
    seed = rng_seed or f"{symbol}:{int(time.time() // 60)}"
    if _deterministic_random(seed) >= epsilon:
        return ExplorationDecision(
            chosen_entry=None, is_exploration=False,
            reason="epsilon_not_triggered",
            epsilon_used=epsilon, size_multiplier=0.0,
            boundary_sample_count=boundary_sample_count,
        )

    candidate = _boundary_candidate(all_entries, min_confluence_score)
    if candidate is None:
        return ExplorationDecision(
            chosen_entry=None, is_exploration=False,
            reason="no_boundary_candidate_available",
            epsilon_used=epsilon, size_multiplier=0.0,
            boundary_sample_count=boundary_sample_count,
        )

    # Stamp the exploration flag onto a copy so caller knows not to
    # treat this as a regular trade in attribution.
    enriched = dict(candidate)
    enriched["is_exploration"] = True
    enriched["exploration_size_multiplier"] = _SIZE_MULTIPLIER
    enriched["exploration_band"] = [
        max(0, min_confluence_score - _BOUNDARY_OFFSET),
        min_confluence_score,
    ]
    # Note: we intentionally do NOT bump triggered=True on exploration —
    # caller must respect the small-size path so risk pipeline sees the
    # explicit override (smaller risk_pct via the multiplier).

    return ExplorationDecision(
        chosen_entry=enriched, is_exploration=True,
        reason="epsilon_triggered_boundary_probe",
        epsilon_used=epsilon, size_multiplier=_SIZE_MULTIPLIER,
        boundary_sample_count=boundary_sample_count,
    )


def count_exploration_trades(
    trade_records: list[dict],
    *,
    symbol: Optional[str] = None,
) -> int:
    """How many boundary samples we've already collected for this symbol."""
    n = 0
    for r in trade_records or []:
        if symbol and r.get("symbol") != symbol:
            continue
        if r.get("source") == "exploration" or r.get("is_exploration"):
            n += 1
    return n
