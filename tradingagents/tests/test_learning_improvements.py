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
