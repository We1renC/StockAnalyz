import numpy as np

from learning.feature_denoising import marchenko_pastur_eigen_clip
from learning.fm_challenger import choose_fm_embedding_dim, fit_factorization_machine_classifier
from learning.uniqueness_weighted_lr import (
    build_feature_matrix,
    build_trade_sample_weights,
    fit_uniqueness_weighted_lr,
)


def _records():
    rows = []
    for i in range(24):
        positive = i % 2 == 0
        rows.append(
            {
                "bos_score": 1.0 if positive else 0.1,
                "choch_score": 1.0 if positive else 0.2,
                "order_block_score": 0.9 if positive else 0.0,
                "fvg_score": 0.8 if positive else 0.1,
                "liquidity_sweep_score": 0.7 if positive else 0.2,
                "premium_discount_score": 0.6 if positive else 0.1,
                "htf_bias_score": 1.0 if positive else 0.0,
                "market_structure_score": 1.0 if positive else -1.0,
                "volume_imbalance_score": 0.8 if positive else 0.1,
                "session_score": 1.0,
                "volatility_regime_score": 0.3 + 0.02 * i,
                "risk_reward_score": 2.0 if positive else 0.7,
                "label": 1 if positive else 0,
                "pnl_R": 1.5 if positive else -0.8,
            }
        )
    return rows


def test_marchenko_pastur_denoising_returns_clean_correlation():
    X = build_feature_matrix(_records())["X"]
    out = marchenko_pastur_eigen_clip(X)
    denoised = np.asarray(out["denoised_corr"])
    assert denoised.shape[0] == denoised.shape[1]
    assert np.allclose(denoised, denoised.T)
    assert np.allclose(np.diag(denoised), 1.0)
    assert np.isfinite(denoised).all()
    assert out["lambda_plus"] > 0.0


def test_uniqueness_weighted_lr_uses_weighted_training_and_emits_proposal():
    block = build_feature_matrix(_records())
    uniqueness = np.linspace(0.5, 1.0, len(block["y"]))
    weights = build_trade_sample_weights(block["y"], uniqueness)
    fitted = fit_uniqueness_weighted_lr(
        block["X"],
        block["y"],
        weights,
        feature_cols=block["feature_cols"],
    )
    assert fitted["trained"] is True
    assert fitted["diagnostics"]["accuracy"] >= 0.5
    assert "bos_score" in fitted["proposal"]
    assert fitted["sample_weight_summary"]["n_eff"] <= len(block["y"])


def test_fm_challenger_respects_n_eff_gate_and_outputs_interactions():
    block = build_feature_matrix(_records())
    weights = build_trade_sample_weights(block["y"], np.ones(len(block["y"])))

    blocked = fit_factorization_machine_classifier(
        block["X"],
        block["y"],
        weights,
        n_eff=20.0,
        feature_cols=block["feature_cols"],
    )
    assert blocked["trained"] is False
    assert blocked["reason"] == "n_eff_below_minimum"

    trained = fit_factorization_machine_classifier(
        block["X"],
        block["y"],
        weights,
        n_eff=40.0,
        feature_cols=block["feature_cols"],
    )
    assert trained["trained"] is True
    assert trained["embedding_dim"] == choose_fm_embedding_dim(40.0, len(block["feature_cols"]))
    assert trained["diagnostics"]["accuracy"] >= 0.5
    assert len(trained["top_interactions"]) > 0
