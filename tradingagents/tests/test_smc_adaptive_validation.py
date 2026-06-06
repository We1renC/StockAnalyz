import sqlite3

import numpy as np
import pandas as pd

from learning.adaptive_store import ensure_adaptive_calibration_schema, upsert_trade_ledger_records
from learning.adaptive_validation import (
    PurgedWalkForwardSplit,
    build_gate_results,
    compute_sample_uniqueness,
    effective_sample_size,
    validation_entropy_sizing,
)
from learning.probe_controller import compute_probe_notional, plan_probe_order


def _ledger_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"entry_time": "2026-06-01T00:00:00", "exit_time": "2026-06-01T01:00:00"},
            {"entry_time": "2026-06-01T00:30:00", "exit_time": "2026-06-01T01:30:00"},
            {"entry_time": "2026-06-01T02:00:00", "exit_time": "2026-06-01T03:00:00"},
            {"entry_time": "2026-06-01T04:00:00", "exit_time": "2026-06-01T05:00:00"},
        ]
    )


def test_sample_uniqueness_and_effective_sample_size_respect_overlap():
    frame = _ledger_frame()
    bar_index = pd.date_range("2026-06-01T00:00:00", periods=11, freq="30min")
    uniqueness = compute_sample_uniqueness(frame, bar_index)

    assert abs(uniqueness.iloc[0] - (2.0 / 3.0)) < 1e-9
    assert abs(uniqueness.iloc[1] - (2.0 / 3.0)) < 1e-9
    assert uniqueness.iloc[2] == 1.0
    assert uniqueness.iloc[3] == 1.0
    assert effective_sample_size(uniqueness.to_numpy()) <= len(frame)


def test_purged_walk_forward_split_removes_overlap_and_embargo():
    frame = _ledger_frame()
    splitter = PurgedWalkForwardSplit(n_splits=2, embargo_bars=1)
    folds = list(splitter.split_with_meta(frame, min_train_samples=2))

    assert len(folds) == 2
    first = folds[0]
    assert np.array_equal(first["test_idx"], np.array([0, 1]))
    assert np.array_equal(first["train_idx"], np.array([3]))
    assert first["reliable"] is False
    assert first["reason"] == "insufficient_train_samples_after_purge"


def test_validation_entropy_sizing_yields_probe_and_ready_states():
    probe_gates = build_gate_results(
        walk_forward_pass_ratio=0.75,
        pbo=0.55,
        dsr_probability=0.70,
        overall_expectancy=0.20,
        recent_expectancy=0.15,
        historical_expectancy=0.50,
        overall_win_rate=0.45,
        recent_win_rate=0.38,
        calibration_new_score=0.20,
        calibration_old_score=0.30,
    )
    probe = validation_entropy_sizing(probe_gates, n_eff=35.0)
    assert probe["state_hint"] == "VALIDATING_PROBE"
    assert 0.0 < probe["risk_multiplier"] <= 0.10

    ready_gates = build_gate_results(
        walk_forward_pass_ratio=1.0,
        pbo=0.10,
        dsr_probability=0.99,
        overall_expectancy=0.70,
        recent_expectancy=0.60,
        historical_expectancy=0.80,
        overall_win_rate=0.58,
        recent_win_rate=0.52,
        calibration_new_score=0.50,
        calibration_old_score=0.20,
    )
    ready = validation_entropy_sizing(ready_gates, n_eff=80.0)
    assert ready["state_hint"] == "READY"
    assert ready["risk_multiplier"] == 1.0


def test_validation_entropy_sizing_locks_on_fatal_gate():
    gates = build_gate_results(
        walk_forward_pass_ratio=0.0,
        pbo=1.0,
        dsr_probability=0.0,
        overall_expectancy=-1.0,
        recent_expectancy=-1.0,
        historical_expectancy=1.0,
        overall_win_rate=0.1,
        recent_win_rate=0.0,
        calibration_new_score=0.0,
        calibration_old_score=1.0,
        fatal_reasons=["data_gap"],
    )
    locked = validation_entropy_sizing(gates, n_eff=100.0)
    assert locked["state_hint"] == "LOCKED"
    assert locked["risk_multiplier"] == 0.0


def test_build_gate_results_blocks_ready_when_quality_floor_not_met():
    gates = build_gate_results(
        walk_forward_pass_ratio=1.0,
        pbo=0.10,
        dsr_probability=0.99,
        overall_expectancy=0.08,
        recent_expectancy=0.07,
        historical_expectancy=0.10,
        overall_win_rate=0.32,
        recent_win_rate=0.28,
        calibration_new_score=0.50,
        calibration_old_score=0.20,
    )
    assert gates["quality"]["pass"] is False
    out = validation_entropy_sizing(gates, n_eff=90.0)
    assert out["state_hint"] != "READY"


def test_probe_controller_caps_notional_and_enforces_daily_limits(tmp_path):
    db_path = tmp_path / "probe.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(conn)

    assert compute_probe_notional(10_000, 0.01, 0.05, 0.02) == 5.0

    for idx in range(5):
        upsert_trade_ledger_records(
            conn,
            [
                {
                    "trade_id": f"probe-{idx}",
                    "symbol": "BTC-USDT",
                    "side": "long",
                    "entry_time": "2026-06-06T00:00:00Z",
                    "exit_time": "2026-06-06T01:00:00Z",
                    "entry_price": 100.0,
                    "exit_price": 99.0,
                    "stop_price": 98.0,
                    "target_price": 103.0,
                    "pnl_usdt": -1.0,
                    "pnl_R": -0.5,
                    "confluence_score": 8.0,
                    "probe": True,
                    "model_version": "smc_adaptive_v1",
                    "config_hash": "cfg-probe",
                }
            ],
        )
    conn.commit()

    blocked = plan_probe_order(
        conn,
        symbol="BTC-USDT",
        risk_multiplier=0.05,
        account_equity=10_000.0,
        base_risk_pct=0.01,
        stop_distance_pct=0.02,
        trade_time="2026-06-06T02:00:00Z",
    )
    conn.close()

    assert blocked["allow_order"] is False
    assert blocked["order_mode"] == "DRY_RUN"
    assert blocked["reason"] == "probe_daily_order_cap_reached"
