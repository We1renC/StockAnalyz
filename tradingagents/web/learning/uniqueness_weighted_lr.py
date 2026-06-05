"""Uniqueness-weighted logistic regression in pure NumPy."""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from learning.adaptive_store import FEATURE_COLUMNS
from learning.feature_denoising import standardize_matrix


def build_feature_matrix(records: list[dict], feature_cols: Optional[list[str]] = None) -> dict:
    columns = list(feature_cols or FEATURE_COLUMNS)
    X = np.zeros((len(records or []), len(columns)), dtype=float)
    y = np.zeros(len(records or []), dtype=int)
    for i, record in enumerate(records or []):
        for j, col in enumerate(columns):
            X[i, j] = float(record.get(col) or 0.0)
        y[i] = 1 if float(record.get("label", 1 if float(record.get("pnl_R") or record.get("r_multiple") or 0.0) > 0 else 0)) > 0 else 0
    return {"X": X, "y": y, "feature_cols": columns}


def build_trade_sample_weights(
    y: np.ndarray,
    uniqueness: np.ndarray,
    half_life_trades: int = 100,
    class_balance: bool = True,
) -> np.ndarray:
    y_arr = np.asarray(y).astype(int)
    u = np.asarray(uniqueness).astype(float)
    n = len(y_arr)
    if n == 0:
        return np.asarray([], dtype=float)
    age = np.arange(n)[::-1]
    recency = 0.5 ** (age / max(int(half_life_trades), 1))
    w = u * recency

    if class_balance:
        pos = max(int(np.sum(y_arr == 1)), 1)
        neg = max(int(np.sum(y_arr == 0)), 1)
        class_w = np.where(y_arr == 1, n / (2.0 * pos), n / (2.0 * neg))
        w *= class_w

    w = np.maximum(w, 1e-8)
    w /= np.mean(w)
    return w


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))


def fit_uniqueness_weighted_lr(
    X,
    y,
    sample_weight,
    *,
    l2_penalty: float = 0.1,
    learning_rate: float = 0.15,
    epochs: int = 1200,
    max_delta: float = 0.12,
    feature_cols: Optional[list[str]] = None,
) -> dict:
    X_arr = np.asarray(X, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    w = np.asarray(sample_weight, dtype=float)
    if X_arr.ndim != 2:
        raise ValueError("X must be 2-dimensional")
    if len(X_arr) == 0:
        return {
            "coefficients": {},
            "intercept": 0.0,
            "proposal": {},
            "diagnostics": {"sample_size": 0},
        }

    scaled = standardize_matrix(X_arr)
    Xz = scaled["Xz"]
    n, k = Xz.shape
    theta = np.zeros(k, dtype=float)
    bias = 0.0
    prev_loss = None

    for _ in range(max(1, int(epochs))):
        z = Xz @ theta + bias
        p = _sigmoid(z)
        error = p - y_arr
        weighted_error = w * error

        grad_theta = (Xz.T @ weighted_error) / max(np.sum(w), 1e-8) + l2_penalty * theta
        grad_bias = float(np.sum(weighted_error) / max(np.sum(w), 1e-8))

        delta_theta = np.clip(learning_rate * grad_theta, -max_delta, max_delta)
        delta_bias = float(np.clip(learning_rate * grad_bias, -max_delta, max_delta))
        theta -= delta_theta
        bias -= delta_bias

        p_clip = np.clip(p, 1e-9, 1.0 - 1e-9)
        loss = float(
            np.sum(w * (-(y_arr * np.log(p_clip) + (1 - y_arr) * np.log(1 - p_clip))))
            / max(np.sum(w), 1e-8)
            + 0.5 * l2_penalty * np.sum(theta ** 2)
        )
        if prev_loss is not None and abs(prev_loss - loss) < 1e-8:
            break
        prev_loss = loss

    preds = (_sigmoid(Xz @ theta + bias) >= 0.5).astype(int)
    accuracy = float(np.mean(preds == y_arr))
    cols = list(feature_cols or [f"x{i}" for i in range(k)])
    coefficients = {col: float(theta[idx]) for idx, col in enumerate(cols)}
    proposal = {}
    for idx, col in enumerate(cols):
        proposal[col] = float(np.clip(theta[idx], -1.5, 1.5))
    return {
        "trained": True,
        "coefficients": coefficients,
        "intercept": float(bias),
        "proposal": proposal,
        "sample_weight_summary": {
            "min": round(float(np.min(w)), 6),
            "max": round(float(np.max(w)), 6),
            "mean": round(float(np.mean(w)), 6),
            "n_eff": round(float((np.sum(w) ** 2) / max(np.sum(w ** 2), 1e-8)), 4),
        },
        "diagnostics": {
            "sample_size": n,
            "accuracy": round(accuracy, 4),
            "log_loss": round(prev_loss or 0.0, 6),
            "weight_mean": round(float(np.mean(w)), 6),
            "weight_std": round(float(np.std(w)), 6),
            "feature_means": {col: float(scaled["means"][idx]) for idx, col in enumerate(cols)},
            "feature_stds": {col: float(scaled["stds"][idx]) for idx, col in enumerate(cols)},
        },
    }
