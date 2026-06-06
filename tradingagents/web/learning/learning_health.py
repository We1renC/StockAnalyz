"""Aggregated learning-loop health score (0-100).

Audit fix C1. We have learning_curve, velocity, real_pnl_gates,
edge_decay, calibration five separate panels. Operators have no single
"is the learning loop healthy?" red/green light. This module folds them
all into one score plus a status label.

Score buckets (each 0-25 points, total 100):
  • SAMPLES (25) — current resolved ledger size vs target_sample_size
    (capped at 25; partial credit linear from 0 to target)
  • PNL_HEALTH (25) — real_pnl_gates all pass → 25; each fail −8
  • VELOCITY (25) — learning velocity interpretation:
        improving → 25, stagnant → 12, degrading → 0, insufficient → 12
  • EDGE_DECAY (25) — kill_switch state:
        READY → 25, VALIDATING_PROBE → 15, ELEVATED → 8, LOCKED → 0

Status label:
  ≥80 healthy / 60-79 watch / 40-59 degraded / <40 critical
"""

from __future__ import annotations

from typing import Optional


def _samples_score(n_resolved: int, target: int = 30) -> int:
    if n_resolved <= 0:
        return 0
    if n_resolved >= target:
        return 25
    return int(round(25.0 * n_resolved / target))


def _pnl_score(gates: dict) -> tuple[int, list[str]]:
    """gates is the dict from run_real_pnl_gates."""
    if not gates or "gates" not in gates:
        return 12, ["no_data"]
    fails = list(gates.get("failures") or [])
    score = 25 - 8 * len(fails)
    return max(0, score), fails


def _velocity_score(velocity: dict) -> int:
    interp = (velocity or {}).get("interpretation") or "insufficient_bins"
    return {
        "improving": 25,
        "stagnant": 12,
        "insufficient_bins": 12,
        "degrading": 0,
    }.get(interp, 12)


def _decay_score(kill_state: Optional[str]) -> int:
    s = (kill_state or "READY").upper()
    return {
        "READY": 25,
        "VALIDATING_PROBE": 15,
        "ELEVATED": 8,
        "LOCKED": 0,
    }.get(s, 12)


def _status_label(score: int) -> str:
    if score >= 80:
        return "healthy"
    if score >= 60:
        return "watch"
    if score >= 40:
        return "degraded"
    return "critical"


def compute_learning_health(
    *,
    records: list[dict],
    kill_switch_state: Optional[str] = None,
    target_sample_size: int = 30,
    bin_size: int = 10,
) -> dict:
    """Return the aggregated health card."""
    from learning.learning_curve import learning_curve_diagnostics
    from learning.real_pnl_gates import run_real_pnl_gates

    diagnostics = learning_curve_diagnostics(
        records, bin_size=bin_size,
        target_sample_size=target_sample_size,
    )
    pnl = run_real_pnl_gates(records)

    n_resolved = diagnostics["samples_to_ready"]["current"]

    samples = _samples_score(n_resolved, target_sample_size)
    pnl_pts, pnl_fails = _pnl_score(pnl)
    velocity_pts = _velocity_score(diagnostics.get("velocity") or {})
    decay_pts = _decay_score(kill_switch_state)
    total = samples + pnl_pts + velocity_pts + decay_pts
    return {
        "score": int(total),
        "max_score": 100,
        "status": _status_label(int(total)),
        "components": {
            "samples": {"score": samples, "max": 25, "n_resolved": n_resolved,
                          "target": target_sample_size},
            "pnl_health": {"score": pnl_pts, "max": 25,
                            "failures": pnl_fails,
                            "all_passed": pnl.get("all_passed")},
            "velocity": {"score": velocity_pts, "max": 25,
                          "interpretation": (diagnostics.get("velocity")
                                              or {}).get("interpretation"),
                          "slope": (diagnostics.get("velocity") or {}).get("slope")},
            "edge_decay": {"score": decay_pts, "max": 25,
                            "kill_switch_state": (kill_switch_state or "READY")},
        },
    }
