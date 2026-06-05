import json
import sqlite3
from datetime import datetime, timedelta

import yaml
from learning.adaptive_store import strategy_config_snapshot
from smc_adaptive_orchestrator import build_adaptive_calibration_report


def _write_strategy_yaml(path):
    path.write_text(
        yaml.safe_dump(
            {
                "confluence": {
                    "threshold": 8,
                    "weights": {
                        "htf_bias_aligned": 2,
                        "premium_discount_side": 2,
                        "unmitigated_ob": 2,
                        "unfilled_fvg": 1,
                        "liquidity_swept": 2,
                        "ltf_choch": 2,
                        "ote_zone": 1,
                        "killzone": 1,
                        "volume_displacement": 1,
                        "strong_dol_target": 1,
                        "poi_displacement_missing": -2,
                    },
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _sample_record(i: int, *, factor_on: bool, r_multiple: float) -> dict:
    entry_dt = datetime(2026, 6, 1, 0, 0, 0) + timedelta(hours=i * 6)
    exit_dt = entry_dt + timedelta(hours=4)
    return {
        "trade_id": f"btc-{i}",
        "symbol": "BTC-USDT",
        "side": "long",
        "entry_time": entry_dt.isoformat(),
        "exit_time": exit_dt.isoformat(),
        "entry_price": 100.0 + i,
        "exit_price": 101.0 + i,
        "stop_price": 98.0 + i,
        "target_price": 104.0 + i,
        "pnl_usdt": 50.0 * r_multiple,
        "pnl_R": r_multiple,
        "r_multiple": r_multiple,
        "label": 1 if r_multiple > 0 else 0,
        "confluence_score": 9.0,
        "probe": False,
        "model_version": "smc_adaptive_v1",
        "config_hash": "cfg-001",
        "rr_planned": 2.0,
        "factors": {
            "htf_bias_aligned": factor_on,
            "unmitigated_ob": factor_on,
            "killzone": True,
        },
        "regime": {"volatility_score": 0.25},
    }


def test_adaptive_orchestrator_stages_patches_without_mutating_strategy_yaml(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    strategy_yaml = tmp_path / "strategy.yaml"
    db_path = tmp_path / "adaptive.db"
    _write_strategy_yaml(strategy_yaml)

    records = []
    for i in range(6):
        records.append(_sample_record(i, factor_on=True, r_multiple=2.0))
    for i in range(6, 12):
        records.append(_sample_record(i, factor_on=False, r_multiple=0.5))
    ledger_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n",
        encoding="utf-8",
    )

    before = strategy_config_snapshot(str(strategy_yaml))
    report = build_adaptive_calibration_report(
        ledger_path=str(ledger_path),
        strategy_yaml_path=str(strategy_yaml),
        db_path=str(db_path),
        symbol="BTC-USDT",
    )
    after = strategy_config_snapshot(str(strategy_yaml))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    runtime_patch = conn.execute(
        "SELECT patch_type, applied FROM smc_adaptive_config_patches WHERE patch_key=?",
        (report.runtime_patch_key,),
    ).fetchone()
    strategy_patch = None
    if report.strategy_patch_key:
        strategy_patch = conn.execute(
            "SELECT patch_type, applied FROM smc_adaptive_config_patches WHERE patch_key=?",
            (report.strategy_patch_key,),
        ).fetchone()
    conn.close()

    assert before["hash"] == after["hash"]
    assert report.training_result["strategy_yaml_updated"] is False
    assert runtime_patch["patch_type"] == "adaptive_runtime"
    assert runtime_patch["applied"] == 0
    if strategy_patch is not None:
        assert strategy_patch["patch_type"] == "strategy"
        assert strategy_patch["applied"] == 0
