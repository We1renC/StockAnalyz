"""Server-side auto-learn scheduler.

Audit fix E1. The self-learning loop was driven ENTIRELY by the browser:
``/api/smc-crypto/auto-learn-tick`` fired from a JS ``setTimeout``. With
no dashboard open, NOTHING learns — B1 auto-sweep, C3 weekly digest, D3
decommission all depend on ticks that only happen when a tab is open.

This module runs the tick loop server-side as an asyncio background task
so learning continues headless. The UI becomes an observer, not the
driver.

Activation (opt-in to avoid surprising dev runs):
  • env ``SMC_AUTOLEARN_ENABLED=1`` → loop runs
  • env ``SMC_AUTOLEARN_SYMBOLS="BTC-USDT,ETH-USDT,SOL-USDT"`` → which
    symbols to cycle (default: a small majors set)
  • env ``SMC_AUTOLEARN_MIN_INTERVAL`` → floor on per-symbol cadence
    (seconds, default 30) so a misconfigured next_interval can't busy-spin

The loop honours each tick's returned ``next_interval_seconds`` (the
existing throttling policy in smc_training_history) per symbol, so a
saturated symbol naturally slows down.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Optional

from learning.obs_log import get_logger, log_event

_log = get_logger(__name__)


def is_enabled() -> bool:
    return os.environ.get("SMC_AUTOLEARN_ENABLED", "").strip() in ("1", "true", "True", "yes")


def configured_symbols() -> list[str]:
    raw = os.environ.get("SMC_AUTOLEARN_SYMBOLS", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


def _min_interval() -> float:
    try:
        return max(5.0, float(os.environ.get("SMC_AUTOLEARN_MIN_INTERVAL", "30")))
    except (TypeError, ValueError):
        return 30.0


def _maintenance_interval() -> float:
    """How often (seconds) to run rotation + decommission. Default 6h."""
    try:
        return max(300.0, float(os.environ.get("SMC_MAINTENANCE_INTERVAL", "21600")))
    except (TypeError, ValueError):
        return 21600.0


# Maintenance state for the ops endpoint.
_maintenance: dict = {"last_run_at": None, "last_result": None, "runs": 0, "errors": 0}


def maintenance_state() -> dict:
    return dict(_maintenance)


def _run_maintenance() -> dict:
    """Audit fix (Round J): autonomous housekeeping so the headless loop
    self-maintains — rotate the training ledger + run the decommission
    sweep. Both are best-effort; failures are logged, never fatal.
    """
    out: dict = {}
    try:
        from smc_quant import LedgerPaths
        from learning.ledger_rotation import rotate_ledger
        keep = int(os.environ.get("SMC_LEDGER_KEEP_PER_SYMBOL", "1000"))
        out["rotation"] = rotate_ledger(LedgerPaths.training_ledger(),
                                          keep_per_symbol=keep)
        # Round-2 audit: the interval-scoped ledgers (.15m/.1h/...) were
        # never rotated — the .15m file had grown past the main one.
        import glob as _glob
        base = LedgerPaths.training_ledger()
        for p in _glob.glob(base.replace(".jsonl", ".*.jsonl")):
            out[f"rotation:{os.path.basename(p)}"] = rotate_ledger(
                p, keep_per_symbol=keep)
    except Exception as exc:
        out["rotation"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        import os as _os
        from smc_quant import LedgerPaths, read_trade_ledger
        from learning.model_decommission import (
            compute_per_model_health, decide_decommission, load_state, save_state,
        )
        records = read_trade_ledger(LedgerPaths.training_ledger())
        decom_path = _os.path.join(
            _os.path.dirname(LedgerPaths.training_ledger()), "decommissioned.json")
        prev = load_state(decom_path)
        health = compute_per_model_health(records)
        dec = decide_decommission(
            health,
            prev,
            min_win_rate=float(os.environ.get("SMC_DECOMMISSION_MIN_WIN_RATE", "0.05")),
            min_clipped_mean_R=float(
                os.environ.get("SMC_DECOMMISSION_MIN_CLIPPED_MEAN_R", "0.0")
            ),
        )
        if dec.get("actions"):
            save_state(decom_path, dec["new_state"])
        out["decommission"] = {"actions": dec.get("actions", [])}
    except Exception as exc:
        out["decommission"] = {"error": f"{type(exc).__name__}: {exc}"}
    # Round N: WAL checkpoint. With E3's WAL mode, the -wal sidecar grows
    # under sustained writes if a long-running reader holds back the
    # auto-checkpoint. A periodic TRUNCATE checkpoint keeps it bounded.
    try:
        out["wal_checkpoint"] = checkpoint_wal()
    except Exception as exc:
        out["wal_checkpoint"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def checkpoint_wal() -> dict:
    """Run PRAGMA wal_checkpoint(TRUNCATE) on the portfolio DB.

    Returns the checkpoint result {busy, log_pages, checkpointed_pages}
    plus the residual -wal byte size so ops-metrics can chart it.
    """
    import os as _os
    from deps import portfolio_db_path, get_db
    conn = get_db()
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    busy, log_pages, ckpt_pages = (list(row) + [None, None, None])[:3] if row else (None, None, None)
    wal_path = portfolio_db_path() + "-wal"
    wal_bytes = _os.path.getsize(wal_path) if _os.path.exists(wal_path) else 0
    return {
        "busy": busy, "log_pages": log_pages,
        "checkpointed_pages": ckpt_pages, "wal_bytes_after": wal_bytes,
    }


# Per-symbol "next eligible run" wallclock + last result, so the loop is
# inspectable from an ops endpoint (G2).
_state: dict[str, dict] = {}


def scheduler_state() -> dict:
    """Snapshot for the ops-metrics endpoint."""
    return {
        "enabled": is_enabled(),
        "symbols": configured_symbols(),
        "min_interval_seconds": _min_interval(),
        "maintenance_interval_seconds": _maintenance_interval(),
        "maintenance": dict(_maintenance),
        "per_symbol": dict(_state),
    }


async def autolearn_loop(
    tick_fn: Callable[[dict], dict],
    *,
    sleep_fn: Optional[Callable] = None,
    now_fn: Optional[Callable[[], float]] = None,
) -> None:
    """Background loop. ``tick_fn(payload)`` runs ONE symbol's tick
    (the existing api_smc_crypto_auto_learn_tick body) and returns its
    response dict; we read ``history.next_interval_seconds`` to pace.

    ``tick_fn`` is sync (DB + network bound); we offload to a thread so
    we never block the event loop.
    """
    if not is_enabled():
        print("[autolearn] disabled (set SMC_AUTOLEARN_ENABLED=1 to run)")
        return
    sleep = sleep_fn or asyncio.sleep
    clock = now_fn or time.time
    floor = _min_interval()
    maint_every = _maintenance_interval()
    symbols = configured_symbols()
    print(f"[autolearn] server-side loop started for {symbols} "
          f"(floor={floor}s, maintenance every {maint_every}s)")
    # Initialize all symbols eligible immediately.
    for s in symbols:
        _state.setdefault(s, {"next_run_at": 0.0, "last_status": None,
                               "last_run_at": None, "errors": 0})
    # First maintenance one interval out (don't rotate on the very first tick).
    next_maint_at = clock() + maint_every

    while True:
        try:
            now = clock()
            # Round J: autonomous housekeeping on its own cadence.
            if now >= next_maint_at:
                try:
                    res = await asyncio.to_thread(_run_maintenance)
                    _maintenance["last_result"] = res
                    _maintenance["runs"] = int(_maintenance.get("runs", 0)) + 1
                except Exception as exc:
                    _maintenance["errors"] = int(_maintenance.get("errors", 0)) + 1
                    log_event(_log, "maintenance_error", err=type(exc).__name__)
                    try:
                        from learning.alerting import send_alert
                        send_alert("自動維護失敗",
                                    f"{type(exc).__name__}: {exc}",
                                    severity="critical")
                    except Exception:
                        pass
                finally:
                    _maintenance["last_run_at"] = clock()
                    next_maint_at = clock() + maint_every
            due = [s for s in configured_symbols()
                     if _state.get(s, {}).get("next_run_at", 0.0) <= now]
            for sym in due:
                st = _state.setdefault(sym, {"next_run_at": 0.0, "last_status": None,
                                              "last_run_at": None, "errors": 0})
                try:
                    resp = await asyncio.to_thread(tick_fn, {"symbol": sym})
                    nxt = float(((resp or {}).get("history") or {})
                                 .get("next_interval_seconds") or floor)
                    nxt = max(floor, nxt)
                    st["last_status"] = (resp or {}).get("state") or "ok"
                    st["last_run_at"] = clock()
                    st["next_run_at"] = clock() + nxt
                except Exception as exc:        # never let one symbol kill the loop
                    st["errors"] = int(st.get("errors", 0)) + 1
                    st["last_status"] = f"error:{type(exc).__name__}"
                    st["last_run_at"] = clock()
                    st["next_run_at"] = clock() + floor
                    log_event(_log, "autolearn_tick_error", symbol=sym,
                              err=type(exc).__name__, errors=st["errors"])
                    # Phase-1 alerting: persistent failure (every 5th error
                    # per symbol) reaches the operator; cooldown dedups.
                    if st["errors"] % 5 == 0:
                        try:
                            from learning.alerting import send_alert
                            send_alert(f"學習迴路連續失敗 {sym}",
                                        f"{st['errors']} errors, last: {type(exc).__name__}",
                                        severity="critical")
                        except Exception:
                            pass
            # Sleep until the soonest next_run (bounded by floor) to avoid
            # busy-spinning.
            soonest = min((v.get("next_run_at", 0.0) for v in _state.values()),
                           default=clock() + floor)
            delay = max(1.0, min(floor, soonest - clock()))
            await sleep(delay)
        except asyncio.CancelledError:
            print("[autolearn] loop cancelled")
            raise
        except Exception as exc:
            print(f"[autolearn] loop error: {exc}")
            await sleep(floor)
