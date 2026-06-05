import json
import sqlite3
from pathlib import Path

import yaml

from learning.adaptive_store import (
    apply_atomic_config_patch,
    create_config_patch,
    ensure_adaptive_calibration_schema,
    load_adaptive_audit_logs,
    rollback_config_patch,
    set_kill_switch_state,
    strategy_config_snapshot,
    upsert_trade_ledger_records,
)
from smc_training_loop import train_from_ledger


def _write_strategy_yaml(path: Path) -> None:
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
    return {
        "trade_id": f"btc-{i}",
        "symbol": "BTC-USDT",
        "side": "long",
        "entry_time": f"2026-06-{(i % 9) + 1:02d}T00:00:00",
        "exit_time": f"2026-06-{(i % 9) + 1:02d}T04:00:00",
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


def test_upsert_trade_ledger_records_persists_probe_and_config_trace(tmp_path):
    db_path = tmp_path / "adaptive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(conn)

    written = upsert_trade_ledger_records(
        conn,
        [
            {
                **_sample_record(1, factor_on=True, r_multiple=1.5),
                "probe": True,
                "config_hash": "cfg-xyz",
            }
        ],
    )
    conn.commit()

    row = conn.execute(
        """SELECT symbol, probe, config_hash, model_version, order_block_score,
                  htf_bias_score, risk_reward_score
             FROM smc_adaptive_trade_ledger"""
    ).fetchone()
    conn.close()

    assert written == 1
    assert row["symbol"] == "BTC-USDT"
    assert row["probe"] == 1
    assert row["config_hash"] == "cfg-xyz"
    assert row["model_version"] == "smc_adaptive_v1"
    assert row["order_block_score"] == 1.0
    assert row["htf_bias_score"] == 1.0
    assert row["risk_reward_score"] == 2.0


def test_config_patch_apply_and_rollback_restore_previous_yaml(tmp_path):
    strategy_yaml = tmp_path / "strategy.yaml"
    _write_strategy_yaml(strategy_yaml)

    conn = sqlite3.connect(tmp_path / "adaptive.db")
    conn.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(conn)

    before = strategy_config_snapshot(str(strategy_yaml))
    patch = create_config_patch(
        conn,
        patch={"confluence": {"threshold": 10}},
        symbol="BTC-USDT",
        reason="unit_test",
        strategy_yaml_path=str(strategy_yaml),
    )
    apply_atomic_config_patch(
        conn,
        patch_key=patch["patch_key"],
        strategy_yaml_path=str(strategy_yaml),
        expected_hash=before["hash"],
    )
    after_apply = strategy_config_snapshot(str(strategy_yaml))
    rollback_config_patch(
        conn,
        patch_key=patch["patch_key"],
        strategy_yaml_path=str(strategy_yaml),
    )
    after_rollback = strategy_config_snapshot(str(strategy_yaml))
    conn.close()

    assert after_apply["data"]["confluence"]["threshold"] == 10
    assert after_apply["hash"] != before["hash"]
    assert after_rollback["data"]["confluence"]["threshold"] == 8
    assert after_rollback["hash"] == before["hash"]


def test_train_from_ledger_records_patch_and_audit_rows(tmp_path):
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

    result = train_from_ledger(
        ledger_path=str(ledger_path),
        strategy_yaml_path=str(strategy_yaml),
        db_path=str(db_path),
        symbol="BTC-USDT",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    patch_row = conn.execute(
        "SELECT patch_key, applied FROM smc_adaptive_config_patches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    ledger_count = conn.execute(
        "SELECT COUNT(*) FROM smc_adaptive_trade_ledger"
    ).fetchone()[0]
    audit_rows = load_adaptive_audit_logs(conn, symbol="BTC-USDT", limit=10)
    conn.close()

    updated_yaml = strategy_config_snapshot(str(strategy_yaml))

    assert result.adopted is True
    assert result.strategy_yaml_updated is True
    assert patch_row is not None
    assert patch_row["applied"] == 1
    assert ledger_count == 12
    assert any(row["event_type"] == "config_patch_applied" for row in audit_rows)
    assert updated_yaml["data"]["confluence"]["weights"]["htf_bias_aligned"] > 2


def test_train_from_ledger_respects_locked_kill_switch(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    strategy_yaml = tmp_path / "strategy.yaml"
    db_path = tmp_path / "adaptive.db"
    _write_strategy_yaml(strategy_yaml)
    ledger_path.write_text(
        json.dumps(_sample_record(1, factor_on=True, r_multiple=1.0), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(conn)
    set_kill_switch_state(conn, state="LOCKED", reason="unit_test_lock")
    conn.commit()
    conn.close()

    result = train_from_ledger(
        ledger_path=str(ledger_path),
        strategy_yaml_path=str(strategy_yaml),
        db_path=str(db_path),
        symbol="BTC-USDT",
    )

    assert result.adopted is False
    assert result.strategy_yaml_updated is False
    assert any("kill switch locked" in note for note in result.notes)
