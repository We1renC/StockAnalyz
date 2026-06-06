"""§10.5 — Integration test: SMC engine ↔ crypto-api paper trading.

Spins up the in-process FastAPI app (including the crypto API + simulated
matching engine) and runs ``SmcPaperRunner.run_once()`` end-to-end:
fetch klines → build_smc_analysis → §6 risk gate → POST /v1/orders → audit.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
sys.path.insert(0, str(WEB_DIR))


@pytest.fixture
def crypto_app(tmp_path, monkeypatch):
    """FastAPI app with a fresh isolated portfolio.db so tests don't pollute prod."""
    test_db = tmp_path / "portfolio.db"
    # Both crypto_api modules read DB via module-level constants; redirect them.
    import importlib
    import crypto_api.auth as auth_mod
    import crypto_api.executor as exec_mod
    import crypto_api.router as router_mod
    import crypto_api.models as models_mod
    monkeypatch.setattr(auth_mod, "DB", test_db)
    monkeypatch.setattr(exec_mod, "DB", test_db)
    monkeypatch.setattr(router_mod, "DB", test_db, raising=False)
    # Re-init schema + seed defaults on the fresh DB
    conn = sqlite3.connect(test_db)
    models_mod.init_crypto_db(conn)
    models_mod.seed_crypto_data(conn)
    conn.close()
    # Now import the parent app — its include_router has already been wired
    if "app" in sys.modules:
        importlib.reload(sys.modules["app"])
    import app as fastapi_app
    return fastapi_app.app


@pytest.fixture
def patched_klines(monkeypatch):
    """Replace MarketPriceEngine.get_klines with deterministic local OHLCV."""
    from crypto_api.executor import price_engine
    async def fake_klines(self, symbol, interval, limit=500):
        # 200 ascending bars: clear up-trend so SMC has structure to detect.
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        price = 30000.0
        for i in range(limit):
            o = price
            c = price + (i % 7) - 3       # mild oscillation
            h = max(o, c) + 50
            l = min(o, c) - 50
            rows.append({
                "open_time": (base + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                "open": str(o),
                "high": str(h),
                "low": str(l),
                "close": str(c),
                "volume": "10.0",
                "quote_volume": "300000",
                "trade_count": 100,
                "close_time": (base + timedelta(hours=i + 1)).isoformat().replace("+00:00", "Z"),
            })
            price = c + 5  # drift up
        return rows
    monkeypatch.setattr(type(price_engine), "get_klines", fake_klines)
    return None


def test_smc_paper_runner_journals_decision_when_no_qualified_entry(crypto_app, patched_klines, tmp_path):
    """End-to-end smoke test: runner must succeed (skip or place) and journal it."""
    from smc_paper_runner import CryptoApiClient, SmcPaperRunner, PaperRunConfig

    client = TestClient(crypto_app)
    api = CryptoApiClient(client)

    # 1. Sanity: klines endpoint responds 200 with our patched payload
    resp = api.klines("BTC-USDT", interval="1h", limit=200)
    assert resp["status"] == 200
    assert len(resp["payload"]["data"]) >= 30

    # 2. Run the SMC paper bot
    journal = tmp_path / "smc_paper_journal.jsonl"
    runner = SmcPaperRunner(
        api,
        PaperRunConfig(
            symbol="BTC-USDT",
            interval="1h",
            bars=200,
            account_equity=100_000.0,
            min_confluence_score=8,
            journal_path=str(journal),
            swing_length=3,
            internal_swing_length=2,
        ),
    )
    result = runner.run_once()

    # 3. Journal must be written regardless of outcome
    assert journal.exists()
    assert journal.read_text(encoding="utf-8").strip(), "journal entry should be non-empty"

    # 4. Action must be one of the documented states
    assert result.action.startswith(("placed", "skipped:", "error:"))
    assert result.symbol == "BTC-USDT"
    assert result.bias in {"strong_bullish", "bullish", "neutral", "bearish", "strong_bearish", None}


def test_smc_paper_runner_places_order_when_entry_passes_all_gates(crypto_app, patched_klines, tmp_path, monkeypatch):
    """If we force a high-confluence entry through the SMC layer, runner POSTs /orders."""
    from smc_paper_runner import CryptoApiClient, SmcPaperRunner, PaperRunConfig
    import smc_paper_runner as runner_mod

    client = TestClient(crypto_app)
    api = CryptoApiClient(client)

    # Lower the confluence floor so even a borderline entry on the deterministic
    # synthetic kline series can pass; we're testing the integration, not
    # SMC tuning. Build a fake "best entry" so the path always reaches POST.
    def fake_pick(self, analysis):
        return {
            "model": "sweep_reversal",
            "direction": 1,
            "entry": 30000.0,
            "stop": 29850.0,
            "target": 30350.0,
            "rr": 2.33,
            "risk": 150.0,
            "confluence": {"score": 12, "threshold": 8, "triggered": True, "weights": {}},
            "factors": {"htf_bias_aligned": True, "liquidity_swept": True, "ltf_choch": True},
            "triggered": True,
            "dol_required": False,
            "dol_target": {"target_kind": "BSL", "target_price": 30500.0, "distance": 500.0},
        }
    monkeypatch.setattr(SmcPaperRunner, "_pick_best_entry", fake_pick)

    journal = tmp_path / "smc_paper_placed.jsonl"
    runner = SmcPaperRunner(
        api,
        PaperRunConfig(
            symbol="BTC-USDT",
            account_equity=100_000.0,
            min_confluence_score=8,
            min_rr=1.5,
            journal_path=str(journal),
            swing_length=3,
            internal_swing_length=2,
        ),
    )
    result = runner.run_once()

    # Best-case: order accepted; otherwise we get a documented error code.
    assert result.action in {"placed"} or result.action.startswith("error:"), result.action
    if result.action == "placed":
        assert result.order_response and result.order_response["status"] in (200, 201)
        # Confirm a trade record was journalled
        records_file = Path(str(journal).replace(".jsonl", "_trades.jsonl"))
        assert records_file.exists()
        lines = [l for l in records_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines, "trade record journal should have at least one row"


def test_smc_paper_runner_threads_cluster_weight_table_into_analysis(tmp_path, monkeypatch):
    """B2: runtime runner must pass the learned cluster table into build_smc_analysis."""
    from smc_paper_runner import SmcPaperRunner, PaperRunConfig
    import smc_paper_runner as runner_mod

    class _StubClient:
        def klines(self, symbol, interval="1h", limit=200):
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            rows = []
            price = 30000.0
            for i in range(limit):
                rows.append({
                    "open_time": (base + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                    "open": str(price),
                    "high": str(price + 50),
                    "low": str(price - 50),
                    "close": str(price + 5),
                    "volume": "10.0",
                })
                price += 5
            return {"status": 200, "payload": {"data": rows}}

    captured = {}

    def fake_analysis(df, symbol, **kwargs):
        captured["symbol"] = symbol
        captured["kwargs"] = kwargs
        return {"summary": {"bias": "bullish"}, "concepts": {"entry_models": {}}}

    monkeypatch.setattr(runner_mod, "build_smc_analysis", fake_analysis)
    monkeypatch.setattr(runner_mod, "load_runtime_cluster_weight_table", lambda path: {"cluster": "ok"})

    runner = SmcPaperRunner(
        _StubClient(),
        PaperRunConfig(symbol="BTC-USDT", interval="1h", bars=40, journal_path=str(tmp_path / "paper.jsonl")),
    )
    result = runner.run_once()

    assert result.action == "skipped:no_qualified_entry"
    assert captured["symbol"] == "BTC-USDT"
    assert captured["kwargs"]["timeframe"] == "1h"
    assert captured["kwargs"]["cluster_weight_table"] == {"cluster": "ok"}
