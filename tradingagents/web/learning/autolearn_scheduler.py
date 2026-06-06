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


# Per-symbol "next eligible run" wallclock + last result, so the loop is
# inspectable from an ops endpoint (G2).
_state: dict[str, dict] = {}


def scheduler_state() -> dict:
    """Snapshot for the ops-metrics endpoint."""
    return {
        "enabled": is_enabled(),
        "symbols": configured_symbols(),
        "min_interval_seconds": _min_interval(),
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
    symbols = configured_symbols()
    print(f"[autolearn] server-side loop started for {symbols} (floor={floor}s)")
    # Initialize all symbols eligible immediately.
    for s in symbols:
        _state.setdefault(s, {"next_run_at": 0.0, "last_status": None,
                               "last_run_at": None, "errors": 0})

    while True:
        try:
            now = clock()
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
                    print(f"[autolearn] {sym} tick error: {exc}")
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
