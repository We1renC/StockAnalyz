"""Cross-model ensemble vote with conflict-aware size adjustment.

Audit fix D4. ``_pick_best_entry`` chooses the single highest-scoring
candidate across the 6 detectors. It never asks:

  > "What if sweep_reversal opens LONG and ote_retracement opens
  >  SHORT at the same bar? They're contradicting each other."

Today the system blindly takes whichever scored higher and trades full
size. This module:

  1. Collects all qualified candidates (score ≥ threshold, rr ≥ min_rr,
     not decommissioned, ...).
  2. Computes per-direction "vote weight" = Σ confluence_score for each
     side (long / short).
  3. If both sides have non-trivial vote weight, the picked entry's
     size is multiplied by ``confidence`` ∈ [size_floor, 1.0]:
       confidence = (winning_side_weight − losing_side_weight)
                    / (winning_side_weight + losing_side_weight)
     Clamped to ``size_floor`` (default 0.3) so we don't fully zero out
     a strong winner.
  4. When all qualified candidates point the same way → full size.

Returns an annotation to be merged onto the picked entry.
"""

from __future__ import annotations

from typing import Iterable, Optional


def compute_ensemble_vote(
    candidates: Iterable[dict],
    *,
    min_score: float = 8.0,
    min_rr: float = 1.5,
    size_floor: float = 0.3,
) -> dict:
    """Run the vote over ALL qualified candidates from all detectors.

    Each candidate must have ``direction`` (int) and either ``confluence``
    dict with ``score`` or ``confluence_score``.
    """
    long_weight = 0.0
    short_weight = 0.0
    n_long = 0
    n_short = 0
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        try:
            direction = int(c.get("direction") or 0)
        except (TypeError, ValueError):
            continue
        if direction == 0:
            continue
        conf = c.get("confluence") if isinstance(c.get("confluence"), dict) else {}
        try:
            score = float(conf.get("score") if conf else (c.get("confluence_score") or 0))
        except (TypeError, ValueError):
            score = 0.0
        try:
            rr = float(c.get("rr") or 0)
        except (TypeError, ValueError):
            rr = 0.0
        if score < min_score or rr < min_rr:
            continue
        if direction > 0:
            long_weight += score
            n_long += 1
        else:
            short_weight += score
            n_short += 1

    total = long_weight + short_weight
    if total <= 0:
        return {
            "size_multiplier": 1.0,
            "confidence": 0.0,
            "long_weight": 0.0, "short_weight": 0.0,
            "n_long": 0, "n_short": 0,
            "status": "no_qualified_candidates",
        }
    # If one side has zero qualified votes, the other side has full
    # confidence — no size reduction.
    if long_weight == 0 or short_weight == 0:
        return {
            "size_multiplier": 1.0,
            "confidence": 1.0,
            "long_weight": round(long_weight, 4),
            "short_weight": round(short_weight, 4),
            "n_long": n_long, "n_short": n_short,
            "status": "unanimous",
        }
    # Both sides have qualified votes → compute conflict-adjusted size.
    if long_weight >= short_weight:
        winning, losing = long_weight, short_weight
        winning_side = "long"
    else:
        winning, losing = short_weight, long_weight
        winning_side = "short"
    raw_confidence = (winning - losing) / (winning + losing)
    size_multiplier = max(float(size_floor), float(raw_confidence))
    return {
        "size_multiplier": round(size_multiplier, 4),
        "confidence": round(raw_confidence, 4),
        "winning_side": winning_side,
        "long_weight": round(long_weight, 4),
        "short_weight": round(short_weight, 4),
        "n_long": n_long, "n_short": n_short,
        "status": "conflict_adjusted",
    }


def collect_all_candidates(analysis: dict) -> list[dict]:
    """Flatten ``analysis.concepts.entry_models.<model>.entries`` into a
    single list, preserving per-entry data and adding a ``model`` field."""
    out: list[dict] = []
    em = (analysis.get("concepts") or {}).get("entry_models") or {}
    for model_key, payload in em.items():
        if not isinstance(payload, dict):
            continue
        if payload.get("decommissioned"):
            continue
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            row = dict(entry)
            row.setdefault("model", model_key)
            out.append(row)
    return out


def annotate_picked_entry_with_vote(
    picked: Optional[dict],
    analysis: dict,
    *,
    min_score: float = 8.0,
    min_rr: float = 1.5,
    size_floor: float = 0.3,
) -> Optional[dict]:
    """Mutate ``picked`` with ``ensemble_vote`` + scaled
    ``exploration_size_multiplier`` (already used by P2-14+ runner)."""
    if picked is None:
        return None
    cands = collect_all_candidates(analysis)
    vote = compute_ensemble_vote(
        cands, min_score=min_score, min_rr=min_rr, size_floor=size_floor,
    )
    picked["ensemble_vote"] = vote
    # Multiply (don't replace) any existing exploration_size_multiplier so
    # ε-greedy probe size + ensemble conflict size compose.
    prev = float(picked.get("exploration_size_multiplier") or 1.0)
    picked["exploration_size_multiplier"] = round(
        prev * float(vote.get("size_multiplier") or 1.0), 4,
    )
    return picked
