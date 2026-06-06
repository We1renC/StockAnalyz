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


def test_ledger_cache_hits_on_unchanged_mtime(tmp_path):
    """A3: second call with same mtime → cache hit, no re-read."""
    from learning.ledger_cache import _LedgerCache
    cache = _LedgerCache(max_entries=4, ttl_sec=60.0)
    f = tmp_path / "ledger.jsonl"
    f.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")

    calls = []
    def loader(path):
        calls.append(path)
        return [{"a": 1}, {"a": 2}]

    r1 = cache.get(str(f), loader)
    r2 = cache.get(str(f), loader)
    assert r1 == r2
    assert len(calls) == 1  # second call hit cache
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


def test_ledger_cache_invalidates_on_mtime_change(tmp_path):
    """A3: file rewritten → mtime changes → cache miss → re-read."""
    import os, time
    from learning.ledger_cache import _LedgerCache
    cache = _LedgerCache(max_entries=4, ttl_sec=60.0)
    f = tmp_path / "ledger.jsonl"
    f.write_text('{"a":1}\n', encoding="utf-8")

    calls = []
    def loader(path):
        calls.append(path)
        return [{"a": len(calls)}]

    cache.get(str(f), loader)
    # Force mtime advance (some filesystems coalesce ns; touch with future ts)
    future = time.time() + 5
    os.utime(f, (future, future))
    f.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    cache.get(str(f), loader)
    assert len(calls) == 2


def test_api_token_middleware_off_when_env_unset(monkeypatch):
    """A2: DASHBOARD_API_TOKEN unset → middleware is a no-op."""
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    from learning.api_auth import _token
    assert _token() == ""


def test_api_token_middleware_protects_smc_endpoints(monkeypatch):
    """A2: when token set, protected prefix without X-API-Token → 401."""
    import asyncio
    from learning.api_auth import api_token_middleware
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "secret123")

    class _Req:
        def __init__(self, path, token=None):
            self.url = type("U", (), {"path": path})()
            self.headers = {"x-api-token": token} if token else {}

    async def _call_next(req):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    # Wrong token → 401
    resp = asyncio.new_event_loop().run_until_complete(
        api_token_middleware(_Req("/api/smc-crypto/learning-curve", "wrong"), _call_next)
    )
    assert resp.status_code == 401
    # Correct token → passes through
    resp2 = asyncio.new_event_loop().run_until_complete(
        api_token_middleware(_Req("/api/smc-crypto/learning-curve", "secret123"), _call_next)
    )
    assert resp2.status_code == 200
    # Open path (no protected prefix) → passes through even without token
    resp3 = asyncio.new_event_loop().run_until_complete(
        api_token_middleware(_Req("/health"), _call_next)
    )
    assert resp3.status_code == 200


def test_file_lock_serializes_concurrent_appenders(tmp_path):
    """A1: two threads appending under locked_append must not interleave."""
    from learning.file_lock import locked_append
    import threading
    target = tmp_path / "ledger.jsonl"

    def writer(tag: str):
        for i in range(50):
            with locked_append(str(target)):
                # Each "logical record" spans 2 writes; without lock these
                # would interleave between threads.
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(f"{tag}-{i}-start\n")
                    fh.write(f"{tag}-{i}-end\n")

    t1 = threading.Thread(target=writer, args=("A",))
    t2 = threading.Thread(target=writer, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    lines = target.read_text().splitlines()
    # Every "start" line must be immediately followed by its "end" line.
    for i in range(0, len(lines), 2):
        assert lines[i].endswith("start"), f"interleaved at line {i}: {lines[i]}"
        assert lines[i + 1].endswith("end"), f"interleaved at line {i + 1}"
        # Same tag and same i within the pair
        assert lines[i].split("-")[:2] == lines[i + 1].split("-")[:2]


def test_cluster_ensemble_buckets_records_correctly():
    """P3-19: clustering by (model, symbol, interval, regime) creates
    distinct buckets."""
    from learning.cluster_ensemble import cluster_records
    recs = [
        {"model": "sweep_reversal", "symbol": "BTC-USDT", "interval": "1h",
         "regime": "trending", "outcome": "target", "r_multiple": 1.0},
        {"model": "sweep_reversal", "symbol": "BTC-USDT", "interval": "1h",
         "regime": "trending", "outcome": "stop", "r_multiple": -1.0},
        {"model": "ote_retracement", "symbol": "ETH-USDT", "interval": "15m",
         "regime": "ranging", "outcome": "target", "r_multiple": 1.5},
        # Unresolved → must be skipped
        {"model": "sweep_reversal", "symbol": "BTC-USDT", "interval": "1h",
         "regime": "trending", "outcome": "pending", "r_multiple": None},
    ]
    clusters = cluster_records(recs)
    assert ("sweep_reversal", "BTC-USDT", "1h", "trending") in clusters
    assert ("ote_retracement", "ETH-USDT", "15m", "ranging") in clusters
    assert len(clusters[("sweep_reversal", "BTC-USDT", "1h", "trending")]) == 2


def test_cluster_ensemble_lift_detects_anti_signal_factor():
    """A factor that's active mostly in losers → negative lift."""
    from learning.cluster_ensemble import build_cluster_weight_table
    recs = []
    # Active "ote_zone" → all losers (-1R each)
    for _ in range(8):
        recs.append({
            "model": "sweep_reversal", "symbol": "BTC", "interval": "1h",
            "regime": "ranging", "outcome": "stop", "r_multiple": -1.0,
            "factors_active": ["ote_zone", "htf_bias_aligned"],
        })
    # Inactive "ote_zone" → all winners (+1R each)
    for _ in range(8):
        recs.append({
            "model": "sweep_reversal", "symbol": "BTC", "interval": "1h",
            "regime": "ranging", "outcome": "target", "r_multiple": 1.0,
            "factors_active": ["htf_bias_aligned"],
        })
    table = build_cluster_weight_table(recs, factors=["ote_zone"], min_samples=10)
    k = ("sweep_reversal", "BTC", "1h", "ranging")
    assert k in table
    lift = table[k]["factors"]["ote_zone"]["lift"]
    # Active mean = -1, inactive mean = +1 → lift = -2
    assert lift == -2.0


def test_resolve_cluster_weights_respects_confidence_threshold():
    """Only override weights when cluster has enough samples."""
    from learning.cluster_ensemble import resolve_cluster_weights
    k = ("sweep_reversal", "BTC", "1h", "ranging")
    # Cluster A: too small (n_total=15 < 30) → no override
    table_small = {k: {"n_total": 15, "mean_R": -1.0,
                        "factors": {"ote_zone": {"lift": -2.0, "n_active": 8, "n_inactive": 7}}}}
    base = {"ote_zone": 1, "htf_bias_aligned": 2}
    out = resolve_cluster_weights(table_small, cluster=k, base_weights=base)
    assert out == base
    # Cluster B: enough samples + strong negative lift → nudge down
    table_big = {k: {"n_total": 50, "mean_R": -0.5,
                       "factors": {"ote_zone": {"lift": -2.0, "n_active": 25, "n_inactive": 25}}}}
    out2 = resolve_cluster_weights(table_big, cluster=k, base_weights=base)
    assert out2["ote_zone"] == 0  # 1 - 1
    assert out2["htf_bias_aligned"] == 2  # untouched


def test_resolve_cluster_weights_skips_weak_lift():
    """Lift below threshold → no override even with enough samples."""
    from learning.cluster_ensemble import resolve_cluster_weights
    k = ("m", "s", "i", "r")
    table = {k: {"n_total": 100, "mean_R": 0.1,
                  "factors": {"ote_zone": {"lift": 0.05, "n_active": 50, "n_inactive": 50}}}}
    base = {"ote_zone": 1}
    out = resolve_cluster_weights(table, cluster=k, base_weights=base,
                                     lift_threshold=0.15)
    assert out["ote_zone"] == 1  # unchanged


def test_hyperparameter_sweep_picks_best_sharpe_cell():
    """P3-18 sweep: when one cell clearly dominates, sweep picks it."""
    from learning.hyperparameter_sweep import sweep_hyperparameters
    records = []
    # High-score winners: varied positive r → real Sharpe > 0
    for r in (1.5, 2.0, 1.0, 1.2, 0.8, 1.7, 1.3) * 5:
        records.append({"confluence_score": 9, "rr": 2.5, "r_multiple": r})
    # Low-score losers: alternating big losses and small wins (mean negative)
    for r in (-1.0, -1.2, 0.3, -0.8, -1.5, 0.2, -1.0) * 5:
        records.append({"confluence_score": 6, "rr": 1.5, "r_multiple": r})
    sweep = sweep_hyperparameters(records, min_trades=10)
    assert sweep["status"] == "ok"
    best = sweep["best"]
    # Best should filter out the losers either via min_score or min_rr.
    # The loser group has score=6, rr=1.5 — at least one of these must
    # be above its respective floor in the picked cell.
    excludes_losers = best["min_score"] >= 7 or best["min_rr"] >= 2.0
    assert excludes_losers
    assert best["score"]["mean"] > 0
    assert best["score"]["sharpe"] > 0.5


def test_hyperparameter_sweep_returns_insufficient_data():
    """Below min_trades → no recommendation."""
    from learning.hyperparameter_sweep import sweep_hyperparameters
    sweep = sweep_hyperparameters([], min_trades=20)
    assert sweep["status"] == "insufficient_data"
    assert sweep["best"] is None


def test_should_apply_recommendation_requires_sharpe_improvement():
    """Conservative gate: only apply when Sharpe improves by ≥ threshold."""
    from learning.hyperparameter_sweep import should_apply_recommendation
    sweep_ok = {
        "status": "ok",
        "best": {"min_score": 8, "min_rr": 2.0, "risk_pct": 1.0,
                  "score": {"sharpe": 0.25, "n_trades": 30}},
    }
    # Current Sharpe 0.22 → delta 0.03, below 0.1 threshold → not applied
    rec = should_apply_recommendation(sweep_ok, current={"sharpe": 0.22})
    assert rec["apply"] is False
    assert rec["reason"] == "improvement_below_threshold"
    # Current 0.0 → delta 0.25 → applied
    rec2 = should_apply_recommendation(sweep_ok, current={"sharpe": 0.0})
    assert rec2["apply"] is True


def test_should_apply_recommendation_rejects_negative_absolute_sharpe():
    """Even with big improvement, refuse to apply if absolute Sharpe < floor."""
    from learning.hyperparameter_sweep import should_apply_recommendation
    sweep = {
        "status": "ok",
        "best": {"min_score": 8, "min_rr": 2.0, "risk_pct": 1.0,
                  "score": {"sharpe": 0.05, "n_trades": 30}},
    }
    # Improvement is 0.05 - (-1.0) = 1.05 (huge) but absolute Sharpe is 0.05 < 0.2
    rec = should_apply_recommendation(sweep, current={"sharpe": -1.0})
    assert rec["apply"] is False
    assert rec["reason"] == "best_sharpe_below_absolute_floor"


def test_build_smc_analysis_applies_mae_mfe_calibration_to_all_models():
    """P2-12+ regression: when caller passes mae_mfe_calibration into
    build_smc_analysis, every model's entry list gets calibrated *before*
    the picker sees RR — not after."""
    from learning.mae_mfe_calibration import apply_calibration_to_entry
    # Simulate the inner loop directly — full build_smc_analysis needs
    # 200+ bars of OHLCV and would over-couple this test to bias state.
    entries = {
        "sweep_reversal": [{
            "model": "sweep_reversal", "direction": 1,
            "entry": 100.0, "stop": 98.0, "target": 104.0,
            "rr": 2.0,
        }],
        "ote_retracement": [{
            "model": "ote_retracement", "direction": -1,
            "entry": 100.0, "stop": 102.0, "target": 96.0,
            "rr": 2.0,
        }],
    }
    cal = {
        ("sweep_reversal", 1): {"stop_R": 1.5, "target_R": 3.0, "n_winners": 12},
        ("ote_retracement", -1): {"stop_R": 1.2, "target_R": 2.5, "n_winners": 9},
    }
    for lst in entries.values():
        for e in lst:
            apply_calibration_to_entry(e, cal)
    sw = entries["sweep_reversal"][0]
    assert sw["calibration_applied"]["source"] == "per_model_mae_mfe"
    # Stop widened from 98 → entry - 1.5*risk = 100 - 1.5*2 = 97
    assert sw["stop"] == 97.0
    # Target = 100 + 3.0*2 = 106
    assert sw["target"] == 106.0
    # RR = |target-entry|/new_risk = 6 / 3 = 2.0 (target_R 3 / stop_R 1.5)
    assert sw["rr"] == 2.0


def test_apply_mae_mfe_calibration_is_idempotent_on_picked_entry():
    """P2-12+ runner._apply_mae_mfe_calibration must skip already-calibrated
    entries so we don't double-widen the stop."""
    entry = {
        "model": "sweep_reversal", "direction": 1,
        "entry": 100.0, "stop": 98.0, "target": 104.0,
        "calibration_applied": {"source": "per_model_mae_mfe",
                                  "stop_widen_R": 1.5, "target_take_R": 3.0,
                                  "n_winners": 12, "model": "sweep_reversal",
                                  "direction": 1},
    }
    # Build minimal runner-like object with only the method under test
    class _Stub:
        config = type("C", (), {"journal_path": "/dev/null"})()
        _build_mae_mfe_table = lambda self: {("sweep_reversal", 1): {
            "stop_R": 9.9, "target_R": 9.9, "n_winners": 99,
        }}
    from smc_paper_runner import SmcPaperRunner
    SmcPaperRunner._apply_mae_mfe_calibration(_Stub(), entry)
    # Stop NOT widened a second time
    assert entry["stop"] == 98.0


def test_merge_detector_extras_lets_learned_weights_survive():
    """P0-3+ regression: hardcoded detector extras must NOT clobber
    learned overrides stored in CONFLUENCE_WEIGHTS_DEFAULT."""
    from smc_quant import _merge_detector_extras
    import smc_quant
    saved = dict(smc_quant.CONFLUENCE_WEIGHTS_DEFAULT)
    smc_quant.CONFLUENCE_WEIGHTS_DEFAULT["killzone_premium"] = 3
    try:
        merged = _merge_detector_extras(None, {
            "killzone_premium": 1,
            "pd_extreme": 1,
            "nearest_poi_within": 1,
        })
        # Learned value (3) MUST survive — old code would force back to 1.
        assert merged["killzone_premium"] == 3
        # Untouched extras still default to 1.
        assert merged["pd_extreme"] == 1
        assert merged["nearest_poi_within"] == 1
    finally:
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.clear()
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.update(saved)


def test_merge_detector_extras_caller_weights_win_over_learned():
    """Caller-supplied weights override even learned defaults."""
    from smc_quant import _merge_detector_extras
    import smc_quant
    saved = dict(smc_quant.CONFLUENCE_WEIGHTS_DEFAULT)
    smc_quant.CONFLUENCE_WEIGHTS_DEFAULT["killzone_premium"] = 3
    try:
        merged = _merge_detector_extras(
            {"killzone_premium": 5},
            {"killzone_premium": 1, "pd_extreme": 1, "nearest_poi_within": 1},
        )
        # Caller's 5 beats learned 3 beats extras 1.
        assert merged["killzone_premium"] == 5
    finally:
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.clear()
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.update(saved)


def test_recent_30d_pnl_gate_passes_when_total_R_above_threshold():
    """P3-17 Gate 1: net 30d R-multiple ≥ min → pass."""
    from learning.real_pnl_gates import recent_30d_real_pnl_gate
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    records = [
        {"entry_time": (now - timedelta(days=15)).isoformat(),
         "outcome": "target", "r_multiple": 1.0} for _ in range(5)
    ] + [
        {"entry_time": (now - timedelta(days=10)).isoformat(),
         "outcome": "stop", "r_multiple": -1.0} for _ in range(3)
    ]
    # Net = 5*1 - 3*1 = 2.0 R > 0.5
    g = recent_30d_real_pnl_gate(records, min_total_R=0.5)
    assert g["passed"] is True
    assert g["metric"] == 2.0


def test_recent_30d_pnl_gate_fails_on_starvation():
    """30d flat-line / fee-bleed (net ≈ 0) → fails."""
    from learning.real_pnl_gates import recent_30d_real_pnl_gate
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    records = [
        {"entry_time": (now - timedelta(days=2)).isoformat(),
         "outcome": "target", "r_multiple": 0.05} for _ in range(3)
    ]
    g = recent_30d_real_pnl_gate(records, min_total_R=0.5)
    assert g["passed"] is False
    assert g["metric"] == 0.15


def test_max_drawdown_gate_catches_consecutive_losses():
    """5 consecutive -1R losses → DD=5; threshold 4 → fail."""
    from learning.real_pnl_gates import max_drawdown_30d_gate
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    records = []
    # Climb +5R
    for i in range(5):
        records.append({
            "entry_time": (now - timedelta(days=20 - i)).isoformat(),
            "outcome": "target", "r_multiple": 1.0,
        })
    # Then drop -5R
    for i in range(5):
        records.append({
            "entry_time": (now - timedelta(days=15 - i)).isoformat(),
            "outcome": "stop", "r_multiple": -1.0,
        })
    g = max_drawdown_30d_gate(records, max_drawdown_R=4.0)
    assert g["passed"] is False
    assert g["metric"] == 5.0


def test_live_vs_backtest_correlation_passes_on_aligned_pools():
    """Bt and live agree per-cluster → high correlation."""
    from learning.real_pnl_gates import live_vs_backtest_correlation_gate
    records = []
    # Three models, each has consistent E[R] in both pools
    for m, er in [("sweep_reversal", 1.5), ("ote_retracement", 0.5), ("unicorn", -0.5)]:
        for _ in range(5):
            records.append({"source": "backtest", "model": m, "symbol": "BTC-USDT",
                              "outcome": "x", "r_multiple": er})
            records.append({"source": "paper", "model": m, "symbol": "BTC-USDT",
                              "outcome": "x", "r_multiple": er * 0.9})
    g = live_vs_backtest_correlation_gate(records, min_correlation=0.5)
    assert g["passed"] is True
    assert g["metric"] >= 0.5


def test_live_vs_backtest_correlation_fails_when_pools_disagree():
    """Backtest says X is best but live says X is worst → low correlation."""
    from learning.real_pnl_gates import live_vs_backtest_correlation_gate
    records = []
    # Backtest: a=2, b=1, c=0; Live: a=0, b=1, c=2 (perfectly negative correlation)
    for er_bt, er_live, m in [(2.0, 0.0, "a"), (1.0, 1.0, "b"), (0.0, 2.0, "c")]:
        for _ in range(5):
            records.append({"source": "backtest", "model": m, "symbol": "X",
                              "outcome": "x", "r_multiple": er_bt})
            records.append({"source": "paper", "model": m, "symbol": "X",
                              "outcome": "x", "r_multiple": er_live})
    g = live_vs_backtest_correlation_gate(records, min_correlation=0.3)
    assert g["passed"] is False
    assert g["metric"] < 0.3


def test_run_real_pnl_gates_aggregates_all_three():
    """Convenience helper returns flat dict + overall pass + failures list."""
    from learning.real_pnl_gates import run_real_pnl_gates
    out = run_real_pnl_gates([])
    assert "all_passed" in out
    assert set(out["gates"].keys()) == {
        "recent_30d_real_pnl",
        "live_vs_backtest_correlation",
        "max_drawdown_30d",
    }


def test_edge_decay_trail_writes_alert_to_sqlite(tmp_path, monkeypatch):
    """P1-8+: edge decay emit a record_alert_delivery row."""
    import sqlite3 as _sqlite3
    from smc_training_loop import _emit_edge_decay_trail
    from paper_acceptance_metrics import (
        ensure_paper_acceptance_metrics_schema, load_alert_deliveries,
    )

    # Force vault to a non-existent path so the Obsidian branch is a no-op
    monkeypatch.setattr(
        "llm_providers.load_settings",
        lambda: {"obsidian_vault_path": ""},
        raising=False,
    )

    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    ensure_paper_acceptance_metrics_schema(conn)

    decay = {
        "is_decaying": True,
        "warning_message": "Recent expectancy crashed",
        "overall_expectancy": 1.5,
        "recent_expectancy": -0.5,
        "overall_win_rate": 0.6,
        "recent_win_rate": 0.2,
    }
    _emit_edge_decay_trail(
        conn, symbol="BTC-USDT", decay=decay, new_mode="VALIDATING_PROBE",
    )
    rows = load_alert_deliveries(conn, symbol="BTC-USDT", limit=10)
    assert len(rows) >= 1
    found = [r for r in rows if r.get("event_type") == "edge_decay_demotion"]
    assert found
    assert found[0]["severity"] == "warning"


def test_edge_decay_trail_writes_obsidian_note_when_vault_set(tmp_path, monkeypatch):
    """When vault path is configured → SMC/EdgeDecay/<sym>_<ts>.md is written."""
    import sqlite3 as _sqlite3
    from smc_training_loop import _emit_edge_decay_trail
    from paper_acceptance_metrics import ensure_paper_acceptance_metrics_schema

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        "llm_providers.load_settings",
        lambda: {"obsidian_vault_path": str(vault)},
        raising=False,
    )
    conn = _sqlite3.connect(":memory:"); conn.row_factory = _sqlite3.Row
    ensure_paper_acceptance_metrics_schema(conn)

    decay = {
        "is_decaying": True, "warning_message": "decay test",
        "overall_expectancy": 1.0, "recent_expectancy": -0.5,
        "overall_win_rate": 0.6, "recent_win_rate": 0.3,
    }
    _emit_edge_decay_trail(
        conn, symbol="BTC-USDT", decay=decay, new_mode="VALIDATING_PROBE",
    )
    decay_dir = vault / "SMC" / "EdgeDecay"
    assert decay_dir.is_dir()
    notes = list(decay_dir.glob("BTC-USDT_*.md"))
    assert len(notes) >= 1
    content = notes[0].read_text(encoding="utf-8")
    assert "edge_decay" in content
    assert "VALIDATING_PROBE" in content


def test_learning_curve_bins_and_cumulates_correctly():
    """P3-20: 25 resolved trades / bin_size=10 → 3 bins (10,10,5)."""
    from learning.learning_curve import cumulative_curve
    from datetime import datetime, timezone, timedelta
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 25 trades alternating ±1R
    records = []
    for i in range(25):
        records.append({
            "entry_time": (base + timedelta(hours=i)).isoformat(),
            "outcome": "target" if i % 2 == 0 else "stop",
            "r_multiple": 1.0 if i % 2 == 0 else -1.0,
        })
    curve = cumulative_curve(records, bin_size=10)
    assert len(curve) == 3
    assert curve[0]["n_in_bin"] == 10
    assert curve[1]["n_in_bin"] == 10
    assert curve[2]["n_in_bin"] == 5 and curve[2].get("is_partial") is True
    # Cumulative count tracks correctly
    assert curve[-1]["cumulative_n"] == 25
    # Mean of alternating ±1 is ~0
    assert abs(curve[-1]["cumulative_E_R"]) < 0.1


def test_learning_velocity_detects_improving_trend():
    """Bins with rising cumulative E[R] → slope > 0, interpretation=improving."""
    from learning.learning_curve import learning_velocity
    curve = [
        {"bin_idx": 1, "cumulative_E_R": 0.10},
        {"bin_idx": 2, "cumulative_E_R": 0.20},
        {"bin_idx": 3, "cumulative_E_R": 0.30},
        {"bin_idx": 4, "cumulative_E_R": 0.40},
    ]
    v = learning_velocity(curve, lookback_bins=3)
    assert v["slope"] > 0
    assert v["interpretation"] == "improving"


def test_learning_velocity_detects_stagnant_trend():
    from learning.learning_curve import learning_velocity
    curve = [
        {"bin_idx": 1, "cumulative_E_R": 0.5},
        {"bin_idx": 2, "cumulative_E_R": 0.501},
        {"bin_idx": 3, "cumulative_E_R": 0.499},
    ]
    v = learning_velocity(curve, lookback_bins=3)
    assert v["interpretation"] == "stagnant"


def test_samples_to_ready_extrapolates_from_recent_rate():
    """P3-20: 10 trades in 24h, target=30 → need 20 more, ETA ≈ 48h."""
    from learning.learning_curve import samples_to_ready
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    records = [
        {"entry_time": (now - timedelta(hours=h)).isoformat(),
         "outcome": "target", "r_multiple": 1.0}
        for h in range(10)
    ]
    eta = samples_to_ready(records, target_sample_size=30,
                             rate_lookback_hours=24.0)
    assert eta["current"] == 10
    assert eta["trades_needed"] == 20
    assert eta["rate_per_hour"] > 0
    # eta_hours = 20 / (10/24) = 48
    assert eta["eta_hours"] >= 40 and eta["eta_hours"] <= 60


def test_samples_to_ready_status_target_reached():
    """≥ target → no work needed."""
    from learning.learning_curve import samples_to_ready
    records = [{"outcome": "target", "r_multiple": 1.0,
                  "entry_time": "2026-01-01T00:00"} for _ in range(35)]
    eta = samples_to_ready(records, target_sample_size=30)
    assert eta["status"] == "target_reached"
    assert eta["trades_needed"] == 0


def test_missed_signals_reconciler_fills_outcome_when_target_hits(tmp_path):
    """P2-15+: future kline reaches target → outcome_at_5_bars='target'."""
    from smc_missed_signals_reconciler import reconcile_missed_signals
    from unittest.mock import MagicMock
    from datetime import datetime, timezone, timedelta

    path = tmp_path / "missed.jsonl"
    # 20 × 15min = 300min = 5h ; need logged ≥ 5h ago. Use 24h to be safe.
    logged = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rec = {
        "logged_at": logged, "symbol": "BTC-USDT", "interval": "15m",
        "direction": 1, "entry": 100.0, "stop": 99.0, "target": 102.0,
        "bias": "bullish", "model": "x", "score": 6,
    }
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    # Fabricate bars starting at logged_at; 4th bar hits target
    base = datetime.now(timezone.utc) - timedelta(hours=24)
    bars = []
    for i in range(10):
        ts = (base + timedelta(minutes=15 * i)).isoformat(timespec="seconds").replace("+00:00", "Z")
        if i == 3:
            bars.append({"open_time": ts, "high": "102.5", "low": "100.0", "open": "100", "close": "102.3"})
        else:
            bars.append({"open_time": ts, "high": "100.5", "low": "99.8", "open": "100", "close": "100.2"})

    api = MagicMock()
    api.klines.return_value = {"status": 200, "payload": {"data": bars}}
    res = reconcile_missed_signals(api, str(path), interval="15m")
    assert res.matched == 1
    # Read back the resolved row
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert rows[0]["outcome_at_5_bars"] == "target"
    # MAE accumulates over pre-breakout bars; on breakout we exit. The
    # sideways high (100.5) gives 0.5R favorable before target hit.
    assert rows[0]["max_favorable_R"] >= 0.5


def test_missed_signals_reconciler_skips_too_young(tmp_path):
    """Logged just now → not enough future bars → skipped_too_young."""
    from smc_missed_signals_reconciler import reconcile_missed_signals
    from unittest.mock import MagicMock
    from datetime import datetime, timezone

    path = tmp_path / "missed.jsonl"
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps({
        "logged_at": fresh, "symbol": "BTC-USDT",
        "direction": 1, "entry": 100, "stop": 99, "target": 102,
    }) + "\n", encoding="utf-8")
    api = MagicMock()
    api.klines.return_value = {"status": 200, "payload": {"data": []}}
    res = reconcile_missed_signals(api, str(path), interval="15m")
    assert res.skipped_too_young == 1
    assert res.matched == 0


def test_missed_signals_reconciler_skips_already_resolved(tmp_path):
    """Already-filled rows are not re-processed."""
    from smc_missed_signals_reconciler import reconcile_missed_signals
    from unittest.mock import MagicMock
    from datetime import datetime, timezone, timedelta
    path = tmp_path / "missed.jsonl"
    path.write_text(json.dumps({
        "logged_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "symbol": "BTC-USDT", "direction": 1,
        "entry": 100, "stop": 99, "target": 102,
        "outcome_at_20_bars": "target",   # already resolved
        "max_favorable_R": 2.0,
    }) + "\n", encoding="utf-8")
    api = MagicMock()
    res = reconcile_missed_signals(api, str(path), interval="15m")
    assert res.skipped_done == 1
    assert res.matched == 0


def test_exploration_size_multiplier_shrinks_crypto_qty(tmp_path):
    """P2-14+: entry with exploration_size_multiplier=0.20 → qty × 0.20."""
    from smc_paper_runner import SmcPaperRunner, PaperRunConfig
    from unittest.mock import MagicMock

    api = MagicMock()
    api.ticker.return_value = {"status": 200, "payload": {"price": "100"}}
    cfg = PaperRunConfig(
        symbol="BTC-USDT",
        journal_path=str(tmp_path / "j.jsonl"),
        max_notional_usdt=1000.0,
    )
    runner = SmcPaperRunner(api, cfg)
    # Normal entry: risk_amount=100 / stop_distance=1 → 100 qty before cap
    normal_entry = {"direction": 1, "entry": 100.0, "stop": 99.0}
    sizing = {"risk_amount": 100.0, "stop_distance": 1.0}
    p_normal = runner._build_order_payload(normal_entry, sizing, "cid-normal")
    q_normal = float(p_normal["quantity"])
    # Exploration probe: same entry but 20% size
    exp_entry = dict(normal_entry, exploration_size_multiplier=0.20)
    p_exp = runner._build_order_payload(exp_entry, sizing, "cid-exp")
    q_exp = float(p_exp["quantity"])
    # The exploration quantity must be smaller (≤ 25% of normal — both qty
    # and cap halved).
    assert q_exp < q_normal
    assert q_exp <= q_normal * 0.25 + 1e-6


def test_time_decay_weights_decay_to_50pct_at_half_life():
    """P3-21: trade at exactly 1 half-life gets weight = 0.5."""
    from learning.time_decay import compute_decay_weights
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    records = [
        {"entry_time": now.isoformat()},                              # weight ≈ 1.0
        {"entry_time": (now - timedelta(days=30)).isoformat()},       # weight ≈ 0.5
        {"entry_time": (now - timedelta(days=60)).isoformat()},       # weight ≈ 0.25
        {"entry_time": (now - timedelta(days=90)).isoformat()},       # weight ≈ 0.125
    ]
    ws = compute_decay_weights(records, half_life_days=30, now=now)
    assert abs(ws[0] - 1.0) < 0.01
    assert abs(ws[1] - 0.5) < 0.01
    assert abs(ws[2] - 0.25) < 0.01
    assert abs(ws[3] - 0.125) < 0.01


def test_time_decay_safe_when_no_timestamp():
    """Records without entry_time get neutral weight 1.0, not crash."""
    from learning.time_decay import compute_decay_weights
    ws = compute_decay_weights([{}, {"entry_time": "broken-iso"}], half_life_days=30)
    assert ws == [1.0, 1.0]


def test_weighted_expectancy_favours_recent_trades():
    """A regime shift: old +R, recent -R → naive E[R] > 0 but weighted < naive."""
    from learning.time_decay import weighted_expectancy
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    records = [
        # 30 old winners
        *[{"entry_time": (now - timedelta(days=120)).isoformat(),
           "r_multiple": 2.0, "outcome": "target"} for _ in range(30)],
        # 10 recent losers
        *[{"entry_time": (now - timedelta(hours=12)).isoformat(),
           "r_multiple": -1.0, "outcome": "stop"} for _ in range(10)],
    ]
    out = weighted_expectancy(records, half_life_days=30, now=now)
    # Naive: (30*2 + 10*-1) / 40 = 1.25
    assert abs(out["naive_expectancy"] - 1.25) < 0.01
    # Weighted: recent -R should drag below naive
    assert out["weighted_expectancy"] < out["naive_expectancy"]
    # ESS captures information shrinkage from skewed weights
    assert out["effective_sample_size"] < out["n"]


def test_split_active_vs_stale_buckets_by_threshold():
    """Trade > 3.3 half-lives old → weight < 0.10 → stale."""
    from learning.time_decay import split_active_vs_stale
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    records = [
        {"entry_time": now.isoformat()},
        {"entry_time": (now - timedelta(days=30)).isoformat()},
        {"entry_time": (now - timedelta(days=200)).isoformat()},   # very stale
    ]
    out = split_active_vs_stale(records, half_life_days=30, decay_threshold=0.10, now=now)
    assert out["active_count"] == 2
    assert out["stale_count"] == 1


def test_adaptive_cooldown_doubles_after_loss_streak():
    """P2-16: 3 連敗 → cooldown ×2"""
    from smc_auto_workflow import _adaptive_cooldown_multiplier
    assert _adaptive_cooldown_multiplier(["loss", "loss", "loss"]) == 2.0
    assert _adaptive_cooldown_multiplier(["loss", "loss", "loss", "win"]) == 2.0  # only first 3 count


def test_adaptive_cooldown_halves_after_win_streak():
    """P2-16: 3 連勝 → cooldown ×0.5"""
    from smc_auto_workflow import _adaptive_cooldown_multiplier
    assert _adaptive_cooldown_multiplier(["win", "win", "win"]) == 0.5


def test_adaptive_cooldown_mixed_returns_baseline():
    from smc_auto_workflow import _adaptive_cooldown_multiplier
    assert _adaptive_cooldown_multiplier(["win", "loss", "win"]) == 1.0
    assert _adaptive_cooldown_multiplier(["loss"]) == 1.0   # too few
    assert _adaptive_cooldown_multiplier([]) == 1.0


def test_exploration_blocked_outside_ready_state():
    """ε-greedy must NEVER fire in LEARNING / VALIDATING / PAUSED."""
    from learning.exploration import decide_exploration
    candidates = [{"confluence": {"score": 7}, "rr": 2.0, "model": "x", "direction": 1}]
    for bad in ("LEARNING", "VALIDATING_PROBE", "PAUSED", "LOCKED", "DRY_RUN"):
        d = decide_exploration(
            all_entries=candidates, min_confluence_score=8,
            state=bad, symbol="BTC-USDT", boundary_sample_count=0,
            rng_seed="force-trigger",   # would normally trigger but state blocks
        )
        assert d.is_exploration is False
        assert "state_blocks_exploration" in d.reason


def test_exploration_picks_boundary_band_candidate():
    """Eligible band = [min-2, min). Only entries in band qualify."""
    from learning.exploration import decide_exploration
    candidates = [
        {"confluence": {"score": 9}, "rr": 2.0, "model": "a", "direction": 1},   # above threshold, ignored
        {"confluence": {"score": 5}, "rr": 2.0, "model": "b", "direction": 1},   # below band
        {"confluence": {"score": 7}, "rr": 2.0, "model": "c", "direction": 1},   # IN BAND (min=8, band=[6,8))
    ]
    # rng_seed crafted to ensure ε triggers (deterministic_random < 0.05)
    # We force ε=1.0 to guarantee trigger for the test
    d = decide_exploration(
        all_entries=candidates, min_confluence_score=8,
        state="READY", symbol="BTC-USDT", boundary_sample_count=0,
        base_epsilon=1.0,
    )
    assert d.is_exploration is True
    assert d.chosen_entry["model"] == "c"
    assert d.chosen_entry["is_exploration"] is True
    assert d.chosen_entry["exploration_size_multiplier"] == 0.20
    assert d.size_multiplier == 0.20


def test_exploration_halves_epsilon_after_enough_samples():
    """boundary_sample_count ≥ 20 → effective ε halves."""
    from learning.exploration import decide_exploration
    candidates = [{"confluence": {"score": 7}, "rr": 2.0, "model": "c", "direction": 1}]
    d_few = decide_exploration(
        all_entries=candidates, min_confluence_score=8,
        state="READY", symbol="BTC-USDT", boundary_sample_count=5,
        base_epsilon=0.05,
    )
    d_many = decide_exploration(
        all_entries=candidates, min_confluence_score=8,
        state="READY", symbol="BTC-USDT", boundary_sample_count=25,
        base_epsilon=0.05,
    )
    assert d_few.epsilon_used == 0.05
    assert d_many.epsilon_used == 0.025


def test_exploration_no_op_when_no_boundary_candidate():
    """ε triggers but no entry in band → return is_exploration=False."""
    from learning.exploration import decide_exploration
    candidates = [
        {"confluence": {"score": 10}, "rr": 2.0, "model": "x", "direction": 1},
        {"confluence": {"score": 3}, "rr": 2.0, "model": "y", "direction": 1},
    ]
    d = decide_exploration(
        all_entries=candidates, min_confluence_score=8,
        state="READY", symbol="BTC-USDT", boundary_sample_count=0,
        base_epsilon=1.0,
    )
    assert d.is_exploration is False
    assert d.reason == "no_boundary_candidate_available"


def test_count_exploration_trades_isolates_source_tag():
    """Attribution must be able to separate exploration P&L."""
    from learning.exploration import count_exploration_trades
    records = [
        {"source": "backtest", "r_multiple": 2.0},
        {"source": "exploration", "r_multiple": -1.0, "symbol": "BTC-USDT"},
        {"is_exploration": True, "r_multiple": 1.5, "symbol": "BTC-USDT"},
        {"source": "exploration", "r_multiple": 0.5, "symbol": "ETH-USDT"},
    ]
    assert count_exploration_trades(records) == 3
    assert count_exploration_trades(records, symbol="BTC-USDT") == 2


def test_slippage_distribution_computes_per_symbol_side_percentiles():
    """P2-13: fills with known offset → percentile bucket correctly."""
    from learning.slippage_model import estimate_slippage_distribution

    submitted = {"ord1": 100.0, "ord2": 100.0, "ord3": 100.0,
                  "ord4": 100.0, "ord5": 100.0, "ord6": 100.0,
                  "ord7": 100.0, "ord8": 100.0}
    # Buy fills filled at 100.05 → +5 bps slip (worse than submitted)
    fills = [
        {"order_id": f"ord{i}", "symbol": "BTC-USDT",
         "side": "buy", "price": "100.05"}
        for i in range(1, 9)
    ]
    dist = estimate_slippage_distribution(fills, submitted)
    assert ("BTC-USDT", "buy") in dist
    b = dist[("BTC-USDT", "buy")]
    assert b["n"] == 8
    assert abs(b["p50_bps"] - 5.0) < 0.5
    assert abs(b["mean_bps"] - 5.0) < 0.5


def test_slippage_sampler_falls_back_when_no_data():
    """No fills for (symbol, side) → return default_bps."""
    from learning.slippage_model import build_slippage_sampler
    s = build_slippage_sampler({}, default_bps=7.5)
    assert s("BTC-USDT", "buy") == 7.5
    assert s("ETH-USDT", "sell") == 7.5


def test_slippage_sampler_uses_p75_when_sufficient_history():
    """≥8 fills → sampler returns P75 (pessimistic), not default."""
    from learning.slippage_model import build_slippage_sampler
    dist = {("BTC-USDT", "buy"): {
        "n": 20, "p50_bps": 4.0, "p75_bps": 8.5,
        "p90_bps": 12.0, "max_bps": 20.0, "mean_bps": 5.5,
    }}
    s = build_slippage_sampler(dist, default_bps=5.0, min_samples_for_real=8)
    assert s("BTC-USDT", "buy") == 8.5
    # Different symbol → fallback
    assert s("ETH-USDT", "buy") == 5.0


def test_slippage_signs_buy_vs_sell_correctly():
    """Buy filled above submitted = +slip; sell filled below = +slip too."""
    from learning.slippage_model import estimate_slippage_distribution
    submitted = {"ord_buy": 100.0, "ord_sell": 100.0}
    fills = [
        {"order_id": "ord_buy", "symbol": "X", "side": "buy", "price": "100.10"},   # +10 bps
        {"order_id": "ord_sell", "symbol": "X", "side": "sell", "price": "99.90"},  # +10 bps
    ]
    dist = estimate_slippage_distribution(fills, submitted)
    assert abs(dist[("X", "buy")]["p50_bps"] - 10.0) < 0.1
    assert abs(dist[("X", "sell")]["p50_bps"] - 10.0) < 0.1


def test_mae_mfe_calibration_builds_per_model_table():
    """P2-12: ≥8 winners per (model, dir) → table emits stop/target multipliers."""
    from learning.mae_mfe_calibration import build_model_calibration_table
    records = []
    # 10 sweep_reversal LONG winners, MAE around 0.5-0.8R, MFE around 1.5-3R
    for i in range(10):
        records.append({
            "model": "sweep_reversal", "direction": 1,
            "outcome": "target", "r_multiple": 2.0,
            "mae": -(0.5 + 0.05 * i), "mfe": 1.5 + 0.2 * i,
        })
    table = build_model_calibration_table(records, min_winners=8)
    assert ("sweep_reversal", 1) in table
    info = table[("sweep_reversal", 1)]
    assert info["n_winners"] >= 8
    assert info["stop_R"] >= 1.0   # never tightens below structural floor
    assert info["target_R"] >= 1.5
    assert info["target_R"] <= info["p90_mfe_R"]


def test_mae_mfe_calibration_skips_under_min_winners():
    from learning.mae_mfe_calibration import build_model_calibration_table
    records = [{
        "model": "unicorn", "direction": -1, "outcome": "target",
        "r_multiple": 2.0, "mae": -0.4, "mfe": 2.5,
    }] * 5
    table = build_model_calibration_table(records, min_winners=8)
    assert ("unicorn", -1) not in table   # 5 < 8 → skipped


def test_apply_calibration_widens_stop_and_takes_at_p50_mfe():
    """Apply rewrites entry.stop / entry.target / entry.rr."""
    from learning.mae_mfe_calibration import apply_calibration_to_entry
    entry = {"model": "sweep_reversal", "direction": 1,
              "entry": 100.0, "stop": 99.0, "target": 102.0}
    cal = {("sweep_reversal", 1): {
        "stop_R": 1.5, "target_R": 3.0, "n_winners": 12,
        "p75_mae_R": 1.5, "p50_mfe_R": 3.0, "p90_mfe_R": 4.0,
    }}
    apply_calibration_to_entry(entry, cal)
    # risk = 1; new stop = 100 - 1.5*1 = 98.5; new target = 100 + 3*1 = 103
    assert entry["stop"] == 98.5
    assert entry["target"] == 103.0
    # RR recalc against new wider stop
    assert entry["rr"] == 2.0
    assert entry["calibration_applied"]["source"] == "per_model_mae_mfe"
    assert entry["original_stop"] == 99.0


def test_apply_calibration_no_op_when_table_missing():
    """No data for this (model, dir) → entry untouched."""
    from learning.mae_mfe_calibration import apply_calibration_to_entry
    entry = {"model": "ote_retracement", "direction": 1,
              "entry": 100.0, "stop": 99.0, "target": 102.0}
    apply_calibration_to_entry(entry, {})
    assert entry["stop"] == 99.0   # unchanged
    assert entry["target"] == 102.0


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
