"""Confluence-score → calibrated win-rate mapping.

Audit fix P2-11. Previously ``min_confluence_score=8`` was a magic
number — nobody knew what real win-rate it corresponded to. This module
fits a monotone calibration from observed (score, win) pairs in the
ledger so we can:

  • show users "score 8 ≈ 53% win rate" instead of "score 8"
  • derive ``min_confluence_score`` from a *target* win-rate
    (e.g. "I want ≥55% win rate → use score ≥ 9")
  • surface miscalibration ("score 10 historically wins LESS than
    score 8" → factor weights need re-fitting)

Three calibration methods available:
  • bucket  — non-parametric histogram (default; fast; n>30 per bucket)
  • isotonic — monotone-increasing fit (best for >100 samples)
  • logistic — parametric logistic regression (smooth, allows extrapolation)

All three return the same shape:
  {
    "method": str,
    "n_samples": int,
    "table": [{"score": int, "win_rate": float, "n": int}, ...],
    "predict": callable(score) -> win_rate,
    "min_score_for_target": callable(target_win_rate) -> int,
  }
"""

from __future__ import annotations

import math
from typing import Callable, Optional


def _bucket_calibration(pairs: list[tuple[int, int]]) -> dict:
    """Non-parametric histogram per score bucket. No assumptions, fast."""
    if not pairs:
        return {"method": "bucket", "n_samples": 0, "table": []}
    by_score: dict[int, list[int]] = {}
    for score, won in pairs:
        by_score.setdefault(int(score), []).append(int(won))
    table = []
    for score in sorted(by_score.keys()):
        wins = by_score[score]
        n = len(wins)
        wr = sum(wins) / n if n else 0.0
        table.append({"score": score, "win_rate": round(wr, 4), "n": n})
    return {"method": "bucket", "n_samples": len(pairs), "table": table}


def _isotonic_calibration(pairs: list[tuple[int, int]]) -> dict:
    """Pool Adjacent Violators isotonic regression — guarantees monotone
    increasing win_rate as score increases. Industry standard for
    binary classification calibration."""
    base = _bucket_calibration(pairs)
    if not base["table"]:
        return {"method": "isotonic", "n_samples": 0, "table": []}
    # Run PAV on the bucket means weighted by bucket size
    rows = sorted(base["table"], key=lambda r: r["score"])
    means = [r["win_rate"] for r in rows]
    weights = [r["n"] for r in rows]
    # Pool Adjacent Violators
    i = 0
    while i < len(means) - 1:
        if means[i] > means[i + 1]:
            # Pool i and i+1
            total_w = weights[i] + weights[i + 1]
            pooled = (means[i] * weights[i] + means[i + 1] * weights[i + 1]) / total_w
            means[i] = pooled
            weights[i] = total_w
            means.pop(i + 1)
            weights.pop(i + 1)
            rows[i] = {**rows[i], "win_rate": round(pooled, 4),
                        "n": total_w, "pooled_into": rows.pop(i + 1)["score"]}
            i = max(0, i - 1)
        else:
            i += 1
    for j, r in enumerate(rows):
        r["win_rate"] = round(means[j], 4)
        r["n"] = weights[j]
    return {"method": "isotonic", "n_samples": len(pairs), "table": rows}


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logistic_calibration(pairs: list[tuple[int, int]]) -> dict:
    """Single-variable logistic regression: P(win | score) = σ(a + b·score).
    Fit by Newton-Raphson, no external dep."""
    if not pairs:
        return {"method": "logistic", "n_samples": 0, "table": [],
                "coefficients": {"a": 0.0, "b": 0.0}}
    a, b = 0.0, 0.0
    for _ in range(40):  # ≤40 Newton steps is plenty for 1D logistic
        g_a, g_b = 0.0, 0.0
        h_aa, h_ab, h_bb = 0.0, 0.0, 0.0
        for score, won in pairs:
            x = float(score)
            p = _sigmoid(a + b * x)
            err = won - p
            g_a += err
            g_b += err * x
            w = p * (1 - p)
            h_aa -= w
            h_ab -= w * x
            h_bb -= w * x * x
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        # Newton step: θ -= H⁻¹·g (note: gradient sign with negative Hessian)
        delta_a = (h_bb * g_a - h_ab * g_b) / det
        delta_b = (h_aa * g_b - h_ab * g_a) / det
        a -= delta_a
        b -= delta_b
        if abs(delta_a) + abs(delta_b) < 1e-7:
            break
    scores_sorted = sorted({int(s) for s, _ in pairs})
    table = [{"score": s, "win_rate": round(_sigmoid(a + b * s), 4),
              "n": sum(1 for ss, _ in pairs if ss == s)} for s in scores_sorted]
    return {"method": "logistic", "n_samples": len(pairs), "table": table,
            "coefficients": {"a": round(a, 5), "b": round(b, 5)}}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calibrate_score_to_winrate(
    trade_records: list[dict],
    *,
    method: str = "isotonic",
    score_field: str = "confluence_score",
) -> dict:
    """Fit a score→win_rate calibration from ledger.

    Returns dict with ``predict(score)`` and ``min_score_for_target(target)``
    closures attached so callers can use them inline.
    """
    pairs: list[tuple[int, int]] = []
    for r in trade_records or []:
        score = r.get(score_field)
        outcome = r.get("outcome")
        r_mult = r.get("r_multiple")
        # Treat target (and any other positive R-multiple) as a win
        if outcome in (None, "pending"):
            continue
        if score is None:
            continue
        try:
            won = 1 if (outcome == "target" or (r_mult is not None and float(r_mult) > 0)) else 0
            pairs.append((int(round(float(score))), won))
        except (TypeError, ValueError):
            continue
    if method == "bucket":
        out = _bucket_calibration(pairs)
    elif method == "logistic":
        out = _logistic_calibration(pairs)
    else:
        out = _isotonic_calibration(pairs)

    table = out["table"]

    def predict(score: float) -> Optional[float]:
        if not table:
            return None
        score = int(round(float(score)))
        # Exact match
        for r in table:
            if r["score"] == score:
                return r["win_rate"]
        # Interpolate/extrapolate
        below = [r for r in table if r["score"] <= score]
        above = [r for r in table if r["score"] >= score]
        if not below:
            return table[0]["win_rate"]
        if not above:
            return table[-1]["win_rate"]
        lo, hi = below[-1], above[0]
        if hi["score"] == lo["score"]:
            return lo["win_rate"]
        # Linear interp
        frac = (score - lo["score"]) / (hi["score"] - lo["score"])
        return round(lo["win_rate"] + frac * (hi["win_rate"] - lo["win_rate"]), 4)

    def min_score_for_target(target_win_rate: float) -> Optional[int]:
        """Return the lowest integer score whose calibrated win_rate ≥ target."""
        if not table:
            return None
        for r in sorted(table, key=lambda x: x["score"]):
            if r["win_rate"] >= float(target_win_rate):
                return r["score"]
        return None  # No score meets target

    out["predict"] = predict
    out["min_score_for_target"] = min_score_for_target
    return out


def calibration_diagnostics(
    trade_records: list[dict],
    *,
    target_win_rates: list[float] = (0.50, 0.55, 0.60, 0.65),
) -> dict:
    """One-call summary for the UI / audit panel:

    {
      "n_samples": int,
      "calibration_table": [{score, win_rate, n}],
      "recommendations": {
        "target_55pct_min_score": int,
        "target_60pct_min_score": int,
        ...
      },
      "miscalibration_alerts": [{score, expected, actual}],
    }
    """
    cal = calibrate_score_to_winrate(trade_records, method="isotonic")
    table = cal["table"]
    recs: dict[str, Optional[int]] = {}
    for t in target_win_rates:
        recs[f"target_{int(t*100)}pct_min_score"] = cal["min_score_for_target"](t)
    # Miscalibration: any score where win_rate < lower-score's win_rate
    # (PAV should have pooled these but bucket fit might show them)
    bucket = calibrate_score_to_winrate(trade_records, method="bucket")
    alerts = []
    prev = None
    for r in bucket["table"]:
        if prev is not None and r["win_rate"] < prev["win_rate"] - 0.05:
            alerts.append({
                "score": r["score"],
                "actual_win_rate": r["win_rate"],
                "expected_higher_than": prev["score"],
                "expected_at_least": prev["win_rate"],
            })
        prev = r
    return {
        "n_samples": cal["n_samples"],
        "calibration_table": table,
        "recommendations": recs,
        "miscalibration_alerts": alerts,
        "method": cal["method"],
    }
