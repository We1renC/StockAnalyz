"""Pure-NumPy factorization-machine challenger."""

from __future__ import annotations

from typing import Optional

import numpy as np

from learning.feature_denoising import standardize_matrix


def choose_fm_embedding_dim(n_eff: float, n_features: int = 12) -> int:
    if n_eff < 30:
        return 2
    if n_eff < 60:
        return 3
    if n_eff < 120:
        return 4
    return min(6, max(2, n_features // 2))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))


def fit_factorization_machine_classifier(
    X,
    y,
    sample_weight,
    *,
    n_eff: float,
    feature_cols: Optional[list[str]] = None,
    epochs: int = 400,
    learning_rate: float = 0.03,
    weight_decay: float = 0.005,
    gradient_clip: float = 0.2,
    early_stopping_rounds: int = 20,
) -> dict:
    X_arr = np.asarray(X, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    w = np.asarray(sample_weight, dtype=float)
    cols = list(feature_cols or [f"x{i}" for i in range(X_arr.shape[1] if X_arr.ndim == 2 else 0)])

    if X_arr.ndim != 2 or len(X_arr) == 0:
        return {"trained": False, "reason": "empty_dataset", "interaction_strength_matrix": []}
    if float(n_eff) < 25.0:
        return {"trained": False, "reason": "n_eff_below_minimum", "interaction_strength_matrix": []}

    scaled = standardize_matrix(X_arr)
    Xz = scaled["Xz"]
    n, k = Xz.shape
    emb_dim = choose_fm_embedding_dim(float(n_eff), k)

    rng = np.random.default_rng(42)
    bias = 0.0
    linear = np.zeros(k, dtype=float)
    V = rng.normal(0.0, 0.01, size=(k, emb_dim))

    best = None
    best_loss = float("inf")
    stale = 0

    for _ in range(max(1, int(epochs))):
        xv = Xz @ V
        interaction = 0.5 * np.sum(xv ** 2 - (Xz ** 2) @ (V ** 2), axis=1)
        logits = bias + Xz @ linear + interaction
        probs = _sigmoid(logits)
        error = probs - y_arr
        weighted_error = w * error

        denom = max(np.sum(w), 1e-8)
        grad_bias = float(np.sum(weighted_error) / denom)
        grad_linear = (Xz.T @ weighted_error) / denom + weight_decay * linear
        interaction_grad = Xz.T @ (weighted_error[:, None] * xv) - ((Xz ** 2).T @ weighted_error)[:, None] * V
        grad_V = interaction_grad / denom + weight_decay * V

        grad_bias = float(np.clip(grad_bias, -gradient_clip, gradient_clip))
        grad_linear = np.clip(grad_linear, -gradient_clip, gradient_clip)
        grad_V = np.clip(grad_V, -gradient_clip, gradient_clip)

        bias -= learning_rate * grad_bias
        linear -= learning_rate * grad_linear
        V -= learning_rate * grad_V

        p_clip = np.clip(probs, 1e-9, 1.0 - 1e-9)
        loss = float(
            np.sum(w * (-(y_arr * np.log(p_clip) + (1 - y_arr) * np.log(1 - p_clip)))) / denom
            + 0.5 * weight_decay * (np.sum(linear ** 2) + np.sum(V ** 2))
        )
        if loss + 1e-8 < best_loss:
            best_loss = loss
            best = (bias, linear.copy(), V.copy())
            stale = 0
        else:
            stale += 1
            if stale >= early_stopping_rounds:
                break

    if best is None:
        best = (bias, linear, V)
    bias, linear, V = best

    xv = Xz @ V
    interaction = 0.5 * np.sum(xv ** 2 - (Xz ** 2) @ (V ** 2), axis=1)
    logits = bias + Xz @ linear + interaction
    probs = _sigmoid(logits)
    preds = (probs >= 0.5).astype(int)
    accuracy = float(np.mean(preds == y_arr))

    interaction_strength = np.abs(V @ V.T)
    np.fill_diagonal(interaction_strength, 0.0)
    ranked_pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            ranked_pairs.append((cols[i], cols[j], float(interaction_strength[i, j])))
    ranked_pairs.sort(key=lambda item: item[2], reverse=True)

    return {
        "trained": True,
        "embedding_dim": emb_dim,
        "bias": float(bias),
        "linear_weights": {col: float(linear[idx]) for idx, col in enumerate(cols)},
        "interaction_strength_matrix": interaction_strength.tolist(),
        "top_interactions": [
            {"left": left, "right": right, "strength": round(strength, 6)}
            for left, right, strength in ranked_pairs[:10]
        ],
        "diagnostics": {
            "sample_size": n,
            "n_eff": float(n_eff),
            "accuracy": round(accuracy, 4),
            "log_loss": round(best_loss, 6),
            "early_stopped": stale >= early_stopping_rounds,
        },
    }
