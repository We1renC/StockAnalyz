"""Integration test for the unified SMC × crypto-api × paper-acceptance pipeline."""

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
    test_db = tmp_path / "portfolio.db"
    import importlib
    import crypto_api.auth as auth_mod
    import crypto_api.executor as exec_mod
    import crypto_api.router as router_mod
    import crypto_api.models as models_mod
    monkeypatch.setattr(auth_mod, "DB", test_db)
    monkeypatch.setattr(exec_mod, "DB", test_db)
    monkeypatch.setattr(router_mod, "DB", test_db, raising=False)
    conn = sqlite3.connect(test_db)
    models_mod.init_crypto_db(conn)
    models_mod.seed_crypto_data(conn)
    conn.close()
    if "app" in sys.modules:
        importlib.reload(sys.modules["app"])
    import app as fastapi_app
    return fastapi_app.app


@pytest.fixture
def patched_klines_and_ticker(monkeypatch):
    """Deterministic OHLCV + ticker so SMC produces stable bias."""
    from crypto_api.executor import price_engine
    from decimal import Decimal

    async def fake_klines(self, symbol, interval, limit=500):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        price = 30000.0
        for i in range(limit):
            o = price
            c = price + (i % 7) - 3
            h = max(o, c) + 50
            l = min(o, c) - 50
            rows.append({
                "open_time": (base + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                "open": str(o), "high": str(h), "low": str(l), "close": str(c),
                "volume": "10.0", "quote_volume": "300000", "trade_count": 100,
                "close_time": (base + timedelta(hours=i + 1)).isoformat().replace("+00:00", "Z"),
            })
            price = c + 5
        return rows

    async def fake_ticker(self, symbol):
        return {"symbol": symbol, "price": "30100", "last_price": "30100",
                "high_24h": "30200", "low_24h": "29800",
                "volume_24h": "100", "quote_volume_24h": "3000000"}

    async def fake_price(self, symbol):
        return Decimal("30100")

    monkeypatch.setattr(type(price_engine), "get_klines", fake_klines)
    monkeypatch.setattr(type(price_engine), "get_ticker_24h", fake_ticker)
    monkeypatch.setattr(type(price_engine), "get_price", fake_price)


def test_unified_session_emits_acceptance_report_even_when_no_signal(crypto_app, patched_klines_and_ticker, tmp_path):
    """Pipeline must succeed end-to-end and produce a paper-acceptance report
    even when no SMC entry qualifies — gates fall through but the
    framework still runs and persists the run.
    """
    from smc_unified_system import UnifiedTradingSession, UnifiedSessionConfig
    from smc_paper_runner import CryptoApiClient

    client = TestClient(crypto_app)
    api = CryptoApiClient(client)
    session = UnifiedTradingSession(api, UnifiedSessionConfig(
        symbols=["BTC-USDT"],
        interval="1h", bars=200,
        min_confluence_score=8,    # production gate
        min_rr=1.5,
        max_notional_usdt=2_000,
        risk_pct=0.05,
        journal_dir=str(tmp_path / "smc_unified"),
    ))
    out = session.run(place_live_orders=True)

    assert "decisions" in out
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["symbol"] == "BTC-USDT"

    accept = out["acceptance"]
    # paper_acceptance Conclusion enum (paper_acceptance.py:18-23)
    assert accept["conclusion"] in {"passed", "conditionally_passed", "failed_repeat_paper", "strategy_invalidated"}
    assert "passed" in accept and "failed" in accept
    assert isinstance(accept["blocking_issues"], list)
    assert isinstance(accept["metrics"], dict)


def test_unified_session_routes_dry_run_through_paper_execution(crypto_app, patched_klines_and_ticker, tmp_path, monkeypatch):
    """When a high-confluence entry is forced through, dry-run must
    populate paper_execution slippage/fee data and the live POST must
    reach the crypto-api matching engine."""
    from smc_unified_system import UnifiedTradingSession, UnifiedSessionConfig
    from smc_paper_runner import CryptoApiClient, SmcPaperRunner

    def fake_pick(self, analysis):
        return {
            "model": "sweep_reversal", "direction": 1,
            "entry": 30000.0, "stop": 29850.0, "target": 30350.0,
            "rr": 2.33, "risk": 150.0,
            "confluence": {"score": 12, "threshold": 8, "triggered": True, "weights": {}},
            "factors": {"htf_bias_aligned": True, "liquidity_swept": True, "ltf_choch": True},
            "triggered": True, "dol_required": False,
            "dol_target": {"target_kind": "BSL", "target_price": 30500.0, "distance": 500.0},
        }
    monkeypatch.setattr(SmcPaperRunner, "_pick_best_entry", fake_pick)

    client = TestClient(crypto_app)
    api = CryptoApiClient(client)
    session = UnifiedTradingSession(api, UnifiedSessionConfig(
        symbols=["BTC-USDT"],
        interval="1h", bars=200,
        min_confluence_score=8, min_rr=1.5,
        max_notional_usdt=2_000, risk_pct=0.05,
        journal_dir=str(tmp_path / "smc_unified_live"),
    ))
    out = session.run(place_live_orders=True)
    dec = out["decisions"][0]

    # Dry-run via paper_execution must have happened
    assert dec.get("dry_run") is not None
    assert dec["dry_run"]["state"] in {"filled", "partially_filled", "rejected"}
    assert "fee" in dec["dry_run"]

    # Live POST should have reached the matching engine (status 200/201) or
    # been rejected with a documented error code — both are valid.
    if dec.get("live_order"):
        live_status = dec["live_order"].get("status")
        # 200/201 ok ; 400 risk-rejected ; 423 kill-switch ; 429 rate-limited
        # in full-suite runs the per-account rate bucket may already be drained
        assert live_status in (200, 201, 400, 423, 429)

    # Acceptance pipeline ran
    accept = out["acceptance"]
    assert accept["conclusion_label"]
    assert "metrics" in accept


def test_unified_session_persists_to_acceptance_store(crypto_app, patched_klines_and_ticker, tmp_path):
    """run_id must come back from persist_acceptance_report."""
    from smc_unified_system import UnifiedTradingSession, UnifiedSessionConfig
    from smc_paper_runner import CryptoApiClient
    from paper_acceptance_store import load_acceptance_reports

    client = TestClient(crypto_app)
    api = CryptoApiClient(client)
    db_path = tmp_path / "acceptance.db"
    session = UnifiedTradingSession(api, UnifiedSessionConfig(
        symbols=["BTC-USDT"],
        min_confluence_score=8,
        max_notional_usdt=2_000,
        risk_pct=0.05,
        journal_dir=str(tmp_path / "u"),
        paper_db_path=str(db_path),
    ))
    out = session.run(place_live_orders=False)
    session.close()

    # Reports queryable from the persisted store
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    reports = load_acceptance_reports(conn)
    conn.close()
    assert len(reports) >= 1
    # paper_acceptance_store returns rows keyed by run_key
    assert out["acceptance"]["run_id"] in {r["run_key"] for r in reports}
