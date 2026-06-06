"""Symbol-only auto trading workflow.

User contract:
    POST /api/smc-crypto/auto-run  { "symbol": "BTC-USDT" }

That's the entire user-facing surface. Everything else — interval,
confluence threshold, RR floor, sizing, max notional, defensive mode —
is derived per-symbol from an asset-tier profile and the latest paper-
acceptance verdict for that symbol.

Workflow (one cron tick or one /auto-run call):

    Phase A — Profile selection
        Look up the asset tier (major / altcoin / smallcap) from the
        symbol and produce a SmcAutoProfile (interval, score floor,
        risk_pct, max_notional, …). Heavier coins get tighter knobs.

    Phase B — Pre-flight guard
        Pull the most recent paper-acceptance run for this symbol.
        • strategy_invalidated  → REFUSE (return action=blocked)
        • failed_repeat_paper   → ALLOW dry-run only, no live order
        • passed / conditional  → ALLOW full live cycle
        • no history            → ALLOW dry-run only

    Phase C — Run the unified pipeline
        UnifiedTradingSession.run(place_live_orders=allowed_live)
        which itself does propose → dry_run → live POST → acceptance.

    Phase D — Reconcile + journal
        Pull /v1/fills, mirror any filled qty into the §18.2 ledger,
        update the per-symbol cooldown so we don't fire again within
        ``cooldown_minutes``.

The result is a single dict describing what happened, why, and what
the operator can expect on the next tick.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from smc_paper_runner import CryptoApiClient
from smc_unified_system import UnifiedTradingSession, UnifiedSessionConfig
from paper_acceptance_store import load_acceptance_reports


# ---------------------------------------------------------------------------
# Asset-tier profile  (§17.6 ATR-adaptive + §17.8 leverage caps)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SmcAutoProfile:
    """All derived knobs for one symbol — never exposed to the user."""

    tier: str
    interval: str
    bars: int
    swing_length: int
    internal_swing_length: int
    min_confluence_score: int
    min_rr: float
    risk_pct: float
    max_notional_usdt: float
    price_deviation_pct: float
    cooldown_minutes: int


_MAJOR = {"BTC-USDT", "ETH-USDT"}
_ALTCOIN = {"SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT", "AVAX-USDT", "MATIC-USDT"}


def profile_for_symbol(symbol: str) -> SmcAutoProfile:
    """Pick a default profile based on the symbol's volatility tier."""
    sym = symbol.upper()
    if sym in _MAJOR:
        return SmcAutoProfile(
            tier="major",
            interval="1h", bars=500,
            swing_length=5, internal_swing_length=3,
            min_confluence_score=8, min_rr=1.5,
            risk_pct=0.02, max_notional_usdt=5_000.0,
            price_deviation_pct=0.02, cooldown_minutes=60,
        )
    if sym in _ALTCOIN:
        return SmcAutoProfile(
            tier="altcoin",
            interval="15m", bars=500,
            swing_length=4, internal_swing_length=2,
            min_confluence_score=9, min_rr=1.8,
            risk_pct=0.01, max_notional_usdt=2_000.0,
            price_deviation_pct=0.025, cooldown_minutes=30,
        )
    # smallcap / unknown — most conservative defaults
    return SmcAutoProfile(
        tier="smallcap",
        interval="15m", bars=500,
        swing_length=3, internal_swing_length=2,
        min_confluence_score=10, min_rr=2.0,
        risk_pct=0.005, max_notional_usdt=500.0,
        price_deviation_pct=0.03, cooldown_minutes=120,
    )


# ---------------------------------------------------------------------------
# Pre-flight: consult paper-acceptance history
# ---------------------------------------------------------------------------

@dataclass
class PreflightVerdict:
    """Outcome of looking at recent acceptance history."""

    allowed_live: bool
    reason: str
    last_conclusion: Optional[str] = None
    last_run_at: Optional[str] = None


def preflight(conn: sqlite3.Connection, symbol: str) -> PreflightVerdict:
    """Read paper-acceptance history for the symbol and decide.

    Rules:
      • strategy_invalidated         → block live (force dry-run only)
      • failed_repeat_paper          → block live
      • passed / conditionally_passed → allow live
      • no history                   → allow dry-run only (need 1 baseline run first)
    """
    reports = load_acceptance_reports(conn, symbol=symbol, limit=1)
    if not reports:
        return PreflightVerdict(
            allowed_live=False,
            reason="no_acceptance_history_yet_run_dry_first",
        )
    last = reports[0]
    conclusion = last.get("conclusion")
    if conclusion in {"passed", "conditionally_passed"}:
        return PreflightVerdict(
            allowed_live=True,
            reason=f"last_acceptance={conclusion}",
            last_conclusion=conclusion,
            last_run_at=last.get("created_at"),
        )
    return PreflightVerdict(
        allowed_live=False,
        reason=f"last_acceptance={conclusion}",
        last_conclusion=conclusion,
        last_run_at=last.get("created_at"),
    )


# ---------------------------------------------------------------------------
# Cooldown registry — keeps per-symbol firing rate sane
# ---------------------------------------------------------------------------

class _CooldownRegistry:
    """In-memory per-(symbol, db_path) last-fire timestamp."""
    _store: dict[tuple[str, str], datetime] = {}

    @classmethod
    def last_fire(cls, symbol: str, db_path: str) -> Optional[datetime]:
        return cls._store.get((symbol.upper(), db_path))

    @classmethod
    def record_fire(cls, symbol: str, db_path: str, at: Optional[datetime] = None) -> None:
        cls._store[(symbol.upper(), db_path)] = at or datetime.now(timezone.utc)

    @classmethod
    def reset(cls) -> None:
        cls._store.clear()


def _adaptive_cooldown_multiplier(
    last_outcomes: list[str],
    *,
    streak_size: int = 3,
    loss_streak_multiplier: float = 2.0,
    win_streak_multiplier: float = 0.5,
) -> float:
    """P2-16 — adapt cooldown to recent outcome streak.

    Rules:
      • Last ``streak_size`` resolved trades all LOSSES → cooldown ×2
        (back off, give the market time to change regime)
      • Last ``streak_size`` resolved trades all WINS → cooldown ×0.5
        (we're in sync, ride momentum but don't go full no-cooldown)
      • Mixed → ×1.0

    ``last_outcomes`` is the most-recent-first list of "win"/"loss"
    strings. Pending trades should be filtered out by the caller.
    """
    if len(last_outcomes) < streak_size:
        return 1.0
    last_n = last_outcomes[:streak_size]
    if all(o == "loss" for o in last_n):
        return float(loss_streak_multiplier)
    if all(o == "win" for o in last_n):
        return float(win_streak_multiplier)
    return 1.0


def _recent_outcomes_for_cooldown(db_path: str, symbol: str, n: int = 5) -> list[str]:
    """Read the last N resolved outcomes for a symbol from training ledger.

    Returns most-recent-first. ``outcome="pending"`` is filtered out.
    Returns ``["win","loss","win"]``-style list.
    """
    try:
        from smc_quant import LedgerPaths, load_cached_trade_records
        all_recs = load_cached_trade_records(LedgerPaths.training_ledger())
    except Exception:
        return []
    filtered = []
    for r in all_recs:
        if r.get("symbol") != symbol:
            continue
        outcome = r.get("outcome")
        if outcome in (None, "pending"):
            continue
        rm = r.get("r_multiple")
        try:
            won = (outcome == "target") or (rm is not None and float(rm) > 0)
        except (TypeError, ValueError):
            won = False
        filtered.append({
            "ts": r.get("entry_time") or r.get("resolved_at") or "",
            "outcome": "win" if won else "loss",
        })
    filtered.sort(key=lambda x: x["ts"], reverse=True)
    return [x["outcome"] for x in filtered[:n]]


def cooldown_remaining(symbol: str, db_path: str, profile: SmcAutoProfile) -> Optional[int]:
    """Seconds remaining on the per-symbol cooldown, or None if free to fire.

    P2-16: applies adaptive multiplier based on recent outcome streak.
    """
    last = _CooldownRegistry.last_fire(symbol, db_path)
    if not last:
        return None
    recent = _recent_outcomes_for_cooldown(db_path, symbol)
    multiplier = _adaptive_cooldown_multiplier(recent)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    effective_cooldown_s = profile.cooldown_minutes * 60 * multiplier
    remaining = effective_cooldown_s - elapsed
    return int(remaining) if remaining > 0 else None


# ---------------------------------------------------------------------------
# Top-level: symbol-only auto run
# ---------------------------------------------------------------------------

@dataclass
class AutoRunResult:
    symbol: str
    started_at: str
    elapsed_seconds: float
    profile: dict
    preflight: dict
    cooldown_seconds_remaining: Optional[int]
    workflow_action: str    # "executed" / "dry_run_only" / "blocked" / "cooldown"
    unified: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


def run_symbol(
    api: CryptoApiClient,
    symbol: str,
    *,
    db_path: str,
    profile_override: Optional[SmcAutoProfile] = None,
    journal_dir: str = "tmp/smc_auto",
    force_live: bool = False,
    ignore_cooldown: bool = False,
) -> AutoRunResult:
    """Run the full symbol-only workflow once."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.time()
    profile = profile_override or profile_for_symbol(symbol)
    notes: list[str] = []

    # Phase A — already done: profile derived

    # Cooldown guard
    cd = cooldown_remaining(symbol, db_path, profile) if not ignore_cooldown else None

    # Phase B — pre-flight
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        verdict = preflight(conn, symbol)
    finally:
        conn.close()
    place_live = (verdict.allowed_live or force_live)

    result = AutoRunResult(
        symbol=symbol,
        started_at=started_at,
        elapsed_seconds=0.0,
        profile=asdict(profile),
        preflight={
            "allowed_live": verdict.allowed_live,
            "reason": verdict.reason,
            "last_conclusion": verdict.last_conclusion,
            "last_run_at": verdict.last_run_at,
        },
        cooldown_seconds_remaining=cd,
        workflow_action="blocked",
        notes=notes,
    )

    if cd is not None and cd > 0:
        result.workflow_action = "cooldown"
        notes.append(f"per-symbol cooldown active: {cd}s left")
        result.elapsed_seconds = round(time.time() - t0, 3)
        return result

    # Phase C — unified pipeline
    cfg = UnifiedSessionConfig(
        symbols=[symbol],
        interval=profile.interval, bars=profile.bars,
        swing_length=profile.swing_length,
        internal_swing_length=profile.internal_swing_length,
        min_confluence_score=profile.min_confluence_score,
        min_rr=profile.min_rr,
        risk_pct=profile.risk_pct,
        max_notional_usdt=profile.max_notional_usdt,
        price_deviation_pct=profile.price_deviation_pct,
        strategy_id=f"smc.v2.auto.{profile.tier}",
        journal_dir=journal_dir,
        paper_db_path=db_path,
    )
    session = UnifiedTradingSession(api, cfg)
    try:
        unified = session.run(place_live_orders=place_live)
    finally:
        session.close()

    result.unified = unified
    if place_live:
        result.workflow_action = "executed"
        _CooldownRegistry.record_fire(symbol, db_path)
        notes.append(f"live cycle ran ({verdict.reason})")
    else:
        result.workflow_action = "dry_run_only"
        notes.append(f"dry-run only ({verdict.reason})")

    # Phase D — quick reconciliation note
    if unified and unified.get("decisions"):
        dec0 = unified["decisions"][0]
        if dec0.get("action") == "placed":
            op = ((dec0.get("live_order") or {}).get("payload") or {})
            notes.append(f"order placed: {op.get('id')} {op.get('side')} {op.get('quantity')}@{op.get('price')}")

    result.elapsed_seconds = round(time.time() - t0, 3)
    return result
