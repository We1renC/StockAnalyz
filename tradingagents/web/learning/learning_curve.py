"""Learning-curve diagnostics — answer "how fast is the model learning?".

Audit fix P3-20. We have a `learning_indicator` enum, ledger size, and
weight-drift count — but no curve, no rate, and no "samples-to-ready"
estimate. UI cannot tell the user "you'll meet the 30-sample threshold
in ~12 more trades at the current pace".

This module derives three signals from the ledger:

  1. CUMULATIVE LEARNING CURVE
     A list of (sample_n, cumulative_E[R], cumulative_win_rate) bins
     so the UI can plot a sparkline. Each bin = ``bin_size`` trades.

  2. LEARNING VELOCITY
     The slope of cumulative E[R] over the last K bins. Positive →
     model improving; negative → degrading; flat ≈ stagnant.

  3. SAMPLES TO NEXT READY (eta)
     If the system is in LEARNING state (ledger < 30), forecast how
     many MORE trades are needed at the current generation rate.

All three rely on the §18.2 ledger schema and respect the dedup'd
view (P0-1 keeps the sample count honest).
"""

from __future__ import annotations

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


def _resolved_records(records: list[dict]) -> list[dict]:
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


def cumulative_curve(
    records: list[dict],
    *,
    bin_size: int = 10,
    r_field: str = "r_multiple",
) -> list[dict]:
    """Bucket resolved trades into bins of ``bin_size`` and accumulate."""
    resolved = _resolved_records(records)
    # Sort by entry_time so cumulative reflects real chronology
    resolved.sort(key=lambda r: _parse_ts(r.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc))

    bins: list[dict] = []
    cum_sum_r = 0.0
    cum_wins = 0
    cum_n = 0
    bin_sum_r = 0.0
    bin_wins = 0
    bin_n = 0
    bin_start_ts: Optional[str] = None
    bin_end_ts: Optional[str] = None

    for r in resolved:
        rm = float(r[r_field])
        cum_sum_r += rm
        cum_n += 1
        bin_sum_r += rm
        bin_n += 1
        if rm > 0:
            cum_wins += 1
            bin_wins += 1
        if bin_start_ts is None:
            bin_start_ts = str(r.get("entry_time") or "")
        bin_end_ts = str(r.get("entry_time") or "")
        if bin_n >= bin_size:
            bins.append({
                "bin_idx": len(bins) + 1,
                "n_in_bin": bin_n,
                "cumulative_n": cum_n,
                "cumulative_E_R": round(cum_sum_r / cum_n, 4),
                "cumulative_win_rate": round(cum_wins / cum_n, 4),
                "bin_E_R": round(bin_sum_r / bin_n, 4),
                "bin_win_rate": round(bin_wins / bin_n, 4),
                "bin_start": bin_start_ts,
                "bin_end": bin_end_ts,
            })
            bin_sum_r = 0.0
            bin_wins = 0
            bin_n = 0
            bin_start_ts = None

    # Tail bin (incomplete)
    if bin_n > 0:
        bins.append({
            "bin_idx": len(bins) + 1,
            "n_in_bin": bin_n,
            "cumulative_n": cum_n,
            "cumulative_E_R": round(cum_sum_r / cum_n, 4),
            "cumulative_win_rate": round(cum_wins / cum_n, 4),
            "bin_E_R": round(bin_sum_r / bin_n, 4),
            "bin_win_rate": round(bin_wins / bin_n, 4),
            "bin_start": bin_start_ts,
            "bin_end": bin_end_ts,
            "is_partial": True,
        })
    return bins


def learning_velocity(
    curve: list[dict],
    *,
    lookback_bins: int = 3,
) -> dict:
    """Linear slope of cumulative_E_R over the last ``lookback_bins`` bins."""
    if len(curve) < 2:
        return {"slope": 0.0, "interpretation": "insufficient_bins",
                "lookback_bins": lookback_bins, "delta_E_R": 0.0}
    sub = curve[-lookback_bins:]
    if len(sub) < 2:
        return {"slope": 0.0, "interpretation": "insufficient_bins",
                "lookback_bins": lookback_bins, "delta_E_R": 0.0}
    # Slope by least-squares on (bin_idx, cumulative_E_R)
    xs = [float(b["bin_idx"]) for b in sub]
    ys = [float(b["cumulative_E_R"]) for b in sub]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = (num / den) if den else 0.0
    delta = sub[-1]["cumulative_E_R"] - sub[0]["cumulative_E_R"]
    if abs(slope) < 0.01:
        interp = "stagnant"
    elif slope > 0:
        interp = "improving"
    else:
        interp = "degrading"
    return {
        "slope": round(slope, 5),
        "delta_E_R": round(delta, 4),
        "interpretation": interp,
        "lookback_bins": lookback_bins,
        "first_bin_E_R": round(sub[0]["cumulative_E_R"], 4),
        "last_bin_E_R": round(sub[-1]["cumulative_E_R"], 4),
    }


def samples_to_ready(
    records: list[dict],
    *,
    target_sample_size: int = 30,
    rate_lookback_hours: float = 24.0,
) -> dict:
    """Forecast how many more (resolved) trades + ETA to reach
    ``target_sample_size``.

    Uses the trade rate of the last ``rate_lookback_hours`` to project.
    """
    resolved = _resolved_records(records)
    n_now = len(resolved)
    if n_now >= target_sample_size:
        return {
            "current": n_now, "target": target_sample_size,
            "trades_needed": 0,
            "rate_per_hour": None, "eta_hours": 0.0,
            "status": "target_reached",
        }
    # Compute trade rate from last N hours
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - rate_lookback_hours * 3600.0
    recent_count = 0
    for r in resolved:
        ts = _parse_ts(r.get("entry_time"))
        if ts and ts.timestamp() >= cutoff:
            recent_count += 1
    rate = recent_count / rate_lookback_hours if rate_lookback_hours > 0 else 0.0
    trades_needed = target_sample_size - n_now
    eta_hours = (trades_needed / rate) if rate > 0 else None
    return {
        "current": n_now,
        "target": target_sample_size,
        "trades_needed": trades_needed,
        "rate_per_hour": round(rate, 3) if rate else 0.0,
        "lookback_hours": rate_lookback_hours,
        "eta_hours": round(eta_hours, 1) if eta_hours is not None else None,
        "status": "in_progress" if rate > 0 else "stalled_no_recent_trades",
    }


def learning_curve_diagnostics(
    records: list[dict],
    *,
    bin_size: int = 10,
    velocity_lookback: int = 3,
    target_sample_size: int = 30,
) -> dict:
    """One-call summary for the UI."""
    curve = cumulative_curve(records, bin_size=bin_size)
    velocity = learning_velocity(curve, lookback_bins=velocity_lookback)
    eta = samples_to_ready(records, target_sample_size=target_sample_size)
    return {
        "bin_size": bin_size,
        "n_bins": len(curve),
        "curve": curve,
        "velocity": velocity,
        "samples_to_ready": eta,
    }
