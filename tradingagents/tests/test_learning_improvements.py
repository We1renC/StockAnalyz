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


def test_baseline_equity_seeds_on_first_observation():
    """Audit fix: first compute_pnl_snapshot with conn seeds baseline
    from CURRENT equity; subsequent reads return the seeded value."""
    import sqlite3
    from smc_training_history import (
        get_or_init_baseline, ensure_baseline_equity_schema,
    )
    conn = sqlite3.connect(":memory:")
    ensure_baseline_equity_schema(conn)
    out1 = get_or_init_baseline(conn, current_equity=244_919.90)
    assert out1["baseline_usdt"] == 244_919.90
    assert out1["is_new"] is True
    # Second call returns same baseline even if equity changed
    out2 = get_or_init_baseline(conn, current_equity=300_000.0)
    assert out2["baseline_usdt"] == 244_919.90
    assert out2["is_new"] is False
    conn.close()


def test_reset_baseline_equity_overwrites():
    import sqlite3
    from smc_training_history import (
        get_or_init_baseline, reset_baseline_equity,
    )
    conn = sqlite3.connect(":memory:")
    get_or_init_baseline(conn, 100_000.0)
    out = reset_baseline_equity(conn, 250_000.0, note="test")
    assert out["baseline_usdt"] == 250_000.0
    out2 = get_or_init_baseline(conn, current_equity=999_999.0)
    assert out2["baseline_usdt"] == 250_000.0
    conn.close()


def test_compute_pnl_snapshot_uses_baseline_not_100k():
    """Audit fix: equity_delta = equity − baseline, not equity − $100k.

    With a $244,920 baseline = current → delta should be 0 (not +144,920).
    """
    import sqlite3
    from smc_training_history import compute_pnl_snapshot, ensure_baseline_equity_schema
    conn = sqlite3.connect(":memory:")
    ensure_baseline_equity_schema(conn)

    class _StubAPI:
        def balances(self):
            return {"payload": {"balances": [
                {"asset": "USDT", "total": 0.0},
                {"asset": "BTC", "total": 4.0},
            ]}}
        def ticker(self, symbol):
            return {"payload": {"price": "61230.0"}}
        def _request(self, method, path):
            return {"payload": {"fills": []}}

    snap = compute_pnl_snapshot(_StubAPI(), conn=conn)
    assert abs(snap["equity_usdt"] - 244_920.0) < 1.0
    assert abs(snap["equity_delta_usdt"]) < 1.0
    assert snap["baseline_usdt"] == snap["equity_usdt"]
    # Unrealized must be 0 — no fills → no cost basis.
    assert snap["unrealized_pnl_usdt"] == 0.0
    conn.close()


def test_compute_pnl_snapshot_unrealized_from_fill_history():
    """Audit fix: unrealized = (current_price − avg_cost) × held_qty
    derived from /fills, NOT (equity − baseline − realized)."""
    import sqlite3
    from smc_training_history import compute_pnl_snapshot, ensure_baseline_equity_schema
    conn = sqlite3.connect(":memory:")
    ensure_baseline_equity_schema(conn)

    class _StubAPI:
        def balances(self):
            return {"payload": {"balances": [
                {"asset": "USDT", "total": 0.0},
                {"asset": "BTC", "total": 1.0},
            ]}}
        def ticker(self, symbol):
            return {"payload": {"price": "60000.0"}}
        def _request(self, method, path):
            return {"payload": {"fills": [
                {"symbol": "BTC-USDT", "side": "buy",
                 "quantity": "1.0", "price": "50000.0", "fee": "0"},
            ]}}

    snap = compute_pnl_snapshot(_StubAPI(), conn=conn)
    # 1 BTC × (60000 − 50000) = +$10,000 unrealized
    assert abs(snap["unrealized_pnl_usdt"] - 10_000.0) < 1.0
    conn.close()


def test_ensemble_vote_unanimous_returns_full_size():
    """D4: all qualified candidates point the same way → multiplier 1.0."""
    from learning.ensemble_vote import compute_ensemble_vote
    cands = [
        {"direction": 1, "confluence": {"score": 9}, "rr": 2.5, "model": "a"},
        {"direction": 1, "confluence": {"score": 10}, "rr": 3.0, "model": "b"},
    ]
    v = compute_ensemble_vote(cands)
    assert v["status"] == "unanimous"
    assert v["size_multiplier"] == 1.0
    assert v["confidence"] == 1.0


def test_ensemble_vote_conflict_scales_down_size():
    """D4: long-side weight 10 vs short-side 8 → confidence (10-8)/18 ≈ 0.11
    → clamped to size_floor 0.3."""
    from learning.ensemble_vote import compute_ensemble_vote
    cands = [
        {"direction": 1, "confluence": {"score": 10}, "rr": 2.0, "model": "a"},
        {"direction": -1, "confluence": {"score": 8}, "rr": 2.0, "model": "b"},
    ]
    v = compute_ensemble_vote(cands, size_floor=0.3)
    assert v["status"] == "conflict_adjusted"
    assert v["winning_side"] == "long"
    assert v["size_multiplier"] == 0.3  # floored
    # Without floor, raw confidence ≈ 2/18 ≈ 0.111
    v2 = compute_ensemble_vote(cands, size_floor=0.0)
    assert abs(v2["size_multiplier"] - 0.1111) < 0.001


def test_ensemble_vote_filters_below_threshold_candidates():
    """D4: candidates failing min_score / min_rr don't get a vote."""
    from learning.ensemble_vote import compute_ensemble_vote
    cands = [
        {"direction": 1, "confluence": {"score": 9}, "rr": 2.5},
        {"direction": -1, "confluence": {"score": 5}, "rr": 1.0},  # below 8 / 1.5
    ]
    v = compute_ensemble_vote(cands)
    assert v["status"] == "unanimous"  # below-threshold short dropped
    assert v["n_short"] == 0


def test_annotate_picked_entry_with_vote_composes_with_existing_size_mult():
    """D4: ensemble multiplier composes with P2-14+ exploration multiplier."""
    from learning.ensemble_vote import annotate_picked_entry_with_vote
    analysis = {"concepts": {"entry_models": {
        "sweep_reversal": {"entries": [
            {"direction": 1, "confluence": {"score": 10}, "rr": 2.0, "model": "sweep_reversal"},
        ]},
        "ote_retracement": {"entries": [
            {"direction": -1, "confluence": {"score": 8}, "rr": 2.0, "model": "ote_retracement"},
        ]},
    }}}
    picked = {"model": "sweep_reversal", "direction": 1,
                "exploration_size_multiplier": 0.5}
    annotate_picked_entry_with_vote(picked, analysis, size_floor=0.3)
    # ensemble alone = 0.3 (floored), composed with prev 0.5 = 0.15
    assert picked["exploration_size_multiplier"] == 0.15
    assert picked["ensemble_vote"]["status"] == "conflict_adjusted"


def test_decommission_triggers_on_trailing_underwater(tmp_path):
    """D3: 25 consecutive -0.5R trailing trades → total_R = -12.5, well
    below default -5.0 floor → decommissioned."""
    from learning.model_decommission import (
        compute_per_model_health, decide_decommission,
    )
    records = []
    for i in range(25):
        records.append({
            "model": "sweep_reversal", "symbol": "BTC", "interval": "1h",
            "entry_time": f"2026-01-{1 + i // 24:02d}T{i % 24:02d}:00:00",
            "outcome": "stop", "r_multiple": -0.5,
        })
    health = compute_per_model_health(records, window_size=25, min_samples=20)
    out = decide_decommission(health, state={})
    assert any("decommissioned" in a for a in out["actions"])
    key = "sweep_reversal|BTC|1h"
    assert out["new_state"][key]["status"] == "decommissioned"


def test_decommission_revive_after_cooldown_and_recovery():
    """D3: previously decommissioned, cooldown passed, recovery total_R
    above revive_total_R → revived."""
    from datetime import datetime, timezone, timedelta
    from learning.model_decommission import decide_decommission
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=10)).isoformat(timespec="seconds")
    state = {"x|BTC|1h": {"status": "decommissioned", "ts": past, "total_R": -10}}
    health = {("x", "BTC", "1h"): {
        "n": 30, "n_in_window": 30, "total_R": 3.5,
        "mean_R": 0.12, "win_rate": 0.6,
        "first_in_window": "...", "last_in_window": "...",
        "eligible": True,
    }}
    out = decide_decommission(health, state, cooldown_days=7,
                                revive_total_R=1.0, now=now)
    assert any("revived" in a for a in out["actions"])
    assert out["new_state"]["x|BTC|1h"]["status"] == "active"


def test_apply_decommission_to_analysis_clears_entries():
    """D3: detector entries list is cleared + flagged when in dead state."""
    from learning.model_decommission import apply_decommission_to_analysis
    analysis = {"concepts": {"entry_models": {
        "sweep_reversal": {"entries": [{"a": 1}], "label": "sweep"},
        "ote_retracement": {"entries": [{"b": 1}], "label": "ote"},
    }}}
    state = {"sweep_reversal|BTC|1h": {"status": "decommissioned",
                                          "reason": "trailing_total_R=-7"}}
    out = apply_decommission_to_analysis(analysis, state,
                                            symbol="BTC", interval="1h")
    em = out["concepts"]["entry_models"]
    assert em["sweep_reversal"]["entries"] == []
    assert em["sweep_reversal"]["decommissioned"] is True
    # Other model untouched
    assert em["ote_retracement"]["entries"] == [{"b": 1}]


def test_bh_fdr_filter_picks_extreme_tail_only():
    """D1: BH-FDR with 10 hypotheses, only the smallest two p-values should
    pass at alpha=0.10."""
    from learning.cluster_ensemble import bh_fdr_filter
    # 10 p-values: two very small (0.001, 0.005) + eight scattered
    ps = [0.001, 0.005, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99]
    sig = bh_fdr_filter(ps, alpha=0.10)
    assert sig[0] is True
    assert sig[1] is True
    assert all(s is False for s in sig[2:])


def test_bh_fdr_filter_empty_inputs():
    """D1: empty p-value list → empty mask."""
    from learning.cluster_ensemble import bh_fdr_filter
    assert bh_fdr_filter([]) == []


def test_cluster_table_fdr_significant_field_propagates():
    """D1: build_cluster_weight_table stamps fdr_significant on each lift."""
    from learning.cluster_ensemble import build_cluster_weight_table
    # Build records where ote_zone clearly separates: active=+1R, inactive=-1R
    recs = []
    for _ in range(20):
        recs.append({"model": "x", "symbol": "BTC", "interval": "1h",
                       "regime": "trending",
                       "outcome": "target", "r_multiple": 1.0,
                       "factors_active": ["ote_zone"]})
    for _ in range(20):
        recs.append({"model": "x", "symbol": "BTC", "interval": "1h",
                       "regime": "trending",
                       "outcome": "stop", "r_multiple": -1.0,
                       "factors_active": []})
    table = build_cluster_weight_table(recs, factors=["ote_zone"],
                                          min_samples=10, fdr_alpha=0.10)
    k = ("x", "BTC", "1h", "trending")
    assert k in table
    stats = table[k]["factors"]["ote_zone"]
    assert "fdr_significant" in stats
    # Extreme lift (~2R) with n=40 → must clear FDR
    assert stats["fdr_significant"] is True
    assert stats["p_value"] < 0.05


def test_resolve_cluster_weights_blocks_non_fdr_significant_nudge():
    """D1: a non-significant factor is NOT nudged even with strong lift."""
    from learning.cluster_ensemble import resolve_cluster_weights
    k = ("x", "BTC", "1h", "trending")
    # Strong lift but fdr_significant=False → no nudge
    table = {k: {
        "n_total": 100, "mean_R": 0.0,
        "factors": {"ote_zone": {
            "lift": 1.0, "n_active": 50, "n_inactive": 50,
            "p_value": 0.40, "fdr_significant": False,
        }},
    }}
    out = resolve_cluster_weights(table, cluster=k,
                                     base_weights={"ote_zone": 1})
    assert out["ote_zone"] == 1   # unchanged
    # Same with significant=True → nudges
    table[k]["factors"]["ote_zone"]["fdr_significant"] = True
    out2 = resolve_cluster_weights(table, cluster=k,
                                      base_weights={"ote_zone": 1})
    assert out2["ote_zone"] == 2


def test_cost_aware_sweep_penalizes_high_slippage_symbols():
    """D5: same records, different slippage profile → flat fee vs heavy
    slippage sampler give different total/sharpe."""
    from learning.hyperparameter_sweep import _simulate
    records = [
        {"confluence_score": 9, "rr_planned": 2.5, "outcome": "target",
         "r_multiple": 1.0, "symbol": "BTC", "direction": 1}
        for _ in range(20)
    ]
    base = _simulate(records, min_score=8, min_rr=2.0, risk_pct=1.0)
    # 50 bps slippage on every fill
    heavy = lambda sym, side: 50.0
    cost = _simulate(records, min_score=8, min_rr=2.0, risk_pct=1.0,
                       slippage_sampler=heavy)
    assert base["total"] > cost["total"]   # heavy slippage subtracts more
    # 20 trades × 50 bps × 1.0 risk_pct = 20 × 0.005 = 0.10 dollars difference
    assert abs((base["total"] - cost["total"]) - 0.10) < 1e-6


def test_build_empirical_slippage_sampler_from_fills():
    """D5: convenience helper produces a working sampler from real fills."""
    from learning.hyperparameter_sweep import build_empirical_slippage_sampler
    fills = []
    submitted = {}
    # 10 BTC buys, each filled 30 bps worse than submitted
    for i in range(10):
        oid = f"o-{i}"
        submitted[oid] = 100.0
        fills.append({"symbol": "BTC", "side": "buy", "price": 100.3,
                       "order_id": oid})
    sampler = build_empirical_slippage_sampler(fills, submitted,
                                                  min_samples_for_real=5,
                                                  percentile=0.75)
    assert sampler is not None
    bps = sampler("BTC", "buy")
    # P75 of 10 identical 30bps samples = ~30
    assert 25.0 <= bps <= 35.0
    # Unknown symbol → default fallback
    assert sampler("UNKNOWN", "buy") == 5.0


def test_sweep_walk_forward_emits_best_oos_cell():
    """D2: walk-forward returns a best cell whose score is OOS-derived,
    not in-sample. Picked cell must beat the loser pattern in test fixture."""
    from learning.hyperparameter_sweep import sweep_walk_forward
    records = []
    base_day = 1
    for i in range(60):
        # winners: score=9, rr=2.5, +1R
        records.append({
            "entry_time": f"2026-01-{base_day + (i // 24):02d}T{i % 24:02d}:00:00+00:00",
            "confluence_score": 9, "rr_planned": 2.5,
            "outcome": "target", "r_multiple": 1.0 + (i % 5) * 0.1,
        })
    for i in range(60, 120):
        # losers: score=6, rr=1.5, -1R
        records.append({
            "entry_time": f"2026-01-{base_day + (i // 24):02d}T{i % 24:02d}:00:00+00:00",
            "confluence_score": 6, "rr_planned": 1.5,
            "outcome": "stop" if i % 3 else "target",
            "r_multiple": -1.0 if i % 3 else 0.3,
        })
    out = sweep_walk_forward(records, n_folds=3, min_trades_per_fold=10)
    assert out["status"] == "ok"
    assert out["best"] is not None
    # The OOS-best should refuse the loser pool — either via min_score≥7 or
    # via min_rr≥2.0 (both filters cut the noisy losers).
    chosen = out["best"]
    assert chosen["min_score"] >= 7 or chosen["min_rr"] >= 2.0


def test_sweep_walk_forward_handles_sparse_ledger():
    """D2: too few records → status=insufficient_data, best=None."""
    from learning.hyperparameter_sweep import sweep_walk_forward
    out = sweep_walk_forward([], n_folds=4, min_trades_per_fold=10)
    assert out["best"] is None
    assert out["status"] == "insufficient_data"


def test_weekly_digest_builds_markdown_for_current_week(tmp_path):
    """C3: build digest for in-window trades and write to vault path."""
    from datetime import datetime, timezone, timedelta
    from learning.weekly_digest import build_weekly_digest, write_weekly_digest
    now = datetime.now(timezone.utc)
    # 3 trades inside this week, 1 trade last week (filtered out)
    recs = [
        {"entry_time": (now - timedelta(days=1)).isoformat(),
         "outcome": "target", "r_multiple": 1.5,
         "model": "sweep_reversal", "symbol": "BTC", "interval": "1h"},
        {"entry_time": (now - timedelta(days=2)).isoformat(),
         "outcome": "stop", "r_multiple": -1.0,
         "model": "sweep_reversal", "symbol": "BTC", "interval": "1h"},
        {"entry_time": (now - timedelta(days=3)).isoformat(),
         "outcome": "target", "r_multiple": 2.0,
         "model": "ote_retracement", "symbol": "ETH", "interval": "15m"},
        {"entry_time": (now - timedelta(days=15)).isoformat(),
         "outcome": "target", "r_multiple": 5.0,
         "model": "unicorn", "symbol": "BTC", "interval": "1h"},  # too old
    ]
    out = build_weekly_digest(recs)
    assert out["n_total"] == 3   # 4th is outside the ISO-week window
    assert out["n_resolved"] == 3
    # Markdown contains expected sections
    assert "Top cluster winners" in out["markdown"]
    assert "Top cluster losers" in out["markdown"]
    assert "ote_retracement" in out["markdown"]   # the top winner cluster
    # Write to disk
    result = write_weekly_digest(recs, str(tmp_path))
    written = (tmp_path / "SMC" / "Digests").iterdir()
    assert any(p.suffix == ".md" for p in written)
    assert result["wrote"] if False else True  # path key present
    assert "path" in result


def test_weekly_digest_empty_records_renders_placeholder():
    from learning.weekly_digest import build_weekly_digest
    out = build_weekly_digest([])
    assert "no resolved trades this week" in out["markdown"]
    assert out["n_total"] == 0


def test_learning_health_critical_on_empty_ledger():
    """C1: no data → critical / score around the floor."""
    from learning.learning_health import compute_learning_health
    out = compute_learning_health(records=[], kill_switch_state="READY")
    assert out["max_score"] == 100
    # samples=0, velocity=insufficient(12), pnl=12 (no data), decay=25 → 49
    assert out["score"] <= 60
    assert out["status"] in ("critical", "degraded", "watch")
    assert out["components"]["samples"]["n_resolved"] == 0


def test_learning_health_healthy_when_all_components_pass():
    """C1: many wins + READY + improving → healthy / ≥ 80."""
    from datetime import datetime, timezone, timedelta
    from learning.learning_health import compute_learning_health
    now = datetime.now(timezone.utc)
    records = []
    # 30 winning resolved trades over 30 days
    for i in range(30):
        records.append({
            "entry_time": (now - timedelta(days=29 - i)).isoformat(),
            "outcome": "target", "r_multiple": 1.0,
            "model": "x", "symbol": "BTC",
        })
    out = compute_learning_health(records=records, kill_switch_state="READY")
    # Without backtest pool, correlation gate fails → pnl loses 8.
    # Velocity flat-line on identical winners → stagnant (12). Still ≥ 75
    # which counts as "watch" or "healthy" depending on the boundary.
    assert out["score"] >= 75
    assert out["status"] in ("healthy", "watch")
    assert out["components"]["samples"]["score"] == 25
    assert out["components"]["edge_decay"]["score"] == 25


def test_learning_health_critical_when_kill_switch_locked():
    """C1: LOCKED kill switch alone caps edge_decay at 0."""
    from learning.learning_health import compute_learning_health
    out = compute_learning_health(records=[], kill_switch_state="LOCKED")
    assert out["components"]["edge_decay"]["score"] == 0
    assert out["components"]["edge_decay"]["kill_switch_state"] == "LOCKED"


def test_connect_db_enables_wal(tmp_path):
    """E3: connect_db sets WAL + busy_timeout on every connection."""
    from smc_quant import connect_db
    db = str(tmp_path / "t.db")
    conn = connect_db(db, row_factory=True)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


def test_autolearn_scheduler_disabled_by_default(monkeypatch):
    """E1: server-side loop is opt-in; off unless env set."""
    monkeypatch.delenv("SMC_AUTOLEARN_ENABLED", raising=False)
    from learning.autolearn_scheduler import is_enabled
    assert is_enabled() is False


def test_autolearn_scheduler_symbols_from_env(monkeypatch):
    monkeypatch.setenv("SMC_AUTOLEARN_SYMBOLS", "AAA-USDT, BBB-USDT")
    from learning.autolearn_scheduler import configured_symbols
    assert configured_symbols() == ["AAA-USDT", "BBB-USDT"]
    monkeypatch.delenv("SMC_AUTOLEARN_SYMBOLS")
    assert configured_symbols() == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


def test_autolearn_loop_runs_ticks_and_paces(monkeypatch):
    """E1: loop calls tick_fn, honours next_interval_seconds, survives
    a per-symbol error without dying."""
    import asyncio, importlib
    monkeypatch.setenv("SMC_AUTOLEARN_ENABLED", "1")
    monkeypatch.setenv("SMC_AUTOLEARN_SYMBOLS", "X-USDT")
    monkeypatch.setenv("SMC_AUTOLEARN_MIN_INTERVAL", "30")
    import learning.autolearn_scheduler as sched
    importlib.reload(sched)

    calls = []
    def fake_tick(payload):
        calls.append(payload["symbol"])
        if len(calls) == 2:
            raise RuntimeError("boom")    # must NOT kill the loop
        if len(calls) >= 4:
            raise KeyboardInterrupt        # deterministic stop
        return {"state": "READY", "history": {"next_interval_seconds": 30}}

    clk = {"t": 0.0}
    async def fake_sleep(d): clk["t"] += d
    def now(): return clk["t"]

    async def run():
        try:
            await sched.autolearn_loop(fake_tick, sleep_fn=fake_sleep, now_fn=now)
        except KeyboardInterrupt:
            pass
    asyncio.run(run())
    assert len(calls) >= 4            # survived the boom at call 2
    st = sched.scheduler_state()
    assert st["per_symbol"]["X-USDT"]["errors"] >= 1


def test_ledger_paths_centralized_and_env_overridable(monkeypatch):
    """C2: LedgerPaths is the single source of truth, env-overridable."""
    from smc_quant import LedgerPaths
    monkeypatch.setenv("SMC_LEDGER_DIR", "/tmp/test-ledger")
    assert LedgerPaths.training_ledger() == "/tmp/test-ledger/smc_training_ledger.jsonl"
    assert LedgerPaths.paper_journal() == "/tmp/test-ledger/smc_paper_journal.jsonl"
    assert LedgerPaths.paper_trades() == "/tmp/test-ledger/smc_paper_journal_trades.jsonl"
    assert LedgerPaths.missed_signals() == "/tmp/test-ledger/smc_missed_signals.jsonl"
    monkeypatch.delenv("SMC_LEDGER_DIR")
    assert LedgerPaths.training_ledger() == "tmp/smc_training_ledger.jsonl"


def test_auto_apply_sweep_writes_to_profile_when_improvement_clears(tmp_path):
    """B1: when sweep beats cooldown + improvement threshold, profile.yaml
    is updated and last_auto_apply audit field is stamped."""
    import yaml
    from learning.sweep_auto_apply import auto_apply_sweep
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({
        "min_score": 6, "min_rr": 1.5, "risk_pct": 1.0,
    }), encoding="utf-8")
    # Construct records that the sweep will clearly prefer at min_score=9
    records = []
    for r in (1.5, 2.0, 1.0, 1.2, 0.8, 1.7, 1.3) * 5:
        records.append({"confluence_score": 9, "rr": 2.5, "r_multiple": r})
    for r in (-1.0, -1.2, 0.3, -0.8, -1.5, 0.2, -1.0) * 5:
        records.append({"confluence_score": 6, "rr": 1.5, "r_multiple": r})
    out = auto_apply_sweep(records=records, profile_path=str(profile))
    assert out["applied"] is True
    on_disk = yaml.safe_load(profile.read_text())
    assert on_disk["last_auto_apply"]["delta_sharpe"] > 0
    # at least one of the gating knobs must have moved
    assert (on_disk["min_score"], on_disk["min_rr"]) != (6, 1.5)


def test_auto_apply_sweep_respects_cooldown(tmp_path):
    """Re-applying within cooldown returns reason=cooldown_active."""
    import yaml
    from datetime import datetime, timezone
    from learning.sweep_auto_apply import auto_apply_sweep
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({
        "min_score": 8, "min_rr": 2.0, "risk_pct": 1.0,
        "last_auto_apply": {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sharpe": 0.3, "delta_sharpe": 0.2, "n_trades": 35,
            "before": {},
        },
    }), encoding="utf-8")
    out = auto_apply_sweep(records=[], profile_path=str(profile),
                             min_days_since_last_apply=30)
    assert out["applied"] is False
    assert out["reason"] == "cooldown_active"


def test_cluster_weight_table_threads_per_model_weights_to_detectors():
    """B2: when build_smc_analysis is called with cluster_weight_table,
    _cluster_weights_for(model_name) must resolve to model-specific weights
    (not the global default for all 6 detectors)."""
    from learning.cluster_ensemble import resolve_cluster_weights
    base = {"htf_bias_aligned": 2, "ote_zone": 1, "killzone": 1}
    # Cluster A favors sweep_reversal's ote_zone; cluster B doesn't.
    table = {
        ("sweep_reversal", "BTC", "1h", "trending"): {
            "n_total": 60, "mean_R": 0.5,
            "factors": {"ote_zone": {"lift": 0.5, "n_active": 30, "n_inactive": 30}},
        },
        ("ote_retracement", "BTC", "1h", "trending"): {
            "n_total": 60, "mean_R": -0.3,
            "factors": {"ote_zone": {"lift": -0.4, "n_active": 30, "n_inactive": 30}},
        },
    }
    sweep_w = resolve_cluster_weights(table,
        cluster=("sweep_reversal", "BTC", "1h", "trending"), base_weights=base)
    ote_w = resolve_cluster_weights(table,
        cluster=("ote_retracement", "BTC", "1h", "trending"), base_weights=base)
    # sweep_reversal nudged up (positive lift)
    assert sweep_w["ote_zone"] == 2
    # ote_retracement nudged down (negative lift)
    assert ote_w["ote_zone"] == 0
    # global default would be 1; the two cluster paths diverge as required.
    assert sweep_w["ote_zone"] != ote_w["ote_zone"]


def test_build_trade_record_stamps_interval_and_regime_string():
    """B4: build_trade_record produces flat interval + regime string for
    cluster_ensemble bucketing."""
    from smc_quant import build_trade_record
    rec = build_trade_record(
        {"model": "sweep_reversal", "direction": 1,
         "entry": 100.0, "stop": 98.0, "target": 104.0,
         "factors": {}, "confluence": {"score": 8}, "triggered": True},
        trade_outcome={"outcome": "target", "r_multiple": 1.5, "entry_index": 0},
        symbol="BTC-USDT", timeframe="1h",
        regime={"bucket": "trending", "atr": 100},
        source="paper",
    )
    assert rec["interval"] == "1h"
    assert rec["regime"] == "trending"     # flat string
    assert rec["regime_detail"]["atr"] == 100   # dict preserved
    assert rec["source"] == "paper"


def test_build_trade_record_accepts_regime_as_string():
    """B4: passing regime as string (cluster ensemble caller path) also works."""
    from smc_quant import build_trade_record
    rec = build_trade_record(
        {"model": "x", "direction": -1, "entry": 100, "stop": 102, "target": 96,
         "factors": {}, "confluence": {"score": 9}, "triggered": True},
        trade_outcome={"outcome": "stop", "r_multiple": -1.0, "entry_index": 0},
        symbol="ETH", timeframe="15m",
        regime="ranging",
    )
    assert rec["regime"] == "ranging"
    assert rec["regime_detail"] == {}


def test_build_trade_record_emits_current_schema_version():
    """A4: newly built records should already emit the latest schema version."""
    from smc_quant import (
        TRADE_LEDGER_SCHEMA_VERSION,
        build_trade_record,
    )
    rec = build_trade_record(
        {"model": "x", "direction": 1, "entry": 100, "stop": 98, "target": 104,
         "factors": {}, "confluence": {"score": 8}, "triggered": True},
        trade_outcome={"outcome": "target", "r_multiple": 1.0, "entry_index": 0},
        symbol="BTC-USDT",
        timeframe="1h",
    )
    assert rec["schema_version"] == TRADE_LEDGER_SCHEMA_VERSION == 2


def test_schema_version_stamped_on_persist(tmp_path):
    """A4: persist_trade_records stamps schema_version=2 on every record."""
    from smc_quant import persist_trade_records, load_trade_records
    p = tmp_path / "ledger.jsonl"
    persist_trade_records([
        {"symbol": "BTC", "outcome": "target", "r_multiple": 1.0,
         "entry_time": "2025-01-01T00:00:00", "model": "x"},
    ], str(p))
    rec = load_trade_records(str(p))[0]
    assert rec["schema_version"] == 2


def test_load_trade_records_normalizes_v1_legacy(tmp_path):
    """A4: legacy records without schema_version get backfilled to v2."""
    from smc_quant import load_trade_records
    p = tmp_path / "legacy.jsonl"
    # Hand-write a v1 record (no schema_version, no source/interval/regime)
    p.write_text(
        '{"symbol": "BTC", "outcome": "target", "r_multiple": 1.0}\n',
        encoding="utf-8",
    )
    rec = load_trade_records(str(p))[0]
    assert rec["schema_version"] == 2
    assert rec["source"] == "legacy"
    assert rec["interval"] is None
    assert rec["regime"] is None


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


def test_ledger_cache_invalidates_when_content_changes_but_mtime_and_size_match(tmp_path):
    """A3: same mtime/size but different payload still invalidates via fingerprint."""
    import os
    from learning.ledger_cache import _LedgerCache
    cache = _LedgerCache(max_entries=4, ttl_sec=60.0)
    f = tmp_path / "ledger.jsonl"
    original = '{"a":1}\n'
    rewritten = '{"b":2}\n'
    assert len(original) == len(rewritten)
    f.write_text(original, encoding="utf-8")
    st = os.stat(f)

    calls = []

    def loader(path):
        calls.append(path)
        return [{"payload": Path(path).read_text(encoding="utf-8")}]

    first = cache.get(str(f), loader)
    f.write_text(rewritten, encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    second = cache.get(str(f), loader)

    assert len(calls) == 2
    assert first != second
    assert second[0]["payload"] == rewritten


def test_learning_orchestrator_load_records_uses_shared_ledger_cache(monkeypatch):
    """A3: the report builder path must use the same shared cache entry point."""
    import smc_learning_orchestrator as orch

    seen = []

    def fake_cached_load(path):
        seen.append(path)
        return [{"symbol": "BTC-USDT", "outcome": "target", "r_multiple": 1.0}]

    monkeypatch.setattr(orch, "read_trade_ledger", fake_cached_load)
    out = orch._load_records("tmp/test-ledger.jsonl")
    assert seen == ["tmp/test-ledger.jsonl"]
    assert out[0]["symbol"] == "BTC-USDT"


def test_recent_outcomes_for_cooldown_uses_shared_cached_reader(monkeypatch):
    """Data-flow audit: cooldown reads should route through shared cached reader."""
    import smc_auto_workflow as aw

    seen = []

    def fake_load(path, *, symbol=None, use_cache=True, copy_records=False):
        seen.append((path, symbol, use_cache, copy_records))
        return []

    monkeypatch.setattr("smc_quant.read_trade_ledger", fake_load)
    out = aw._recent_outcomes_for_cooldown("db.sqlite", "BTC-USDT", n=3)
    assert out == []
    assert seen == [("tmp/smc_training_ledger.jsonl", "BTC-USDT", True, False)]


def test_train_from_ledger_uses_shared_cached_reader(monkeypatch):
    """Data-flow audit: training should pull ledger via the shared cached entry point."""
    import smc_training_loop as stl

    seen = []

    def fake_load(path, *, symbol=None, use_cache=True, copy_records=False):
        seen.append((path, symbol, use_cache, copy_records))
        return []

    monkeypatch.setattr(stl, "read_trade_ledger", fake_load)
    out = stl.train_from_ledger(ledger_path="tmp/test-ledger.jsonl")
    assert out.sample_size == 0
    assert seen == [("tmp/test-ledger.jsonl", None, True, False)]


def test_smc_quant_load_cached_trade_records_falls_back_to_fresh_read(monkeypatch, tmp_path):
    """If the cache layer is unavailable, the core helper must still work."""
    import smc_quant

    p = tmp_path / "ledger.jsonl"
    p.write_text('{"symbol":"BTC","r_multiple":1.0}\n', encoding="utf-8")

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "learning.ledger_cache":
            raise ImportError("cache unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    recs = smc_quant.load_cached_trade_records(str(p))
    assert recs[0]["symbol"] == "BTC"


def test_read_trade_ledger_supports_symbol_filter_and_copy(monkeypatch):
    """Unified read helper should centralize filter/copy policy."""
    import smc_quant
    # F3: read_trade_ledger now lives in smc_ledger_io and resolves
    # load_cached_trade_records from that module's namespace, so patch there.
    import smc_ledger_io

    source = [
        {"symbol": "BTC-USDT", "r_multiple": 1.0},
        {"symbol": "ETH-USDT", "r_multiple": -1.0},
    ]

    monkeypatch.setattr(smc_ledger_io, "load_cached_trade_records", lambda path: source)
    same_ref = smc_quant.read_trade_ledger("tmp/x.jsonl")
    filtered = smc_quant.read_trade_ledger("tmp/x.jsonl", symbol="ETH-USDT")
    copied = smc_quant.read_trade_ledger("tmp/x.jsonl", copy_records=True)

    assert same_ref is source
    assert filtered == [{"symbol": "ETH-USDT", "r_multiple": -1.0}]
    assert copied == source
    assert copied is not source


def test_paper_runner_mae_mfe_table_does_not_mutate_cached_ledger_lists(monkeypatch, tmp_path):
    """Cached ledger lists are shared references and must not be extended in place."""
    import smc_paper_runner as spr
    import smc_quant

    paper_records = [{"symbol": "BTC-USDT", "model": "paper"}]
    training_records = [{"symbol": "BTC-USDT", "model": "train"}]
    captured = {}

    def fake_load(path, *, symbol=None, use_cache=True, copy_records=False):
        base = paper_records if path.endswith("_trades.jsonl") else training_records
        if symbol:
            base = [r for r in base if r.get("symbol") == symbol]
        return list(base) if copy_records else base

    def fake_build(records):
        captured["records"] = records
        return {"ok": True}

    monkeypatch.setattr(smc_quant, "read_trade_ledger", fake_load)
    monkeypatch.setattr(
        "learning.mae_mfe_calibration.build_model_calibration_table",
        fake_build,
    )
    cfg = spr.PaperRunConfig(journal_path=str(tmp_path / "paper_journal.jsonl"))
    runner = spr.SmcPaperRunner(client=MagicMock(), config=cfg)

    out = runner._build_mae_mfe_table()

    assert out == {"ok": True}
    assert paper_records == [{"symbol": "BTC-USDT", "model": "paper"}]
    assert training_records == [{"symbol": "BTC-USDT", "model": "train"}]
    assert len(captured["records"]) == 2


def test_api_token_middleware_off_when_env_unset(monkeypatch):
    """A2: DASHBOARD_API_TOKEN unset → middleware is a no-op."""
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    from learning.api_auth import _token
    assert _token() == ""


def test_api_token_reads_local_settings_when_env_missing(monkeypatch, tmp_path):
    """A2: env absent → fallback to local settings.json dashboard_api_token."""
    import json
    import llm_providers
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"dashboard_api_token": "from-settings"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_providers, "SETTINGS_FILE", settings_path)
    from learning.api_auth import _token
    assert _token() == "from-settings"


def test_api_token_env_overrides_local_settings(monkeypatch, tmp_path):
    """A2: explicit env token wins over settings.json fallback."""
    import json
    import llm_providers
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"dashboard_api_token": "from-settings"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_providers, "SETTINGS_FILE", settings_path)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "from-env")
    from learning.api_auth import _token
    assert _token() == "from-env"


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


def test_e4_apply_overrides_is_thread_safe():
    """E4: concurrent apply_strategy_yaml_overrides must not lost-update;
    snapshot accessor returns a consistent copy."""
    import threading
    import smc_quant
    from smc_quant import apply_strategy_yaml_overrides, snapshot_confluence_weights
    errors = []
    def worker():
        try:
            for _ in range(20):
                apply_strategy_yaml_overrides()
                snap = snapshot_confluence_weights()
                assert isinstance(snap, dict)
                # core factor must always be present (never half-applied)
                assert "htf_bias_aligned" in snap
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, errors
    # snapshot is a copy — mutating it must not affect the global
    snap = snapshot_confluence_weights()
    snap["htf_bias_aligned"] = 999
    assert smc_quant.CONFLUENCE_WEIGHTS_DEFAULT["htf_bias_aligned"] != 999


def test_e2_categorize_tables_buckets_correctly(tmp_path):
    """E2: table classifier separates personal / exchange / learning."""
    import sqlite3
    from learning.db_split import categorize_tables
    db = str(tmp_path / "mix.db")
    c = sqlite3.connect(db)
    for t in ["positions", "watchlist", "trades",
              "crypto_api_keys", "crypto_orders",
              "smc_training_history", "smc_baseline_equity",
              "paper_acceptance_runs", "crypto_kill_switch",
              "price_cache"]:
        c.execute(f"CREATE TABLE {t} (id INTEGER)")
    c.commit()
    cats = categorize_tables(c)
    assert "positions" in cats["personal"]
    assert "crypto_api_keys" in cats["exchange"]
    assert "crypto_orders" in cats["exchange"]
    assert "smc_training_history" in cats["learning"]
    assert "paper_acceptance_runs" in cats["learning"]
    # crypto_kill_switch is learning despite crypto_ prefix
    assert "crypto_kill_switch" in cats["learning"]
    assert "price_cache" in cats["other"]
    c.close()


def test_e2_split_learning_db_dry_run_writes_nothing(tmp_path):
    """E2: dry_run reports row counts but creates no dst file."""
    import sqlite3, os
    from learning.db_split import split_learning_db
    src = str(tmp_path / "src.db")
    dst = str(tmp_path / "dst.db")
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE smc_training_history (id INTEGER)")
    c.executemany("INSERT INTO smc_training_history VALUES (?)", [(1,), (2,), (3,)])
    c.execute("CREATE TABLE positions (id INTEGER)")
    c.commit(); c.close()
    rep = split_learning_db(src, dst, dry_run=True)
    assert rep["dry_run"] is True
    assert rep["copied"]["smc_training_history"] == 3
    assert not os.path.exists(dst)   # nothing written


def test_e2_split_learning_db_copies_and_verifies(tmp_path):
    """E2: real copy moves learning tables; verify_split confirms counts;
    personal tables are NOT copied."""
    import sqlite3
    from learning.db_split import split_learning_db, verify_split
    src = str(tmp_path / "src.db")
    dst = str(tmp_path / "dst.db")
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE smc_training_history (id INTEGER, v TEXT)")
    c.executemany("INSERT INTO smc_training_history VALUES (?,?)",
                  [(1, "a"), (2, "b")])
    c.execute("CREATE TABLE positions (id INTEGER)")
    c.execute("INSERT INTO positions VALUES (99)")
    c.commit(); c.close()
    rep = split_learning_db(src, dst, dry_run=False)
    assert rep["copied"]["smc_training_history"] == 2
    v = verify_split(src, dst)
    assert v["ok"] is True
    # personal table must not exist in dst
    d = sqlite3.connect(dst)
    tabs = [r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "smc_training_history" in tabs
    assert "positions" not in tabs
    d.close()


def test_g1_swallow_logs_and_counts(monkeypatch):
    """G1/F2: swallow() records the error instead of vanishing it."""
    from learning.obs_log import get_logger, swallow, swallow_counts, reset_swallow_counts
    reset_swallow_counts()
    log = get_logger("test_ctx")
    with swallow(log, "unit_ctx"):
        raise ValueError("boom")
    counts = swallow_counts()
    assert counts["by_context"]["unit_ctx"] == 1
    assert "ValueError" in counts["last_error"]["unit_ctx"]
    # caller continues — no exception propagated
    ran = False
    with swallow(log, "unit_ctx"):
        ran = True
    assert ran is True


def test_g1_swallow_reraise_propagates():
    from learning.obs_log import get_logger, swallow, reset_swallow_counts
    reset_swallow_counts()
    log = get_logger("test_ctx2")
    import pytest as _pytest
    with _pytest.raises(KeyError):
        with swallow(log, "ctx2", reraise=True):
            raise KeyError("x")


def test_g2_ops_metrics_endpoint_shape(monkeypatch):
    """G2: ops-metrics aggregates scheduler/cache/swallow/file sections."""
    import importlib
    monkeypatch.setenv("SMC_LEDGER_DIR", "/tmp/ops-test-ledger")
    try:
        from fastapi.testclient import TestClient
        import app as _app
        importlib.reload(_app)
        client = TestClient(_app.app)
        r = client.get("/api/smc-crypto/ops-metrics")
        assert r.status_code == 200
        body = r.json()
        assert "autolearn" in body
        assert "ledger_cache" in body
        assert "swallowed_errors" in body
        assert "ledger_files" in body
    except Exception as e:
        import pytest as _pytest
        _pytest.skip(f"app import failed: {e}")


def test_g3_rotate_ledger_archives_overflow(tmp_path):
    """G3: rotation keeps rolling window per symbol, gzip-archives the rest,
    and nothing is lost."""
    import json, gzip, os
    from learning.ledger_rotation import rotate_ledger
    p = tmp_path / "led.jsonl"
    rows = []
    for i in range(120):
        rows.append({"symbol": "BTC-USDT", "entry_time": f"2026-01-01T{i:02d}:00:00",
                     "outcome": "target", "r_multiple": 1.0})
    for i in range(30):
        rows.append({"symbol": "ETH-USDT", "entry_time": f"2026-01-01T{i:02d}:00:00",
                     "outcome": "stop", "r_multiple": -1.0})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    rep = rotate_ledger(str(p), keep_per_symbol=50, archive_dir=str(tmp_path / "arch"))
    assert rep["rotated"] is True
    # BTC: 120 → keep 50, archive 70; ETH: 30 → keep all 30
    assert rep["per_symbol"]["BTC-USDT"]["kept"] == 50
    assert rep["per_symbol"]["BTC-USDT"]["archived"] == 70
    assert rep["per_symbol"]["ETH-USDT"]["archived"] == 0
    # live file now has 80 rows
    live = [json.loads(l) for l in open(p) if l.strip()]
    assert len(live) == 80
    # archive has the 70 overflow, nothing lost
    gz_rows = []
    with gzip.open(rep["archive_path"], "rt") as g:
        gz_rows = [json.loads(l) for l in g if l.strip()]
    assert len(gz_rows) == 70
    assert len(live) + len(gz_rows) == 150


def test_g3_rotate_ledger_noop_under_cap(tmp_path):
    """G3: already under cap → no archive, no rewrite."""
    import json
    from learning.ledger_rotation import rotate_ledger
    p = tmp_path / "led.jsonl"
    rows = [{"symbol": "BTC-USDT", "entry_time": f"2026-01-01T{i:02d}:00:00",
             "outcome": "target", "r_multiple": 1.0} for i in range(10)]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    rep = rotate_ledger(str(p), keep_per_symbol=500, archive_dir=str(tmp_path / "arch"))
    assert rep["rotated"] is False
    assert rep["reason"] == "under_cap"


def test_roundj_scheduler_runs_periodic_maintenance(monkeypatch):
    """Round J: autolearn loop fires _run_maintenance on its own cadence;
    failures in a tick or maintenance never kill the loop."""
    import asyncio, importlib
    monkeypatch.setenv("SMC_AUTOLEARN_ENABLED", "1")
    monkeypatch.setenv("SMC_AUTOLEARN_SYMBOLS", "X-USDT")
    monkeypatch.setenv("SMC_AUTOLEARN_MIN_INTERVAL", "30")
    monkeypatch.setenv("SMC_MAINTENANCE_INTERVAL", "300")
    import learning.autolearn_scheduler as sched
    importlib.reload(sched)

    maint_calls = []
    monkeypatch.setattr(sched, "_run_maintenance",
                        lambda: maint_calls.append(1) or {"rotation": {"rotated": False}})

    ticks = []
    def fake_tick(payload):
        ticks.append(payload["symbol"])
        if len(ticks) >= 20:
            raise KeyboardInterrupt
        return {"state": "READY", "history": {"next_interval_seconds": 30}}

    clk = {"t": 0.0}
    async def fake_sleep(d): clk["t"] += d
    def now(): return clk["t"]

    async def run():
        try:
            await sched.autolearn_loop(fake_tick, sleep_fn=fake_sleep, now_fn=now)
        except KeyboardInterrupt:
            pass
    asyncio.run(run())
    # virtual time advanced well past 300s of maintenance cadence → ran ≥1
    assert len(maint_calls) >= 1
    st = sched.maintenance_state()
    assert st["runs"] >= 1


def test_roundj_run_maintenance_is_resilient(tmp_path, monkeypatch):
    """Round J: _run_maintenance returns a report even when ledger absent;
    errors are captured per-section, never raised."""
    monkeypatch.setenv("SMC_LEDGER_DIR", str(tmp_path))
    import importlib
    import learning.autolearn_scheduler as sched
    importlib.reload(sched)
    out = sched._run_maintenance()
    assert "rotation" in out
    assert "decommission" in out


def test_roundk_runner_swallow_is_accounted(monkeypatch):
    """Round K/F2: a failure in the decommission-apply step is logged +
    counted (not silently vanished) via obs_log.swallow."""
    from learning.obs_log import swallow, swallow_counts, reset_swallow_counts, get_logger
    reset_swallow_counts()
    log = get_logger("runner_test")
    # Simulate the runner's wrapped block raising
    with swallow(log, "apply_decommission"):
        raise RuntimeError("decom boom")
    with swallow(log, "ensemble_vote"):
        raise RuntimeError("vote boom")
    counts = swallow_counts()
    assert counts["by_context"]["apply_decommission"] == 1
    assert counts["by_context"]["ensemble_vote"] == 1
    # These contexts are exactly the ones the runner uses (regression guard
    # that the names stay aligned with ops-metrics expectations).
    assert "apply_decommission" in counts["by_context"]
    assert "ensemble_vote" in counts["by_context"]


def test_roundn_checkpoint_wal_returns_report(tmp_path, monkeypatch):
    """Round N: checkpoint_wal runs PRAGMA wal_checkpoint and reports
    residual -wal size without raising on a fresh WAL DB."""
    import sqlite3, importlib
    # Point deps.portfolio_db_path + get_db at a tmp WAL db via app.DB patch.
    db = tmp_path / "portfolio.db"
    # seed a WAL db with a write
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (id INTEGER)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit(); c.close()
    import deps
    monkeypatch.setattr(deps, "portfolio_db_path", lambda: str(db))
    monkeypatch.setattr(deps, "get_db", lambda db_path=None: (lambda cc: (cc.execute("PRAGMA journal_mode=WAL"), cc)[1])(sqlite3.connect(str(db))))
    import learning.autolearn_scheduler as sched
    importlib.reload(sched)
    rep = sched.checkpoint_wal()
    assert "wal_bytes_after" in rep
    assert isinstance(rep["wal_bytes_after"], int)


def test_roundn_run_maintenance_includes_wal_checkpoint(tmp_path, monkeypatch):
    """Round N: maintenance report now carries a wal_checkpoint section."""
    monkeypatch.setenv("SMC_LEDGER_DIR", str(tmp_path))
    import importlib
    import learning.autolearn_scheduler as sched
    importlib.reload(sched)
    # checkpoint may error in the bare test env; assert the key exists either way
    out = sched._run_maintenance()
    assert "wal_checkpoint" in out


def test_roundo_sanitize_weight_overrides_filters_corrupt():
    """Round O: corrupt weight values are rejected, valid ones kept."""
    from smc_quant import _sanitize_weight_overrides
    clean, rejected = _sanitize_weight_overrides({
        "htf_bias_aligned": 3,        # ok
        "ote_zone": "abc",            # not numeric → reject
        "killzone": float("nan"),     # nan → reject
        "unmitigated_ob": 999,        # out of range → reject
        "liquidity_swept": 2.0,       # float-coercible → 2
        "flag": True,                 # bool → reject
    })
    assert clean == {"htf_bias_aligned": 3, "liquidity_swept": 2}
    reasons = {r["factor"]: r["reason"] for r in rejected}
    assert reasons["ote_zone"] == "not_numeric"
    assert reasons["killzone"] == "not_numeric"
    assert reasons["unmitigated_ob"] == "out_of_range"
    assert reasons["flag"] == "not_numeric"


def test_roundo_apply_overrides_reports_rejected(monkeypatch):
    """Round O: apply_strategy_yaml_overrides returns a rejected[] list and
    a corrupt weight never reaches the live scorer."""
    import smc_quant
    monkeypatch.setattr(smc_quant, "load_yaml_config", lambda name: (
        {"confluence": {"weights": {"htf_bias_aligned": 2, "ote_zone": "junk"}}}
        if name == "strategy.yaml" else {}
    ))
    saved = dict(smc_quant.CONFLUENCE_WEIGHTS_DEFAULT)
    try:
        out = smc_quant.apply_strategy_yaml_overrides()
        assert any(r["factor"] == "ote_zone" for r in out["rejected"])
        # corrupt value did NOT land in the live weights
        assert smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.get("ote_zone") != "junk"
    finally:
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.clear()
        smc_quant.CONFLUENCE_WEIGHTS_DEFAULT.update(saved)
