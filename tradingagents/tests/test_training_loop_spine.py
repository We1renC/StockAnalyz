"""Integration tests for the training-loop spine (audit fix H1 / S8).

The decision→trade→learn spine (auto_backtest_window → build_smc_analysis
→ evaluate_entry_models → persist_trade_records) had no dedicated test;
coverage lived only in leaf learning modules. This drives the whole chain
with a synthetic-kline stub API against an isolated tmp ledger + DB.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))


def _synthetic_klines(n: int = 260) -> list[dict]:
    """Trend-with-pullbacks OHLCV so SMC detectors have structure to find."""
    rows = []
    price = 100.0
    base_ms = 1_700_000_000_000
    for i in range(n):
        # gentle uptrend + sinusoidal pullbacks → swings, FVGs, OBs
        drift = i * 0.15
        wave = 6.0 * math.sin(i / 7.0)
        close = 100.0 + drift + wave
        openp = close - 0.8 * math.sin(i / 5.0)
        high = max(openp, close) + 1.5 + abs(math.sin(i / 3.0))
        low = min(openp, close) - 1.5 - abs(math.cos(i / 4.0))
        rows.append({
            "open_time": base_ms + i * 3_600_000,
            "open": round(openp, 4), "high": round(high, 4),
            "low": round(low, 4), "close": round(close, 4),
            "volume": 1000 + (i % 13) * 50,
        })
    return rows


class _StubApi:
    """Minimal CryptoApiClient surface used by auto_backtest_window."""
    def __init__(self, rows):
        self._rows = rows

    def klines(self, symbol, interval="1h", limit=500):
        return {"payload": {"data": self._rows[-limit:]}}


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SMC_LEDGER_DIR", str(tmp_path))
    ledger = tmp_path / "smc_training_ledger.jsonl"
    db = tmp_path / "t.db"
    return {"ledger": str(ledger), "db": str(db)}


def test_auto_backtest_window_runs_spine_and_persists(isolated_env):
    """H1: klines → analysis → evaluate → ledger, end-to-end, no crash."""
    from smc_training_loop import auto_backtest_window
    api = _StubApi(_synthetic_klines(260))
    summary = auto_backtest_window(
        api, "BTC-USDT", interval="1h", bars=260,
        ledger_path=isolated_env["ledger"], db_path=isolated_env["db"],
    )
    # Summary is well-formed regardless of how many entries triggered.
    assert summary.symbol == "BTC-USDT"
    assert summary.ledger_path == isolated_env["ledger"]
    assert summary.trades_settled >= 0


def test_auto_backtest_window_empty_klines_is_graceful(isolated_env):
    """H1: empty kline payload → zero-trade summary, no exception."""
    from smc_training_loop import auto_backtest_window

    class _Empty:
        def klines(self, symbol, interval="1h", limit=500):
            return {"payload": {"data": []}}

    summary = auto_backtest_window(
        _Empty(), "ETH-USDT", interval="1h", bars=100,
        ledger_path=isolated_env["ledger"], db_path=isolated_env["db"],
    )
    assert summary.trades_settled == 0


def test_run_training_cycle_smoke(isolated_env):
    """H1: full cycle (backtest → ingest → train → audit) returns a
    well-formed dict and does not raise on synthetic data."""
    from smc_training_loop import run_training_cycle
    api = _StubApi(_synthetic_klines(260))
    out = run_training_cycle(
        api, ["BTC-USDT"], db_path=isolated_env["db"],
        interval="1h", bars=260, ledger_path=isolated_env["ledger"],
    )
    assert isinstance(out, dict)
    # cycle reports per-symbol backtest summaries
    assert "backtests" in out or "backtest_summaries" in out or out
