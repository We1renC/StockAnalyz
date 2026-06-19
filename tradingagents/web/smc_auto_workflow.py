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
from learning.obs_log import get_logger, swallow
_logger = get_logger(__name__)


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
        profile = SmcAutoProfile(
            tier="major",
            interval="15s", bars=500,
            swing_length=5, internal_swing_length=3,
            min_confluence_score=8, min_rr=1.5,
            risk_pct=0.02, max_notional_usdt=5_000.0,
            price_deviation_pct=0.02, cooldown_minutes=60,
        )
    elif sym in _ALTCOIN:
        profile = SmcAutoProfile(
            tier="altcoin",
            interval="15s", bars=500,
            swing_length=4, internal_swing_length=2,
            min_confluence_score=9, min_rr=1.8,
            risk_pct=0.01, max_notional_usdt=2_000.0,
            price_deviation_pct=0.025, cooldown_minutes=30,
        )
    else:
        # smallcap / unknown — most conservative defaults
        profile = SmcAutoProfile(
            tier="smallcap",
            interval="15s", bars=500,
            swing_length=3, internal_swing_length=2,
            min_confluence_score=10, min_rr=2.0,
            risk_pct=0.005, max_notional_usdt=500.0,
            price_deviation_pct=0.03, cooldown_minutes=120,
        )

    # Check for custom overrides in config/strategy.yaml
    try:
        # Check if override in strategy.yaml
        config_dir = Path(__file__).parent.parent / "config"
        yaml_path = config_dir / "strategy.yaml"
        if yaml_path.exists():
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            # Allow override under either adaptive or risk section
            custom_cooldown = data.get("adaptive", {}).get("cooldown_minutes")
            if custom_cooldown is None:
                custom_cooldown = data.get("risk", {}).get("cooldown_minutes")
            if custom_cooldown is not None:
                from dataclasses import replace
                profile = replace(profile, cooldown_minutes=int(custom_cooldown))
    except Exception as e:
        _logger.warning("Failed to load strategy yaml override for cooldown: %s", e)

    return profile


# ---------------------------------------------------------------------------
# Pre-flight: consult paper-acceptance history
# ---------------------------------------------------------------------------

def load_latest_adaptive_runtime_patch(conn: sqlite3.Connection, symbol: str) -> dict:
    row = conn.execute(
        """SELECT patch_payload FROM smc_adaptive_config_patches 
            WHERE symbol=? AND patch_type='adaptive_runtime'
         ORDER BY id DESC LIMIT 1""",
        (symbol.upper(),),
    ).fetchone()
    if row:
        try:
            return json.loads(row["patch_payload"])
        except Exception as e:
            _logger.error("Failed to parse patch_payload: %s", e)
    return {}


def get_current_equity_usdt(api) -> Optional[float]:
    """Calculate total account equity in USDT dynamically based on spot balances and ticker prices."""
    try:
        bal_resp = api.balances()
        if not bal_resp or bal_resp.get("status") != 200:
            return None
        
        balances_data = bal_resp.get("payload", {}).get("data", [])
        total_value_usdt = 0.0
        
        for bal in balances_data:
            asset = bal.get("asset")
            total_qty = float(bal.get("total") or 0.0)
            if total_qty <= 0:
                continue
                
            if asset == "USDT":
                total_value_usdt += total_qty
            else:
                last_price = 0.0
                try:
                    ticker_resp = api.ticker(f"{asset}-USDT")
                    if ticker_resp and ticker_resp.get("status") == 200:
                        last_price = float(ticker_resp.get("payload", {}).get("last_price") or 0.0)
                except Exception as e:
                    _logger.warning("Failed to fetch ticker for %s: %s", asset, e)
                
                if last_price <= 0.0:
                    try:
                        from deps import get_db
                        with get_db() as conn:
                            row = conn.execute(
                                "SELECT price FROM price_cache WHERE symbol = ? OR symbol = ?",
                                (f"{asset}-USDT", asset)
                            ).fetchone()
                            if row and row["price"] is not None:
                                last_price = float(row["price"])
                    except Exception as db_err:
                        _logger.warning("Failed to lookup price_cache for %s: %s", asset, db_err)

                if last_price <= 0.0:
                    FALLBACK_PRICES = {
                        "BTC": 68000.0,
                        "ETH": 3500.0,
                        "SOL": 150.0,
                        "BNB": 600.0,
                        "XRP": 0.6,
                    }
                    last_price = FALLBACK_PRICES.get(asset.upper(), 1.0)
                
                total_value_usdt += total_qty * last_price
        return total_value_usdt if total_value_usdt > 10.0 else None
    except Exception as e:
        _logger.exception("Failed to get current equity USDT: %s", e)
        return None


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
      • VALIDATING_PROBE mode        → allow live (scaled down exploration)
      • strategy_invalidated         → block live (force dry-run only)
      • failed_repeat_paper          → block live
      • passed / conditionally_passed → allow live
      • no history                   → allow dry-run only (need 1 baseline run first)
    """
    patch = load_latest_adaptive_runtime_patch(conn, symbol)
    mode = (patch.get("state") or {}).get("mode", "DRY_RUN")

    reports = load_acceptance_reports(conn, symbol=symbol, limit=1)
    last_conclusion = reports[0].get("conclusion") if reports else None
    last_run_at = reports[0].get("created_at") if reports else None

    if mode == "VALIDATING_PROBE":
        return PreflightVerdict(
            allowed_live=True,
            reason="validating_probe_mode",
            last_conclusion=last_conclusion,
            last_run_at=last_run_at,
        )

    if not reports:
        return PreflightVerdict(
            allowed_live=False,
            reason="no_acceptance_history_yet_run_dry_first",
            last_conclusion=last_conclusion,
            last_run_at=last_run_at,
        )
    conclusion = last_conclusion
    if conclusion in {"passed", "conditionally_passed"}:
        return PreflightVerdict(
            allowed_live=True,
            reason=f"last_acceptance={conclusion}",
            last_conclusion=conclusion,
            last_run_at=last_run_at,
        )
    return PreflightVerdict(
        allowed_live=False,
        reason=f"last_acceptance={conclusion}",
        last_conclusion=conclusion,
        last_run_at=last_run_at,
    )


# ---------------------------------------------------------------------------
# Cooldown registry — keeps per-symbol firing rate sane
# ---------------------------------------------------------------------------

class _CooldownRegistry:
    """Persistent per-(symbol, db_path) last-fire timestamp via SQLite with in-memory fallback."""
    _store: dict[tuple[str, str], datetime] = {}

    @classmethod
    def last_fire(cls, symbol: str, db_path: str) -> Optional[datetime]:
        if db_path and db_path != ":memory:":
            try:
                import sqlite3
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("CREATE TABLE IF NOT EXISTS smc_cooldown_registry (symbol TEXT PRIMARY KEY, last_fire_at TEXT)")
                conn.commit()
                cursor = conn.cursor()
                cursor.execute("SELECT last_fire_at FROM smc_cooldown_registry WHERE symbol = ?", (symbol.upper(),))
                row = cursor.fetchone()
                conn.close()
                if row and row[0]:
                    return datetime.fromisoformat(row[0])
            except Exception as e:
                _logger.warning("Failed to query last_fire from DB for %s: %s", symbol, e)
        return cls._store.get((symbol.upper(), db_path))

    @classmethod
    def record_fire(cls, symbol: str, db_path: str, at: Optional[datetime] = None) -> None:
        fire_time = at or datetime.now(timezone.utc)
        if db_path and db_path != ":memory:":
            try:
                import sqlite3
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("CREATE TABLE IF NOT EXISTS smc_cooldown_registry (symbol TEXT PRIMARY KEY, last_fire_at TEXT)")
                conn.execute("REPLACE INTO smc_cooldown_registry (symbol, last_fire_at) VALUES (?, ?)", (symbol.upper(), fire_time.isoformat()))
                conn.commit()
                conn.close()
            except Exception as e:
                _logger.warning("Failed to write record_fire to DB for %s: %s", symbol, e)
        cls._store[(symbol.upper(), db_path)] = fire_time

    @classmethod
    def try_acquire_cooldown(cls, symbol: str, db_path: str, cooldown_s: float) -> bool:
        """Atomic check-and-set for cooldown: returns True if acquired, False if still in cooldown."""
        now = datetime.now(timezone.utc)
        symbol_upper = symbol.upper()
        
        # 1. Fallback for :memory: or if db_path is empty
        if not db_path or db_path == ":memory:":
            last = cls._store.get((symbol_upper, db_path))
            if last and (now - last).total_seconds() < cooldown_s:
                return False
            cls._store[(symbol_upper, db_path)] = now
            return True
            
        # 2. SQLite atomic check-and-set
        try:
            import sqlite3
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS smc_cooldown_registry (symbol TEXT PRIMARY KEY, last_fire_at TEXT)")
                cursor = conn.cursor()
                cursor.execute("SELECT last_fire_at FROM smc_cooldown_registry WHERE symbol = ?", (symbol_upper,))
                row = cursor.fetchone()
                if row and row[0]:
                    last_time = datetime.fromisoformat(row[0])
                    if last_time.tzinfo is None:
                        last_time = last_time.replace(tzinfo=timezone.utc)
                    if (now - last_time).total_seconds() < cooldown_s:
                        conn.rollback()
                        return False
                
                conn.execute(
                    "REPLACE INTO smc_cooldown_registry (symbol, last_fire_at) VALUES (?, ?)",
                    (symbol_upper, now.isoformat())
                )
                conn.commit()
                cls._store[(symbol_upper, db_path)] = now
                return True
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception:
            # Fallback on database errors
            last = cls._store.get((symbol_upper, db_path))
            if last and (now - last).total_seconds() < cooldown_s:
                return False
            cls._store[(symbol_upper, db_path)] = now
            return True

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
        from smc_quant import LedgerPaths, read_trade_ledger
        all_recs = read_trade_ledger(LedgerPaths.training_ledger(), symbol=symbol)
    except Exception:
        return []
    filtered = []
    for r in all_recs:
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

    # Phase B — pre-flight (E3: WAL-enabled shared connect) and daily loss limit check
    from smc_quant import connect_db
    conn = connect_db(db_path, row_factory=True)
    daily_loss_breached = False
    daily_realized_pnl = 0.0
    daily_loss_limit = 50000.0
    try:
        verdict = preflight(conn, symbol)
        patch = load_latest_adaptive_runtime_patch(conn, symbol)
        
        # Load daily_loss_limit from strategy.yaml
        try:
            from pathlib import Path
            import yaml
            yaml_path = Path(__file__).resolve().parent.parent / "config/strategy.yaml"
            if yaml_path.exists():
                with open(yaml_path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                    daily_loss_limit = float(data.get("risk", {}).get("daily_loss_limit", 50000.0))
        except Exception:
            pass

        # Calculate daily realized PnL from crypto_fills
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crypto_fills'")
        if c.fetchone() is not None:
            from decimal import Decimal
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            rows = c.execute("SELECT symbol, side, price, quantity, fee, executed_at FROM crypto_fills ORDER BY executed_at ASC").fetchall()
            cost_basis = {}
            for row in rows:
                sym = row["symbol"]
                side = row["side"]
                qty = Decimal(row["quantity"])
                price = Decimal(row["price"])
                fee = Decimal(row["fee"])
                executed_at = row["executed_at"]
                
                base_asset = sym.split("-")[0]
                pos = cost_basis.setdefault(base_asset, {"qty": Decimal("0"), "avg_price": Decimal("0")})
                
                if side == "buy":
                    new_qty = pos["qty"] + qty
                    if new_qty > 0:
                        pos["avg_price"] = (pos["avg_price"] * pos["qty"] + price * qty) / new_qty
                    else:
                        pos["avg_price"] = price
                    pos["qty"] = new_qty
                else: # sell
                    if pos["qty"] > 0:
                        closed_qty = min(qty, pos["qty"])
                        trade_pnl = (price - pos["avg_price"]) * closed_qty - fee
                        if executed_at >= today_start:
                            daily_realized_pnl += float(trade_pnl)
                        pos["qty"] = max(Decimal("0"), pos["qty"] - qty)
            
            if daily_realized_pnl < -daily_loss_limit:
                daily_loss_breached = True
    finally:
        conn.close()
    place_live = (verdict.allowed_live or force_live)

    # G11: Atomic check-and-set cooldown lock if going live
    if place_live and not ignore_cooldown:
        recent = _recent_outcomes_for_cooldown(db_path, symbol)
        multiplier = _adaptive_cooldown_multiplier(recent)
        effective_cooldown_s = profile.cooldown_minutes * 60 * multiplier
        
        acquired = _CooldownRegistry.try_acquire_cooldown(symbol, db_path, effective_cooldown_s)
        if not acquired:
            cd = cooldown_remaining(symbol, db_path, profile)
            notes.append(f"CIRCUIT BREAKER: Atomic cooldown registry lock failed, symbol in cooldown: {cd}s remaining")
            return AutoRunResult(
                symbol=symbol,
                started_at=started_at,
                elapsed_seconds=round(time.time() - t0, 3),
                profile=asdict(profile),
                preflight={
                    "allowed_live": verdict.allowed_live,
                    "reason": verdict.reason,
                    "last_conclusion": verdict.last_conclusion,
                    "last_run_at": verdict.last_run_at,
                },
                cooldown_seconds_remaining=cd,
                workflow_action="cooldown",
                notes=notes,
            )

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
    mode = (patch.get("state") or {}).get("mode", "DRY_RUN")
    risk_multiplier = float((patch.get("risk") or {}).get("risk_multiplier", 1.0))
    probe_cap = float((patch.get("risk") or {}).get("probe_notional_cap_usdt", 11.0))
    probe_cap = max(5.5, min(probe_cap, 1000.0))
    min_score = (patch.get("strategy") or {}).get("confluence_min_score")
    optimal_interval = (patch.get("strategy") or {}).get("optimal_interval")

    # Load max_notional_cap_pct from strategy.yaml, default to 10% (0.10)
    max_notional_cap_pct = 0.10
    try:
        from pathlib import Path
        import yaml
        yaml_path = Path(__file__).resolve().parent.parent / "config/strategy.yaml"
        if yaml_path.exists():
            with open(yaml_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
                max_notional_cap_pct = float(data.get("risk", {}).get("max_notional_cap_pct", 0.10))
    except Exception:
        pass

    # Statistical dynamic single-order max notional calculation to prevent ruin
    current_equity = get_current_equity_usdt(api)
    if current_equity is None:
        notes.append("BLOCKED: equity fetch failed — refusing to size position")
        result.workflow_action = "blocked"
        result.elapsed_seconds = round(time.time() - t0, 3)
        return result

    if daily_loss_breached:
        notes.append(f"CIRCUIT BREAKER: Daily loss limit hit: {daily_realized_pnl:.2f} < -{daily_loss_limit}")
        result.workflow_action = "blocked"
        result.elapsed_seconds = round(time.time() - t0, 3)
        return result

    dynamic_max_notional = current_equity * max_notional_cap_pct
    dynamic_max_notional = max(11.0, dynamic_max_notional)

    risk_pct = profile.risk_pct
    max_notional = dynamic_max_notional
    min_confluence_score = profile.min_confluence_score
    probe_active = False

    # Override profile interval if system evaluated an optimal one
    interval = optimal_interval if optimal_interval else profile.interval
    if optimal_interval:
        notes.append(f"Auto-selected optimal interval: {optimal_interval} (overrode default {profile.interval})")

    if mode == "VALIDATING_PROBE":
        risk_pct = float(profile.risk_pct) * risk_multiplier
        max_notional = min(dynamic_max_notional, probe_cap)
        if max_notional < 11.0:
            max_notional = 11.0
        probe_active = True
        notes.append(f"VALIDATING_PROBE active: scaling risk_pct by {risk_multiplier} to {risk_pct:.5f}, cap notional by {max_notional}")
    else:
        notes.append(f"Dynamic max_notional calculated from equity ${current_equity:.2f} (cap_pct={max_notional_cap_pct*100:.1f}%): ${max_notional:.2f}")

    if min_score is not None:
        min_confluence_score = int(min_score)

    cfg = UnifiedSessionConfig(
        symbols=[symbol],
        interval=interval, bars=profile.bars,
        swing_length=profile.swing_length,
        internal_swing_length=profile.internal_swing_length,
        min_confluence_score=min_confluence_score,
        min_rr=profile.min_rr,
        risk_pct=risk_pct,
        max_notional_usdt=max_notional,
        price_deviation_pct=profile.price_deviation_pct,
        strategy_id=f"smc.v2.auto.{profile.tier}",
        journal_dir=journal_dir,
        paper_db_path=db_path,
        probe=probe_active,
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
