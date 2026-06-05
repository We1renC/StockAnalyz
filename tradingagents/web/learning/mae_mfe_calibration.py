"""Per-model MAE / MFE stop & target calibration.

Audit fix P2-12. Today all six entry detectors hard-code
``stop = SMC_stop ± 5%`` and ``target = entry + 2·risk·direction``. The
``mae_mfe_recommendations`` helper exists but only emits suggestions
into a UI panel — nothing actually consumes them.

This module turns the ledger into per-model calibration tables:

  • For each ``(model, direction)`` group, look at all RESOLVED winners
    (``outcome="target"`` or ``r_multiple>0``):
      - MAE (max adverse excursion) in R-units → P75 = "how deep do
        winners dip before recovering" → recommended stop multiplier
      - MFE (max favourable excursion) in R-units → P50 = "how far do
        winners actually run" → recommended target multiplier

  • The recommendation is conservative:
      - Stop never tightens beyond 1.0R (the original SMC stop is the
        structural invalidation; we only ever WIDEN if winners
        consistently breach it)
      - Target never extends beyond P90 MFE (cap at the 90th-percentile
        winner; chasing the tails over-optimises)

Usage:

    cal = build_model_calibration_table(records)
    # → {("sweep_reversal", 1): {"stop_R": 1.25, "target_R": 2.40, "n": 18}}
    apply_calibration_to_entry(entry, cal)  # mutates entry.stop / entry.target
"""

from __future__ import annotations

import math
from typing import Optional


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 1]). No numpy dep."""
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


def _key(model: Optional[str], direction) -> tuple[str, int]:
    try:
        d = int(direction)
    except (TypeError, ValueError):
        d = 0
    return (str(model or "unknown"), d)


def build_model_calibration_table(
    trade_records: list[dict],
    *,
    min_winners: int = 8,
    stop_widen_percentile: float = 0.75,
    target_take_percentile: float = 0.50,
    target_cap_percentile: float = 0.90,
) -> dict[tuple[str, int], dict]:
    """Build per-(model, direction) calibration table from RESOLVED winners.

    Returns
    -------
    {(model, direction): {
        "stop_R": float,           # recommended widen multiplier (≥1.0)
        "target_R": float,         # recommended take multiplier (capped)
        "n_winners": int,
        "p75_mae_R": float,
        "p50_mfe_R": float,
        "p90_mfe_R": float,
    }}
    """
    buckets: dict[tuple[str, int], dict[str, list[float]]] = {}
    for r in trade_records or []:
        outcome = r.get("outcome")
        rm = r.get("r_multiple")
        if outcome in (None, "pending") or rm is None:
            continue
        try:
            rm = float(rm)
        except (TypeError, ValueError):
            continue
        is_winner = (outcome == "target") or rm > 0
        if not is_winner:
            continue
        mae = r.get("mae_R") if "mae_R" in r else r.get("mae")
        mfe = r.get("mfe_R") if "mfe_R" in r else r.get("mfe")
        try:
            mae_val = abs(float(mae)) if mae is not None else None
            mfe_val = float(mfe) if mfe is not None else None
        except (TypeError, ValueError):
            continue
        if mae_val is None and mfe_val is None:
            continue
        k = _key(r.get("model"), r.get("direction"))
        bkt = buckets.setdefault(k, {"mae": [], "mfe": []})
        if mae_val is not None:
            bkt["mae"].append(mae_val)
        if mfe_val is not None:
            bkt["mfe"].append(mfe_val)

    out: dict[tuple[str, int], dict] = {}
    for k, bkt in buckets.items():
        n_winners = max(len(bkt["mae"]), len(bkt["mfe"]))
        if n_winners < min_winners:
            continue
        p75_mae = _percentile(bkt["mae"], stop_widen_percentile) if bkt["mae"] else 1.0
        p50_mfe = _percentile(bkt["mfe"], target_take_percentile) if bkt["mfe"] else 2.0
        p90_mfe = _percentile(bkt["mfe"], target_cap_percentile) if bkt["mfe"] else p50_mfe
        # Stop never tightens below 1.0R structural floor; widen if winners
        # consistently dip past it.
        stop_R = max(1.0, round(p75_mae, 3))
        # Target uses P50 winner MFE, capped by P90.
        target_R = min(round(p50_mfe, 3), round(p90_mfe, 3))
        out[k] = {
            "stop_R": stop_R,
            "target_R": float(target_R),
            "n_winners": n_winners,
            "p75_mae_R": round(p75_mae, 3),
            "p50_mfe_R": round(p50_mfe, 3),
            "p90_mfe_R": round(p90_mfe, 3),
        }
    return out


def apply_calibration_to_entry(
    entry: dict,
    calibration: dict[tuple[str, int], dict],
    *,
    base_stop_field: str = "stop",
    base_entry_field: str = "entry",
) -> dict:
    """Mutate ``entry`` with calibrated stop / target if a table exists.

    NOT applied when calibration table has no entry for (model, direction).
    Annotates the entry with ``calibration_applied`` so downstream audit
    can see what happened.
    """
    model = entry.get("model")
    direction = int(entry.get("direction") or 0)
    k = (str(model or "unknown"), direction)
    cal = calibration.get(k)
    if not cal:
        entry["calibration_applied"] = None
        return entry

    try:
        entry_px = float(entry.get(base_entry_field) or 0)
        stop_px = float(entry.get(base_stop_field) or 0)
    except (TypeError, ValueError):
        return entry
    if entry_px <= 0 or stop_px <= 0 or entry_px == stop_px:
        return entry

    risk = abs(entry_px - stop_px)
    sign = 1 if direction >= 0 else -1
    new_stop = entry_px - sign * cal["stop_R"] * risk
    new_target = entry_px + sign * cal["target_R"] * risk

    entry["original_stop"] = stop_px
    entry["original_target"] = entry.get("target")
    entry["stop"] = round(float(new_stop), 4)
    entry["target"] = round(float(new_target), 4)
    new_risk = abs(entry_px - new_stop)
    if new_risk > 0:
        entry["rr"] = round(abs(new_target - entry_px) / new_risk, 2)
    entry["calibration_applied"] = {
        "model": model, "direction": direction,
        "stop_widen_R": cal["stop_R"], "target_take_R": cal["target_R"],
        "n_winners": cal["n_winners"],
        "source": "per_model_mae_mfe",
    }
    return entry


def calibration_summary(table: dict[tuple[str, int], dict]) -> list[dict]:
    """Flat list for UI rendering."""
    out = []
    for (model, direction), v in sorted(table.items()):
        out.append({
            "model": model,
            "direction": "long" if direction >= 0 else "short",
            **v,
        })
    return out
