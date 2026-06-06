"""Historical kline backfill + offline backtest seeding.

The local crypto-api defaults to ``testnet.binance.vision`` which only
holds a few days of history → SMC engine can't find enough structure to
trigger entries (BTC stays at 0 samples). This module:

  1. Fetches N months of klines directly from Binance public data
     endpoints (no auth required).
  2. Walks a synthetic forward-window across the history (same logic as
     auto_backtest_window) and produces trade records.
  3. Persists into the §18.2 trade ledger via persist_trade_records
     (which already dedups by trade_id, so re-running is idempotent).

CLI:
    python -m learning.historical_seeder --months 6
    python -m learning.historical_seeder --months 12 \\
        --symbols BTC-USDT,ETH-USDT --interval 1h
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, asdict, is_dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

# Binance public-data endpoint (no auth, generous rate limits).
DATA_API = "https://data-api.binance.vision"


def fetch_klines_window(
    symbol: str,
    *,
    interval: str = "1h",
    start_ms: int,
    end_ms: int,
    chunk: int = 1000,
    sleep_between_ms: int = 100,
) -> list[dict]:
    """Paginate ``/api/v3/klines`` across a time window.

    Binance caps a single call at 1000 candles, so we walk forward in
    chunks. Sleeps a tiny bit between calls to be polite to the public
    endpoint.
    """
    binance_sym = symbol.replace("-", "")
    out: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            f"{DATA_API}/api/v3/klines?symbol={binance_sym}"
            f"&interval={interval}&startTime={cursor}&limit={chunk}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                data = json.loads(res.read().decode("utf-8"))
        except Exception as exc:
            print(f"[seeder] {symbol} fetch failed at cursor={cursor}: {exc}")
            break
        if not data:
            break
        for k in data:
            out.append({
                "open_time": int(k[0]),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })
        # advance cursor to bar after the last returned
        last_close_ms = int(data[-1][6])
        if last_close_ms <= cursor:        # protect against stuck cursor
            break
        cursor = last_close_ms + 1
        time.sleep(sleep_between_ms / 1000.0)
    # Trim anything past the requested end
    out = [r for r in out if r["open_time"] <= end_ms]
    return out


@dataclass
class SeedResult:
    symbol: str
    interval: str
    klines_fetched: int
    backtests_run: int
    trades_persisted: int
    window_start: str
    window_end: str
    elapsed_s: float


def run_post_seed_training(
    *,
    ledger_path: Optional[str] = None,
    db_path: Optional[str] = None,
    symbol: str = "ALL",
) -> dict:
    """Close the loop after a historical backfill by retraining once.

    Seeding writes trade records into the shared training ledger, but that
    alone does not refresh adaptive gates / runtime patches. This helper runs
    one learning cycle over the freshly seeded ledger and returns a compact
    summary for CLI or automation callers.
    """
    from smc_quant import LedgerPaths
    from smc_training_loop import train_from_ledger

    result = train_from_ledger(
        ledger_path=ledger_path or LedgerPaths.training_ledger(),
        db_path=db_path,
        symbol=symbol,
    )
    if is_dataclass(result):
        payload = asdict(result)
    elif isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {
            "sample_size": getattr(result, "sample_size", 0),
            "adopted": getattr(result, "adopted", False),
            "verdict": getattr(result, "verdict", {}),
            "weights_changed": getattr(result, "weights_changed", []),
            "adaptive_patch_key": getattr(result, "adaptive_patch_key", None),
            "strategy_patch_key": getattr(result, "strategy_patch_key", None),
            "notes": getattr(result, "notes", []),
            "adaptive_state": getattr(result, "adaptive_state", {}),
        }
    return {
        "sample_size": payload["sample_size"],
        "adopted": payload["adopted"],
        "verdict": payload["verdict"],
        "learning_indicator": payload.get("adaptive_state", {}).get("mode"),
        "weights_changed": payload["weights_changed"],
        "adaptive_patch_key": payload.get("adaptive_patch_key"),
        "strategy_patch_key": payload.get("strategy_patch_key"),
        "notes": payload["notes"],
    }


def seed_one_symbol(
    api_stub,
    symbol: str,
    *,
    interval: str = "1h",
    months: int = 6,
    bars_per_run: int = 500,
    step_bars: int = 100,
    ledger_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> SeedResult:
    """Walk a backtest window across N months of history.

    We fetch the whole window once, then slide a ``bars_per_run`` window
    forward by ``step_bars`` each iteration, calling auto_backtest_window
    with a stub api that returns the slice. Persist results to the same
    training ledger live learning writes to → dedup keeps it idempotent.
    """
    from smc_quant import LedgerPaths
    from smc_training_loop import auto_backtest_window

    ledger_path = ledger_path or LedgerPaths.training_ledger()
    t0 = time.time()
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=months * 30)
                     ).timestamp() * 1000)

    print(f"[seeder] {symbol} {interval} fetching {months}mo history...")
    all_klines = fetch_klines_window(symbol, interval=interval,
                                       start_ms=start_ms, end_ms=end_ms)
    if not all_klines:
        return SeedResult(symbol, interval, 0, 0, 0, "", "", time.time() - t0)
    print(f"[seeder] {symbol} got {len(all_klines)} klines "
          f"{datetime.fromtimestamp(all_klines[0]['open_time']/1000)} → "
          f"{datetime.fromtimestamp(all_klines[-1]['open_time']/1000)}")

    # Make a stub api whose klines() returns a sliding window slice.
    class _WindowedStub:
        def __init__(self, rows):
            self._rows = rows
            self._cursor_end = bars_per_run

        def klines(self, sym, interval="1h", limit=500):
            end = min(self._cursor_end, len(self._rows))
            start = max(0, end - limit)
            slice_ = self._rows[start:end]
            # Format to match crypto-api response shape (open_time as ISO)
            payload_rows = [{
                "open_time": datetime.fromtimestamp(r["open_time"]/1000,
                                                     timezone.utc).isoformat(),
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "volume": r["volume"],
            } for r in slice_]
            return {"payload": {"data": payload_rows}}

    stub = _WindowedStub(all_klines)
    backtests = 0
    persisted_before = _count_ledger_rows(ledger_path, symbol)

    while stub._cursor_end <= len(all_klines):
        # auto_backtest_window will fetch via stub.klines, run SMC, evaluate,
        # and persist via persist_trade_records (which dedups by trade_id).
        try:
            auto_backtest_window(
                stub, symbol, interval=interval, bars=bars_per_run,
                ledger_path=ledger_path, db_path=db_path,
            )
            backtests += 1
        except Exception as exc:
            print(f"[seeder] {symbol} backtest at cursor={stub._cursor_end} failed: {exc}")
        stub._cursor_end += step_bars

    persisted_after = _count_ledger_rows(ledger_path, symbol)
    return SeedResult(
        symbol=symbol, interval=interval,
        klines_fetched=len(all_klines),
        backtests_run=backtests,
        trades_persisted=persisted_after - persisted_before,
        window_start=datetime.fromtimestamp(all_klines[0]["open_time"]/1000).isoformat(),
        window_end=datetime.fromtimestamp(all_klines[-1]["open_time"]/1000).isoformat(),
        elapsed_s=round(time.time() - t0, 1),
    )


def _count_ledger_rows(path: str, symbol: str) -> int:
    import os
    if not os.path.exists(path):
        return 0
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("symbol") == symbol:
                        n += 1
                except Exception:
                    continue
    except Exception:
        return 0
    return n


def _cli() -> None:
    import argparse, sys
    sys.path.insert(0, ".")
    p = argparse.ArgumentParser(description="Seed training ledger from real Binance history")
    p.add_argument("--months", type=int, default=6, help="how far back (default 6)")
    p.add_argument("--symbols", type=str,
                     default="BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT",
                     help="comma-separated list")
    p.add_argument("--interval", type=str, default=None,
                     help="override profile interval (otherwise per profile_for_symbol)")
    p.add_argument("--bars-per-run", type=int, default=500)
    p.add_argument("--step-bars", type=int, default=100)
    p.add_argument("--skip-training", action="store_true",
                   help="only seed history, skip the post-seed learning cycle")
    args = p.parse_args()

    from smc_auto_workflow import profile_for_symbol
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    results = []
    for sym in symbols:
        iv = args.interval or profile_for_symbol(sym).interval
        r = seed_one_symbol(None, sym, interval=iv, months=args.months,
                              bars_per_run=args.bars_per_run,
                              step_bars=args.step_bars)
        results.append(r)
        print(f"[seeder] {sym} ✓ +{r.trades_persisted} trades "
              f"({r.backtests_run} backtests, {r.elapsed_s}s)")

    print("\n=== summary ===")
    print(f"{'symbol':<12} {'interval':<6} {'klines':>7} {'backtests':>10} "
          f"{'+trades':>8} {'elapsed':>8}")
    for r in results:
        print(f"{r.symbol:<12} {r.interval:<6} {r.klines_fetched:>7} "
              f"{r.backtests_run:>10} {r.trades_persisted:>8} {r.elapsed_s:>7}s")

    if not args.skip_training:
        training = run_post_seed_training()
        print("\n=== post-seed training ===")
        print(
            f"sample_size={training['sample_size']} "
            f"mode={training.get('learning_indicator')} "
            f"adopted={training['adopted']} "
            f"verdict={training['verdict']}"
        )
        if training.get("weights_changed"):
            print(f"weights_changed={training['weights_changed']}")
        if training.get("notes"):
            print("notes:")
            for note in training["notes"]:
                print(f"  - {note}")


if __name__ == "__main__":
    _cli()
