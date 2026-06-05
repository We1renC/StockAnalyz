"""Regression tests for the P0/P1/P2 audit-driven improvements:

P0-1  persist_trade_records dedup
P0-2  reconcile_paper_trades resolves pending → target/stop/flat
P0-3  apply_strategy_yaml_overrides re-applied at startup
P1-7  trade record carries regime tagging
P1-9  ledger split per interval
P2-11 score → win_rate calibration
P2-15 missed signals logged to jsonl
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
sys.path.insert(0, str(WEB_DIR))


# ─────────────────────────────────────────────────────────────
# P0-1 dedup
# ─────────────────────────────────────────────────────────────

def test_persist_trade_records_skips_duplicates(tmp_path):
    from smc_quant import persist_trade_records

    rec = {"trade_id": "BTC-USDT:sweep:abc",
           "symbol": "BTC-USDT", "model": "sweep_reversal",
           "entry_price": 30000.0, "r_multiple": 2.0}
    path = tmp_path / "ledger.jsonl"

    # First write: 1 row
    n1 = persist_trade_records([rec], str(path), dedup=True)
    assert n1 == 1
    # Second write same record: 0 added
    n2 = persist_trade_records([rec], str(path), dedup=True)
    assert n2 == 0
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_persist_trade_records_dedup_false_preserves_old_behaviour(tmp_path):
    from smc_quant import persist_trade_records
    rec = {"trade_id": "X", "r_multiple": 1.0}
    path = tmp_path / "ledger.jsonl"
    persist_trade_records([rec], str(path), dedup=False)
    persist_trade_records([rec], str(path), dedup=False)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_persist_trade_records_dedup_falls_back_to_composite_key(tmp_path):
    from smc_quant import persist_trade_records
    # No trade_id → key built from (symbol, model, entry_time, entry_price)
    rec = {"symbol": "BTC-USDT", "model": "ote", "entry_time": "2026-06-05",
           "entry_price": 60000.0, "r_multiple": 1.0}
    path = tmp_path / "ledger.jsonl"
    persist_trade_records([rec], str(path), dedup=True)
    persist_trade_records([rec], str(path), dedup=True)
    persist_trade_records([dict(rec, entry_price=60001.0)], str(path), dedup=True)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    # 1 (original) + 1 (different price) = 2; the second duplicate is skipped
    assert len(lines) == 2


# ─────────────────────────────────────────────────────────────
# P0-2 reconciler
# ─────────────────────────────────────────────────────────────

def test_reconcile_resolves_target_when_price_hits(tmp_path):
    from smc_paper_reconciler import reconcile_paper_trades

    ledger = tmp_path / "ledger.jsonl"
    pending = {
        "trade_id": "BTC-USDT:client-1",
        "symbol": "BTC-USDT", "direction": 1, "outcome": "pending",
        "broker_order_id": "ord_abc", "client_order_id": "client-1",
        "plan_entry": 60000.0, "plan_stop": 59000.0, "plan_target": 62000.0,
        "rr_planned": 2.0,
        "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ledger.write_text(json.dumps(pending) + "\n", encoding="utf-8")

    api = MagicMock()
    api._request.return_value = {
        "status": 200,
        "payload": {"fills": [{
            "order_id": "ord_abc", "quantity": "0.05", "price": "60000",
        }]},
    }
    api.ticker.return_value = {
        "status": 200,
        "payload": {"price": "62500"},   # past target
    }
    res = reconcile_paper_trades(api, str(ledger), stale_minutes=60)
    assert res.matched == 1
    assert res.resolved_target == 1
    rows = ledger.read_text(encoding="utf-8").strip().splitlines()
    resolved = [json.loads(r) for r in rows if "target" in r]
    assert resolved and resolved[-1]["outcome"] == "target"
    assert resolved[-1]["r_multiple"] >= 2.0


def test_reconcile_resolves_stop_when_price_pierces(tmp_path):
    from smc_paper_reconciler import reconcile_paper_trades
    ledger = tmp_path / "ledger.jsonl"
    pending = {
        "trade_id": "BTC-USDT:client-2",
        "symbol": "BTC-USDT", "direction": 1, "outcome": "pending",
        "broker_order_id": "ord_xyz", "client_order_id": "client-2",
        "plan_entry": 60000.0, "plan_stop": 59000.0, "plan_target": 62000.0,
        "rr_planned": 2.0,
        "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ledger.write_text(json.dumps(pending) + "\n", encoding="utf-8")
    api = MagicMock()
    api._request.return_value = {
        "status": 200,
        "payload": {"fills": [{"order_id": "ord_xyz",
                                 "quantity": "0.05", "price": "60000"}]},
    }
    api.ticker.return_value = {"status": 200, "payload": {"price": "58500"}}
    res = reconcile_paper_trades(api, str(ledger))
    assert res.resolved_stop == 1


def test_reconcile_stale_no_fill_becomes_flat(tmp_path):
    from smc_paper_reconciler import reconcile_paper_trades
    ledger = tmp_path / "ledger.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    pending = {
        "trade_id": "BTC-USDT:client-stale",
        "symbol": "BTC-USDT", "direction": 1, "outcome": "pending",
        "broker_order_id": "ord_stale", "client_order_id": "client-stale",
        "plan_entry": 60000.0, "plan_stop": 59000.0, "plan_target": 62000.0,
        "rr_planned": 2.0, "entry_time": old,
    }
    ledger.write_text(json.dumps(pending) + "\n", encoding="utf-8")
    api = MagicMock()
    # NO fills
    api._request.return_value = {"status": 200, "payload": {"fills": []}}
    api.ticker.return_value = {"status": 200, "payload": {"price": "60500"}}
    res = reconcile_paper_trades(api, str(ledger), stale_minutes=60)
    assert res.resolved_flat == 1


# ─────────────────────────────────────────────────────────────
# P2-11 score calibration
# ─────────────────────────────────────────────────────────────

def test_score_calibration_bucket_returns_per_score_winrate():
    from learning.score_calibration import calibrate_score_to_winrate
    records = [
        {"confluence_score": 5, "outcome": "stop", "r_multiple": -1.0},
        {"confluence_score": 5, "outcome": "stop", "r_multiple": -1.0},
        {"confluence_score": 8, "outcome": "target", "r_multiple": 2.0},
        {"confluence_score": 8, "outcome": "stop", "r_multiple": -1.0},
        {"confluence_score": 8, "outcome": "target", "r_multiple": 2.0},
        {"confluence_score": 10, "outcome": "target", "r_multiple": 3.0},
        {"confluence_score": 10, "outcome": "target", "r_multiple": 3.0},
    ]
    cal = calibrate_score_to_winrate(records, method="bucket")
    table = {r["score"]: r["win_rate"] for r in cal["table"]}
    assert table[5] == 0.0
    assert abs(table[8] - 0.6667) < 0.01
    assert table[10] == 1.0


def test_score_calibration_isotonic_is_monotone():
    from learning.score_calibration import calibrate_score_to_winrate
    # Manufacture a non-monotone case; isotonic should pool to monotone
    records = (
        [{"confluence_score": 5, "outcome": "target", "r_multiple": 2.0}] * 6 +
        [{"confluence_score": 5, "outcome": "stop", "r_multiple": -1.0}] * 4 +
        [{"confluence_score": 8, "outcome": "stop", "r_multiple": -1.0}] * 8 +
        [{"confluence_score": 8, "outcome": "target", "r_multiple": 2.0}] * 2 +
        [{"confluence_score": 10, "outcome": "target", "r_multiple": 2.0}] * 9 +
        [{"confluence_score": 10, "outcome": "stop", "r_multiple": -1.0}] * 1
    )
    cal = calibrate_score_to_winrate(records, method="isotonic")
    wrs = [r["win_rate"] for r in sorted(cal["table"], key=lambda r: r["score"])]
    # Must be non-decreasing after PAV
    for a, b in zip(wrs, wrs[1:]):
        assert b >= a - 1e-6


def test_edge_decay_helper_detects_recent_negative_expectancy():
    """P1-8: when recent 20 trades go from +R to flat, helper raises is_decaying."""
    from smc_training_loop import _detect_recent_edge_decay
    from datetime import datetime, timedelta
    base = datetime(2026, 1, 1)
    # 30 historical winners (+1.5R) then 25 recent losses (-1R)
    records = []
    for i in range(30):
        records.append({
            "entry_time": (base + timedelta(hours=i)).isoformat(),
            "r_multiple": 1.5, "outcome": "target",
        })
    for i in range(30, 55):
        records.append({
            "entry_time": (base + timedelta(hours=i)).isoformat(),
            "r_multiple": -1.0, "outcome": "stop",
        })
    diag = _detect_recent_edge_decay(records, window_size=20)
    assert diag.get("is_decaying") is True
    assert diag.get("recent_expectancy", 0) <= 0


def test_edge_decay_helper_safe_when_no_resolved_records():
    """No resolved trades (all pending) → return False, not crash."""
    from smc_training_loop import _detect_recent_edge_decay
    diag = _detect_recent_edge_decay([{"outcome": "pending"} for _ in range(10)])
    assert diag.get("is_decaying") is False
    assert "insufficient" in (diag.get("warning_message") or "")


def test_min_score_for_target_returns_lowest_qualifying_bucket():
    from learning.score_calibration import calibrate_score_to_winrate
    records = (
        [{"confluence_score": 6, "outcome": "stop", "r_multiple": -1.0}] * 8 +
        [{"confluence_score": 9, "outcome": "target", "r_multiple": 2.0}] * 7 +
        [{"confluence_score": 9, "outcome": "stop", "r_multiple": -1.0}] * 3
    )
    cal = calibrate_score_to_winrate(records, method="bucket")
    # score 9: 7/10 = 0.7 wins → meets 0.55 target
    assert cal["min_score_for_target"](0.55) == 9
    # 0.9 not reached anywhere
    assert cal["min_score_for_target"](0.9) is None
