"""Adaptive calibration orchestrator for patch-only inspection runs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from learning.adaptive_store import strategy_config_snapshot
from smc_quant import LedgerPaths
from smc_training_loop import TrainingResult, train_from_ledger


@dataclass
class AdaptiveCalibrationReport:
    ledger_path: str
    strategy_yaml_path: str
    symbol: str
    strategy_snapshot: dict
    training_result: dict
    runtime_patch_key: Optional[str]
    strategy_patch_key: Optional[str]


def build_adaptive_calibration_report(
    *,
    ledger_path: Optional[str] = None,
    strategy_yaml_path: str = "config/strategy.yaml",
    db_path: Optional[str] = None,
    symbol: str = "ALL",
) -> AdaptiveCalibrationReport:
    ledger_path = ledger_path or LedgerPaths.training_ledger()
    snapshot = strategy_config_snapshot(strategy_yaml_path)
    result = train_from_ledger(
        ledger_path=ledger_path,
        strategy_yaml_path=strategy_yaml_path,
        db_path=db_path,
        symbol=symbol,
        apply_strategy_patch=False,
    )
    return AdaptiveCalibrationReport(
        ledger_path=ledger_path,
        strategy_yaml_path=strategy_yaml_path,
        symbol=symbol,
        strategy_snapshot={
            "path": snapshot["path"],
            "hash": snapshot["hash"],
            "exists": snapshot["exists"],
        },
        training_result=asdict(result),
        runtime_patch_key=result.adaptive_patch_key,
        strategy_patch_key=result.strategy_patch_key,
    )
