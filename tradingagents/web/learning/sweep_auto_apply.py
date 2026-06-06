"""Auto-apply hyperparameter sweep recommendations to strategy.yaml.

Audit fix B1. ``sweep_hyperparameters`` (P3-18) emits a recommendation,
but nothing wires it back into the live config — a human has to eyeball
the endpoint and hand-patch profile.yaml. Defeats the purpose of having
a learning loop.

This module:
  • Loads the current ``profile.yaml`` (or whatever path the caller
    points at) and reads the current ``min_score`` / ``min_rr`` /
    ``risk_pct`` as the comparison anchor.
  • Runs ``sweep_walk_forward`` first; falls back to
    ``sweep_hyperparameters`` only when OOS data is too sparse.
  • Applies ``should_apply_recommendation`` to the selected sweep mode.
  • If apply=True AND last-apply timestamp is older than
    ``min_days_since_last_apply`` (default 30), writes the new values
    back to profile.yaml and stamps a ``last_auto_apply`` audit field.
  • Always emits an Obsidian markdown audit note regardless of whether
    we applied — so users see WHY we didn't apply (delta too small,
    last-apply too recent, ...).

Idempotent: re-running within the cooldown is a no-op.
Safe-by-default: if anything goes wrong reading/writing yaml, return
with applied=False and a reason field. Never partial-writes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


def _read_yaml(path: str) -> dict:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _write_yaml(path: str, data: dict) -> bool:
    try:
        import yaml
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _last_apply_age_days(profile: dict, now: Optional[datetime] = None) -> Optional[float]:
    last = profile.get("last_auto_apply") or {}
    ts = last.get("ts") if isinstance(last, dict) else None
    if not ts:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        last_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now - last_dt).total_seconds() / 86400.0
    except Exception:
        return None


def _write_obsidian_note(vault: str, *, applied: bool, reason: str,
                          before: dict, after: dict, sweep: dict,
                          ts: str) -> None:
    try:
        notes_dir = Path(vault) / "SMC" / "HyperparameterSweep"
        notes_dir.mkdir(parents=True, exist_ok=True)
        fp = notes_dir / f"{ts.replace(':', '-')}.md"
        body = (
            f"---\n"
            f"title: Hyperparameter Sweep {ts}\n"
            f"tags: [smc, hyperparameter_sweep, audit]\n"
            f"applied: {applied}\n"
            f"reason: {reason}\n"
            f"---\n\n"
            f"## Before\n```yaml\n"
            f"min_score: {before.get('min_score')}\n"
            f"min_rr: {before.get('min_rr')}\n"
            f"risk_pct: {before.get('risk_pct')}\n```\n\n"
            f"## After\n```yaml\n"
            f"min_score: {after.get('min_score')}\n"
            f"min_rr: {after.get('min_rr')}\n"
            f"risk_pct: {after.get('risk_pct')}\n```\n\n"
            f"## Sweep status\n- status: {sweep.get('status')}\n"
            f"- n_records: {sweep.get('n_records')}\n"
            f"- n_valid_candidates: {sweep.get('n_valid_candidates')}\n"
        )
        fp.write_text(body, encoding="utf-8")
    except Exception:
        pass


def auto_apply_sweep(
    *,
    records: list[dict],
    profile_path: str,
    min_days_since_last_apply: int = 30,
    min_sharpe_improvement: float = 0.1,
    min_sharpe_absolute: float = 0.2,
    obsidian_vault: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Run sweep, decide, optionally write back. See module docstring."""
    from learning.hyperparameter_sweep import (
        sweep_hyperparameters, sweep_walk_forward, should_apply_recommendation,
    )
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")

    profile = _read_yaml(profile_path)
    current = {
        "min_score": profile.get("min_score"),
        "min_rr": profile.get("min_rr"),
        "risk_pct": profile.get("risk_pct"),
        "sharpe": (profile.get("last_auto_apply") or {}).get("sharpe", 0.0),
    }

    age = _last_apply_age_days(profile, now=now)
    if age is not None and age < min_days_since_last_apply:
        out = {
            "applied": False,
            "reason": "cooldown_active",
            "days_since_last_apply": round(age, 1),
            "cooldown_days": min_days_since_last_apply,
        }
        if obsidian_vault:
            _write_obsidian_note(obsidian_vault, applied=False,
                                   reason="cooldown_active",
                                   before=current, after=current,
                                   sweep={"status": "skipped"}, ts=ts)
        return out

    sweep = sweep_walk_forward(records)
    if sweep.get("status") != "ok":
        sweep = sweep_hyperparameters(records)
    rec = should_apply_recommendation(
        sweep, current=current,
        min_sharpe_improvement=min_sharpe_improvement,
        min_sharpe_absolute=min_sharpe_absolute,
    )
    if not rec.get("apply"):
        out = {
            "applied": False,
            "reason": rec.get("reason"),
            "sweep_status": sweep.get("status"),
            "delta_sharpe": rec.get("delta"),
        }
        if obsidian_vault:
            _write_obsidian_note(obsidian_vault, applied=False,
                                   reason=rec.get("reason") or "no_apply",
                                   before=current, after=current,
                                   sweep=sweep, ts=ts)
        return out

    new = rec["new"]
    after = {
        "min_score": new["min_score"],
        "min_rr": new["min_rr"],
        "risk_pct": new["risk_pct"],
    }
    profile.update(after)
    profile["last_auto_apply"] = {
        "ts": ts,
        "delta_sharpe": rec.get("delta"),
        "sharpe": new["sharpe"],
        "n_trades": new["n_trades"],
        "before": current,
    }
    if not _write_yaml(profile_path, profile):
        return {"applied": False, "reason": "yaml_write_failed"}

    if obsidian_vault:
        _write_obsidian_note(obsidian_vault, applied=True,
                               reason="sweep_recommendation_applied",
                               before=current, after=after,
                               sweep=sweep, ts=ts)
    return {
        "applied": True,
        "reason": "sweep_recommendation_applied",
        "before": current,
        "after": after,
        "delta_sharpe": rec.get("delta"),
        "ts": ts,
    }
