"""Feature denoising helpers for adaptive calibration."""

from __future__ import annotations

import numpy as np


def standardize_matrix(X) -> dict:
    arr = np.asarray(X, dtype=float)
    if arr.ndim != 2:
        raise ValueError("X must be 2-dimensional")
    means = np.nanmean(arr, axis=0)
    stds = np.nanstd(arr, axis=0)
    stds = np.where(stds <= 1e-8, 1.0, stds)
    z = (np.nan_to_num(arr, nan=0.0) - means) / stds
    return {"Xz": z, "means": means, "stds": stds}


def marchenko_pastur_eigen_clip(X, eps: float = 1e-8) -> dict:
    arr = np.asarray(X, dtype=float)
    if arr.ndim != 2:
        raise ValueError("X must be 2-dimensional")
    t_steps, n_features = arr.shape
    if t_steps == 0 or n_features == 0:
        raise ValueError("X must be non-empty")

    scaled = standardize_matrix(arr)
    xz = scaled["Xz"]

    cov = (xz.T @ xz) / max(t_steps - 1, 1)
    denom = np.sqrt(np.maximum(np.diag(cov), eps))
    corr = cov / np.outer(denom, denom)
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    corr = 0.5 * (corr + corr.T)
    np.fill_diagonal(corr, 1.0)

    eigvals, eigvecs = np.linalg.eigh(corr)
    q = n_features / max(t_steps, 1)
    lambda_plus = (1.0 + np.sqrt(q)) ** 2

    noise_mask = eigvals <= lambda_plus
    clipped = eigvals.copy()
    if np.any(noise_mask):
        clipped[noise_mask] = float(np.mean(eigvals[noise_mask]))
    clipped = np.maximum(clipped, eps)

    denoised = eigvecs @ np.diag(clipped) @ eigvecs.T
    denoised = 0.5 * (denoised + denoised.T)

    diag = np.sqrt(np.maximum(np.diag(denoised), eps))
    denoised_corr = denoised / np.outer(diag, diag)
    denoised_corr = np.clip(denoised_corr, -1.0, 1.0)
    np.fill_diagonal(denoised_corr, 1.0)

    return {
        "denoised_corr": denoised_corr,
        "raw_corr": corr,
        "eigvals": eigvals,
        "eigvals_clipped": clipped,
        "lambda_plus": float(lambda_plus),
        "means": scaled["means"],
        "stds": scaled["stds"],
    }
