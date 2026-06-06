"""Per-model decommission gate.

Audit fix D3. ``edge_decay_check`` demotes the whole strategy
state to VALIDATING_PROBE; that's a coarse lever. The system has no
way to say "sweep_reversal on BTC-1h is dead but ote_retracement is
fine — disable just the first detector".

This module:
  • Computes per-(model, symbol, interval) trailing performance from
    the ledger (rolling window of N most-recent resolved trades).
  • Decommissions if the window's total R drops below
    ``min_total_R`` AND n ≥ ``min_samples``.
  • Auto-revives after ``cooldown_days`` if the rolling window since
    that point recovers above ``revive_total_R``.

State persists to ``<ledger_dir>/decommissioned.json`` so the runner
can read it on each tick and skip the entry list returned by that
detector.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


def _resolved(records: list[dict]) -> list[dict]:
    out = []
    for r in records or []:
        if r.get("outcome") in (None, "pending"):
            continue
        if r.get("r_multiple") is None:
            continue
        out.append(r)
    return out


def _trailing_window(records: list[dict], n: int) -> list[dict]:
    """Most-recent N records by entry_time."""
    out = sorted(
        records,
        key=lambda r: str(r.get("entry_time") or ""),
    )
    return out[-n:]


def compute_per_model_health(
    records: list[dict],
    *,
    window_size: int = 50,
    min_samples: int = 20,
    cluster_keys: tuple[str, ...] = ("model", "symbol", "interval"),
) -> dict[tuple, dict]:
    """Return per-cluster trailing window stats.

    {
      ("sweep_reversal", "BTC-USDT", "1h"): {
        "n": int, "n_in_window": int, "total_R": float,
        "mean_R": float, "win_rate": float,
        "first_in_window": ts, "last_in_window": ts,
      }
    }
    """
    resolved = _resolved(records)
    buckets: dict[tuple, list[dict]] = {}
    for r in resolved:
        key = tuple(str(r.get(k) or "unknown") for k in cluster_keys)
        buckets.setdefault(key, []).append(r)
    out: dict[tuple, dict] = {}
    for key, recs in buckets.items():
        window = _trailing_window(recs, window_size)
        rs = [float(r["r_multiple"]) for r in window]
        wins = sum(1 for x in rs if x > 0)
        out[key] = {
            "n": len(recs),
            "n_in_window": len(window),
            "total_R": round(sum(rs), 4),
            "mean_R": round((sum(rs) / len(rs)) if rs else 0.0, 4),
            "win_rate": round(wins / len(rs), 4) if rs else 0.0,
            "first_in_window": (str(window[0].get("entry_time")) if window else None),
            "last_in_window": (str(window[-1].get("entry_time")) if window else None),
            "eligible": len(window) >= min_samples,
        }
    return out


def decide_decommission(
    health: dict[tuple, dict],
    state: dict,
    *,
    min_total_R: float = -5.0,
    revive_total_R: float = 1.0,
    cooldown_days: int = 7,
    now: Optional[datetime] = None,
) -> dict:
    """Given current health + persisted state, decide actions.

    Returns ``{"new_state": dict, "actions": [...]}``:
      • new_state — dict keyed by ``"<model>|<symbol>|<interval>"`` →
        {status: "active"|"decommissioned", ts, total_R, n_in_window}
      • actions — human-readable strings: "decommissioned X", "revived Y"
    """
    now = now or datetime.now(timezone.utc)
    new_state = dict(state or {})
    actions: list[str] = []
    for key, stats in health.items():
        skey = "|".join(str(x) for x in key)
        if not stats.get("eligible"):
            continue
        prev = new_state.get(skey) or {}
        prev_status = prev.get("status", "active")
        total_R = float(stats.get("total_R") or 0.0)
        if prev_status == "active":
            if total_R <= min_total_R:
                new_state[skey] = {
                    "status": "decommissioned",
                    "ts": now.isoformat(timespec="seconds"),
                    "total_R": total_R,
                    "n_in_window": stats["n_in_window"],
                    "reason": f"trailing_total_R={total_R}<={min_total_R}",
                }
                actions.append(f"decommissioned {skey} (total_R={total_R})")
        else:
            # Currently decommissioned — check cooldown + revive criterion
            ts = prev.get("ts")
            try:
                ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            except Exception:
                ts_dt = now - timedelta(days=cooldown_days + 1)
            elapsed_days = (now - ts_dt).total_seconds() / 86400.0
            if elapsed_days < cooldown_days:
                continue
            if total_R >= revive_total_R:
                new_state[skey] = {
                    "status": "active",
                    "ts": now.isoformat(timespec="seconds"),
                    "total_R": total_R,
                    "n_in_window": stats["n_in_window"],
                    "reason": f"recovered_total_R={total_R}>={revive_total_R}",
                }
                actions.append(f"revived {skey} (total_R={total_R})")
    return {"new_state": new_state, "actions": actions}


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_state(path: str, state: dict) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def is_decommissioned(
    state: dict,
    *,
    model: str,
    symbol: str,
    interval: str,
) -> bool:
    """Lookup helper for the runner."""
    key = f"{model}|{symbol}|{interval}"
    return (state or {}).get(key, {}).get("status") == "decommissioned"


def apply_decommission_to_analysis(
    analysis: dict,
    state: dict,
    *,
    symbol: str,
    interval: str,
) -> dict:
    """Mutate ``analysis["concepts"]["entry_models"]`` to drop decommissioned
    detectors. Returns the same analysis for chaining.

    Each model that is decommissioned has its ``entries`` list cleared
    and a ``decommissioned: True`` marker added so the UI can show a
    badge.
    """
    em = (analysis.get("concepts") or {}).get("entry_models") or {}
    for model_key, payload in em.items():
        if not isinstance(payload, dict):
            continue
        if is_decommissioned(state, model=model_key, symbol=symbol, interval=interval):
            payload["entries"] = []
            payload["decommissioned"] = True
            payload["decommissioned_reason"] = (
                state.get(f"{model_key}|{symbol}|{interval}", {}).get("reason")
            )
    return analysis
