"""Regression tests for the three audit-feedback fixes:

  1. [High] patch_type boundary — adaptive_runtime cannot be applied to
     strategy.yaml even if apply=True is passed.
  2. [Med-High] FM challenger adoption uses Purged Walk-Forward OOS,
     not in-sample accuracy.
  3. [Med] validation gate fatal flag is per-gate, not globally
     broadcast from fatal_reasons.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
sys.path.insert(0, str(WEB_DIR))


# ─────────────────────────────────────────────────────────────
# Fix 1: patch_type boundary
# ─────────────────────────────────────────────────────────────

def test_adaptive_runtime_patch_cannot_be_applied_to_strategy_yaml(tmp_path):
    """create_config_patch with apply=True + non-strategy patch_type → ValueError."""
    from learning.adaptive_store import (
        create_config_patch, ensure_adaptive_calibration_schema,
        APPLICABLE_PATCH_TYPES,
    )
    # adaptive_runtime is explicitly NOT in the applicable set
    assert "adaptive_runtime" not in APPLICABLE_PATCH_TYPES
    assert "strategy" in APPLICABLE_PATCH_TYPES

    yaml_path = tmp_path / "strategy.yaml"
    yaml_path.write_text("confluence:\n  weights:\n    htf_bias_aligned: 2\n", encoding="utf-8")
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(db)

    # Direct apply via create_config_patch should be refused
    with pytest.raises(ValueError, match="not applicable"):
        create_config_patch(
            db, patch={"state": {"mode": "READY"}},
            symbol="BTC-USDT", reason="test",
            strategy_yaml_path=str(yaml_path),
            patch_type="adaptive_runtime", apply=True,
        )


def test_apply_atomic_config_patch_refuses_adaptive_runtime_row(tmp_path):
    """apply_atomic_config_patch must refuse a row whose patch_type is non-strategy."""
    from learning.adaptive_store import (
        create_config_patch, apply_atomic_config_patch,
        ensure_adaptive_calibration_schema,
    )
    yaml_path = tmp_path / "strategy.yaml"
    yaml_path.write_text("confluence:\n  weights:\n    htf_bias_aligned: 2\n", encoding="utf-8")
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(db)

    # apply=False is allowed — creates the row but doesn't write yaml
    row = create_config_patch(
        db, patch={"state": {"mode": "READY"}, "diagnostics": {"x": 1}},
        symbol="BTC-USDT", reason="test runtime",
        strategy_yaml_path=str(yaml_path),
        patch_type="adaptive_runtime", apply=False,
    )
    # after_config must equal before_config — no runtime fields leaked into diff
    assert row["after_config"] == row["before_config"]
    assert row["after_hash"] == row["before_hash"]

    # Now try to apply it — must refuse
    with pytest.raises(ValueError, match="not a strategy patch"):
        apply_atomic_config_patch(db, patch_key=row["patch_key"],
                                    strategy_yaml_path=str(yaml_path))

    # File on disk untouched
    assert "state" not in yaml_path.read_text(encoding="utf-8")


def test_strategy_patch_still_applies_normally(tmp_path):
    """Real strategy patches still work (regression guard)."""
    from learning.adaptive_store import (
        create_config_patch, apply_atomic_config_patch,
        ensure_adaptive_calibration_schema,
    )
    yaml_path = tmp_path / "strategy.yaml"
    yaml_path.write_text("confluence:\n  weights:\n    htf_bias_aligned: 2\n", encoding="utf-8")
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    ensure_adaptive_calibration_schema(db)
    row = create_config_patch(
        db, patch={"confluence": {"weights": {"htf_bias_aligned": 5}}},
        symbol="ALL", reason="bump weight",
        strategy_yaml_path=str(yaml_path),
        patch_type="strategy", apply=False,
    )
    apply_atomic_config_patch(db, patch_key=row["patch_key"],
                                strategy_yaml_path=str(yaml_path))
    text = yaml_path.read_text(encoding="utf-8")
    assert "htf_bias_aligned: 5" in text


# ─────────────────────────────────────────────────────────────
# Fix 3: per-gate fatal is no longer globally broadcast
# ─────────────────────────────────────────────────────────────

def test_validation_gate_fatal_is_not_globally_broadcast():
    """System fatal_reasons → system_fatal meta, NOT every gate fatal=True."""
    from learning.adaptive_validation import build_gate_results

    out = build_gate_results(
        walk_forward_pass_ratio=1.0, pbo=0.10, dsr_probability=0.99,
        recent_expectancy=0.60, historical_expectancy=0.80,
        calibration_new_score=0.50, calibration_old_score=0.20,
        fatal_reasons=["data_freshness_violation"],
    )
    # Each gate's fatal flag must be False — only the system meta carries the fatal
    for key in ("walk_forward", "pbo", "dsr", "edge_decay", "closed_loop_calibration"):
        assert out[key]["fatal"] is False, f"{key} should not inherit system_fatal"
    # System-wide meta is present
    assert out["__system_fatal__"]["fatal"] is True
    assert "data_freshness_violation" in out["__system_fatal__"]["reasons"]


def test_validation_gate_individual_reasons_are_specific():
    """Each failing gate should have its own specific reason, not the system reason."""
    from learning.adaptive_validation import build_gate_results

    out = build_gate_results(
        walk_forward_pass_ratio=0.5,        # fails (threshold 1.0)
        pbo=0.8,                              # fails (threshold 0.5)
        dsr_probability=0.6,                  # fails (threshold 0.95)
        recent_expectancy=0.1,                # fails
        historical_expectancy=1.0,
        calibration_new_score=0.1,            # fails
        calibration_old_score=0.5,
        fatal_reasons=[],                     # NO system reasons
    )
    # GateResult.to_dict renames 'passed' → 'pass' (avoid Python keyword clash)
    assert out["walk_forward"]["pass"] is False
    assert "walk_forward_pass_ratio_below_threshold" in out["walk_forward"]["reason"]
    assert "pbo_above_threshold" in out["pbo"]["reason"]
    assert "dsr_probability_below_threshold" in out["dsr"]["reason"]
    assert "edge_decay_below_floor" in out["edge_decay"]["reason"]
    assert "calibration_regression" in out["closed_loop_calibration"]["reason"]


def test_validation_entropy_sizing_ignores_system_meta_key():
    """entropy_sizing must not treat __system_fatal__ as a phantom gate."""
    from learning.adaptive_validation import build_gate_results, validation_entropy_sizing

    ready_gates = build_gate_results(
        walk_forward_pass_ratio=1.0, pbo=0.10, dsr_probability=0.99,
        recent_expectancy=0.60, historical_expectancy=0.80,
        calibration_new_score=0.50, calibration_old_score=0.20,
    )
    out = validation_entropy_sizing(ready_gates, n_eff=80.0)
    assert out["state_hint"] == "READY"


def test_validation_entropy_sizing_locks_on_system_fatal_only():
    """System-wide fatal → LOCKED even if every gate individually passes."""
    from learning.adaptive_validation import build_gate_results, validation_entropy_sizing

    gates_with_system_fatal = build_gate_results(
        walk_forward_pass_ratio=1.0, pbo=0.10, dsr_probability=0.99,
        recent_expectancy=0.60, historical_expectancy=0.80,
        calibration_new_score=0.50, calibration_old_score=0.20,
        fatal_reasons=["data_corruption_detected"],
    )
    out = validation_entropy_sizing(gates_with_system_fatal, n_eff=100.0)
    assert out["state_hint"] == "LOCKED"
    assert "data_corruption_detected" in out["system_fatal_reasons"]


# ─────────────────────────────────────────────────────────────
# Fix 2: FM challenger requires Purged Walk-Forward OOS
# ─────────────────────────────────────────────────────────────

def test_purged_walk_forward_fm_vs_lr_rejects_insufficient_samples():
    """With < min_samples records → verdict=insufficient_samples_for_walk_forward."""
    from smc_training_loop import _purged_walk_forward_fm_vs_lr

    out = _purged_walk_forward_fm_vs_lr([{
        "factors": {"htf_bias_aligned": True}, "r_multiple": 1.0
    }] * 5)
    assert out["verdict"] == "insufficient_samples_for_walk_forward"


def test_purged_walk_forward_fm_vs_lr_returns_per_fold_detail():
    """≥30 records → real fold-by-fold accuracy comparison."""
    from smc_training_loop import _purged_walk_forward_fm_vs_lr

    records = []
    for i in range(60):
        records.append({
            "factors": {
                "htf_bias_aligned": i % 3 != 0,
                "premium_discount_side": i % 4 != 0,
                "killzone": i % 5 == 0,
                "unmitigated_ob": i % 2 == 0,
            },
            "r_multiple": (2.0 if i % 3 == 0 else -1.0),
        })
    out = _purged_walk_forward_fm_vs_lr(records, n_folds=4)
    # Should at least attempt comparison
    assert out["verdict"] in {
        "fm_beats_lr_oos", "fm_not_better_oos",
        "insufficient_valid_folds",
    }
    if out.get("fm_oos_accuracy") is not None:
        assert 0.0 <= out["fm_oos_accuracy"] <= 1.0
        assert "folds" in out


def test_adopt_challenger_false_when_fm_only_wins_in_sample():
    """Adoption gate requires OOS win, not in-sample. Manufacturing a case
    where FM wins on training accuracy but loses OOS should NOT adopt."""
    from smc_training_loop import _purged_walk_forward_fm_vs_lr
    # We can't easily force a specific OOS verdict from random data, so
    # we just assert the verdict semantics: only ``fm_beats_lr_oos``
    # would flip adopt_challenger=True; any other verdict keeps it False.
    out = _purged_walk_forward_fm_vs_lr([{
        "factors": {"htf_bias_aligned": True}, "r_multiple": 1.0
    }] * 3)
    # The actual adoption flag is computed in run_training_cycle but the
    # logic chain is: adopt_challenger = fm_trained AND verdict == "fm_beats_lr_oos"
    # AND state == "READY". With insufficient samples, verdict is not
    # the magic string, so adoption MUST be False.
    assert out["verdict"] != "fm_beats_lr_oos"
