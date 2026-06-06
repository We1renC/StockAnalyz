from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parents[1] / "web"
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))


def _wf_records() -> list[dict]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records: list[dict] = []
    # Dominant regime: high-score / high-RR entries are stable winners.
    for i in range(36):
        records.append(
            {
                "entry_time": (base + timedelta(hours=i)).isoformat(),
                "confluence_score": 9,
                "rr_planned": 2.5,
                "outcome": "target",
                "r_multiple": 1.2 + (i % 4) * 0.2,
            }
        )
    # Low-score / low-RR bucket bleeds over time and should be filtered out.
    for i in range(36, 72):
        records.append(
            {
                "entry_time": (base + timedelta(hours=i)).isoformat(),
                "confluence_score": 6,
                "rr_planned": 1.5,
                "outcome": "stop" if i % 3 else "target",
                "r_multiple": -1.0 if i % 3 else 0.2,
            }
        )
    return records


def test_sweep_walk_forward_picks_oos_resilient_cell():
    from learning.hyperparameter_sweep import sweep_walk_forward

    sweep = sweep_walk_forward(
        _wf_records(),
        n_folds=4,
        purge_size=1,
        min_trades_per_fold=8,
    )

    assert sweep["status"] == "ok"
    assert sweep["best"] is not None
    best = sweep["best"]
    assert best["score"]["sharpe"] > 0
    assert best["walk_forward"]["folds_with_trades"] >= 2
    excludes_losers = best["min_score"] >= 7 or best["min_rr"] >= 2.0
    assert excludes_losers
    assert len(sweep["per_fold"]) == 4


def test_sweep_walk_forward_requires_initial_train_window():
    from learning.hyperparameter_sweep import sweep_walk_forward

    records = _wf_records()[:30]
    sweep = sweep_walk_forward(records, n_folds=4, min_trades_per_fold=8)

    assert sweep["status"] == "insufficient_data"
    assert sweep["best"] is None


def test_auto_apply_sweep_prefers_walk_forward(monkeypatch, tmp_path):
    import learning.hyperparameter_sweep as hs
    from learning.sweep_auto_apply import auto_apply_sweep

    monkeypatch.setattr(
        hs,
        "sweep_walk_forward",
        lambda records: {
            "status": "ok",
            "best": {
                "min_score": 8,
                "min_rr": 2.0,
                "risk_pct": 1.0,
                "score": {"sharpe": 0.45, "n_trades": 24},
            },
        },
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("in-sample fallback should not be used when walk-forward is ok")

    monkeypatch.setattr(hs, "sweep_hyperparameters", _boom)

    profile = tmp_path / "profile.yaml"
    profile.write_text("min_score: 6\nmin_rr: 1.5\nrisk_pct: 0.5\n", encoding="utf-8")

    out = auto_apply_sweep(records=_wf_records(), profile_path=str(profile))

    assert out["applied"] is True
    assert out["after"]["min_score"] == 8
    assert out["after"]["min_rr"] == 2.0
