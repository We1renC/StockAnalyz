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


def test_preflight_and_run_symbol_in_validating_probe_mode(tmp_path):
    from smc_auto_workflow import preflight, run_symbol, load_latest_adaptive_runtime_patch
    from learning.adaptive_store import create_config_patch
    from smc_paper_runner import CryptoApiClient
    import json
    from unittest.mock import MagicMock

    db_path = str(tmp_path / "portfolio.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(conn)

    # 1. 建立一個 VALIDATING_PROBE 的 runtime patch
    patch_data = {
        "state": {
            "mode": "VALIDATING_PROBE",
            "adopt_weights": False,
            "n_eff": 15.0,
            "validation_entropy": 0.8,
            "validation_amplitude": 1.0,
        },
        "risk": {
            "risk_multiplier": 0.03,
            "probe_notional_cap_usdt": 5.0,
        },
        "strategy": {
            "confluence_min_score": 10.0,
        },
        "model": {},
        "diagnostics": {}
    }
    create_config_patch(
        conn,
        patch=patch_data,
        symbol="BTC-USDT",
        reason="test_probe",
        strategy_yaml_path="config/strategy.yaml",
        patch_type="adaptive_runtime",
        apply=False,
    )
    conn.commit()

    # 2. 驗證 preflight 在沒有歷史的情況下依然返回 allowed_live=True
    verdict = preflight(conn, "BTC-USDT")
    assert verdict.allowed_live is True
    assert verdict.reason == "validating_probe_mode"

    conn.close()

    # 3. 測試 run_symbol 呼叫時，帶入 patch 參數
    api = MagicMock()
    # 模擬 klines 回傳
    api.klines.return_value = {
        "status": 200,
        "payload": {
            "data": [
                {
                    "open_time": f"2026-06-06T00:{idx:02d}:00Z",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "volume": 10.0,
                }
                for idx in range(35)
            ]
        }
    }
    # 模擬 ticker
    api.ticker.return_value = {"status": 200, "payload": {"price": "100.0"}}
    api.create_order.return_value = {"status": 200, "payload": {"id": "order-123"}}

    # 我們可以使用 patch 來監控 UnifiedTradingSession 的初始化或 UnifiedSessionConfig 的生成
    from unittest.mock import patch as mock_patch

    # 跑 run_symbol 看看
    # 因為 run_symbol 會寫 journal，我們可以把 journal_dir 指向 tmp_path
    journal_dir = str(tmp_path / "smc_auto_test")

    with mock_patch("smc_auto_workflow.UnifiedTradingSession") as MockSession:
        mock_sess_inst = MockSession.return_value
        mock_sess_inst.run.return_value = {"decisions": []}
        
        run_symbol(
            api,
            "BTC-USDT",
            db_path=db_path,
            journal_dir=journal_dir,
            ignore_cooldown=True,
        )
        
        # 取得傳入 UnifiedTradingSession 的 config
        cfg = MockSession.call_args[0][1]
        # 驗證 config 中的引數是否有成功套用 VALIDATING_PROBE 規則
        # 正常 major (BTC) 的 risk_pct 是 0.02
        # VALIDATING_PROBE 下 risk_pct = 0.02 * 0.03 = 0.0006
        assert abs(cfg.risk_pct - 0.0006) < 1e-9
        assert cfg.max_notional_usdt == 5.0
        assert cfg.min_confluence_score == 10
        assert cfg.probe is True

