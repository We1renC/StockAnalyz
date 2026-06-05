"""Adaptive validation primitives for the SMC calibration loop."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    metric: float
    threshold: float
    severity: float
    fatal: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["pass"] = payload.pop("passed")
        return payload


def compute_sample_uniqueness(
    ledger: pd.DataFrame,
    bar_index: pd.DatetimeIndex,
    entry_col: str = "entry_time",
    exit_col: str = "exit_time",
) -> pd.Series:
    bars = pd.DatetimeIndex(bar_index).sort_values()
    n_bars = len(bars)
    if len(ledger) == 0:
        return pd.Series(dtype=float)
    if n_bars == 0:
        raise ValueError("bar_index is empty")

    starts = np.searchsorted(
        bars.values,
        pd.to_datetime(ledger[entry_col]).values,
        side="left",
    )
    ends = np.searchsorted(
        bars.values,
        pd.to_datetime(ledger[exit_col]).values,
        side="right",
    ) - 1
    starts = np.clip(starts, 0, n_bars - 1)
    ends = np.clip(ends, 0, n_bars - 1)
    ends = np.maximum(ends, starts)

    concurrency = np.zeros(n_bars, dtype=float)
    for start, end in zip(starts, ends):
        concurrency[start : end + 1] += 1.0

    uniqueness = []
    for start, end in zip(starts, ends):
        current = np.maximum(concurrency[start : end + 1], 1.0)
        uniqueness.append(float(np.mean(1.0 / current)))
    return pd.Series(uniqueness, index=ledger.index, name="sample_uniqueness")


def effective_sample_size(weights: np.ndarray, eps: float = 1e-12) -> float:
    seq = np.asarray(weights, dtype=float)
    seq = np.maximum(seq, 0.0)
    if seq.sum() <= eps:
        return 0.0
    return float((seq.sum() ** 2) / (np.sum(seq ** 2) + eps))


class PurgedWalkForwardSplit:
    def __init__(
        self,
        n_splits: int = 4,
        embargo_bars: int = 5,
        entry_col: str = "entry_time",
        exit_col: str = "exit_time",
    ):
        self.n_splits = max(1, int(n_splits))
        self.embargo_bars = max(0, int(embargo_bars))
        self.entry_col = entry_col
        self.exit_col = exit_col

    def _prepare(self, ledger: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
        frame = ledger.sort_values(self.entry_col).reset_index(drop=True)
        entries = pd.to_datetime(frame[self.entry_col]).values
        exits = pd.to_datetime(frame[self.exit_col]).values
        indices = np.arange(len(frame))
        return frame, indices, entries, exits

    def split(self, ledger: pd.DataFrame):
        for fold in self.split_with_meta(ledger):
            yield fold["train_idx"], fold["test_idx"]

    def split_with_meta(self, ledger: pd.DataFrame, *, min_train_samples: int = 10):
        if ledger.empty:
            return
        frame, indices, entries, exits = self._prepare(ledger)
        n = len(frame)
        fold_sizes = np.full(self.n_splits, n // self.n_splits, dtype=int)
        fold_sizes[: n % self.n_splits] += 1

        current = 0
        for fold_id, fold_size in enumerate(fold_sizes):
            if fold_size <= 0:
                continue
            test_start = current
            test_end = current + fold_size
            test_idx = indices[test_start:test_end]

            test_entry_min = entries[test_idx].min()
            test_exit_max = exits[test_idx].max()

            train_mask = np.ones(n, dtype=bool)
            train_mask[test_idx] = False

            overlaps = (entries <= test_exit_max) & (exits >= test_entry_min)
            train_mask[overlaps] = False

            embargo_start = test_end
            embargo_end = min(n, test_end + self.embargo_bars)
            train_mask[embargo_start:embargo_end] = False
            train_idx = indices[train_mask]

            reliable = len(train_idx) >= min_train_samples
            yield {
                "fold": fold_id,
                "train_idx": train_idx,
                "test_idx": test_idx,
                "reliable": reliable,
                "reason": "" if reliable else "insufficient_train_samples_after_purge",
            }
            current = test_end


def severity_walk_forward(pass_ratio: float, threshold: float = 1.0) -> float:
    if pass_ratio >= threshold:
        return 0.0
    return float(np.clip((threshold - pass_ratio) / max(threshold, 1e-12), 0.0, 1.0))


def severity_pbo(pbo: float, threshold: float = 0.50) -> float:
    if pbo <= threshold:
        return 0.0
    return float(np.clip((pbo - threshold) / (1.0 - threshold), 0.0, 1.0))


def severity_dsr(dsr_prob: float, threshold: float = 0.95) -> float:
    if dsr_prob >= threshold:
        return 0.0
    return float(np.clip((threshold - dsr_prob) / threshold, 0.0, 1.0))


def severity_edge_decay(
    recent_expectancy: float,
    historical_expectancy: float,
    floor_ratio: float = 0.50,
) -> float:
    required = historical_expectancy * floor_ratio
    if recent_expectancy >= required:
        return 0.0
    denom = max(abs(required), 1e-6)
    return float(np.clip((required - recent_expectancy) / denom, 0.0, 1.0))


def severity_calibration(new_score: float, old_score: float) -> float:
    if new_score > old_score:
        return 0.0
    denom = max(abs(old_score), 1e-6)
    return float(np.clip((old_score - new_score) / denom, 0.0, 1.0))


def build_gate_results(
    *,
    walk_forward_pass_ratio: float,
    walk_forward_threshold: float = 1.0,
    pbo: float,
    pbo_threshold: float = 0.50,
    dsr_probability: float,
    dsr_threshold: float = 0.95,
    recent_expectancy: float,
    historical_expectancy: float,
    edge_decay_floor_ratio: float = 0.50,
    calibration_new_score: float,
    calibration_old_score: float,
    calibration_threshold: float = 0.0,
    fatal_reasons: Iterable[str] | None = None,
) -> dict[str, dict]:
    reasons = [str(reason) for reason in (fatal_reasons or []) if reason]
    fatal = bool(reasons)
    out = {
        "walk_forward": GateResult(
            name="walk_forward",
            passed=walk_forward_pass_ratio >= walk_forward_threshold,
            metric=float(walk_forward_pass_ratio),
            threshold=float(walk_forward_threshold),
            severity=severity_walk_forward(walk_forward_pass_ratio, walk_forward_threshold),
            fatal=fatal,
            reason="; ".join(reasons),
        ),
        "pbo": GateResult(
            name="pbo",
            passed=float(pbo) <= float(pbo_threshold),
            metric=float(pbo),
            threshold=float(pbo_threshold),
            severity=severity_pbo(float(pbo), float(pbo_threshold)),
            fatal=fatal,
            reason="; ".join(reasons),
        ),
        "dsr": GateResult(
            name="dsr",
            passed=float(dsr_probability) >= float(dsr_threshold),
            metric=float(dsr_probability),
            threshold=float(dsr_threshold),
            severity=severity_dsr(float(dsr_probability), float(dsr_threshold)),
            fatal=fatal,
            reason="; ".join(reasons),
        ),
        "edge_decay": GateResult(
            name="edge_decay",
            passed=severity_edge_decay(
                float(recent_expectancy),
                float(historical_expectancy),
                float(edge_decay_floor_ratio),
            )
            == 0.0,
            metric=float(recent_expectancy),
            threshold=float(historical_expectancy) * float(edge_decay_floor_ratio),
            severity=severity_edge_decay(
                float(recent_expectancy),
                float(historical_expectancy),
                float(edge_decay_floor_ratio),
            ),
            fatal=fatal,
            reason="; ".join(reasons),
        ),
        "closed_loop_calibration": GateResult(
            name="closed_loop_calibration",
            passed=float(calibration_new_score) > float(calibration_old_score),
            metric=float(calibration_new_score),
            threshold=float(calibration_threshold),
            severity=severity_calibration(
                float(calibration_new_score),
                float(calibration_old_score),
            ),
            fatal=fatal,
            reason="; ".join(reasons),
        ),
    }
    return {key: value.to_dict() for key, value in out.items()}


def validation_entropy_sizing(
    gate_results: dict,
    n_eff: float,
    n_eff_probe_min: float = 20.0,
    n_eff_ready_min: float = 60.0,
    max_probe_multiplier: float = 0.10,
    eps: float = 1e-12,
) -> dict:
    if any(g.get("fatal", False) for g in gate_results.values()):
        return {
            "state_hint": "LOCKED",
            "risk_multiplier": 0.0,
            "entropy": 1.0,
            "amplitude": 1.0,
        }

    severities = np.array(
        [float(g.get("severity", 1.0)) for g in gate_results.values()],
        dtype=float,
    )
    severities = np.clip(severities, 0.0, 1.0)
    all_pass = all(bool(g.get("pass", False)) for g in gate_results.values())

    if all_pass and n_eff >= n_eff_ready_min:
        return {
            "state_hint": "READY",
            "risk_multiplier": 1.0,
            "entropy": 0.0,
            "amplitude": 0.0,
        }
    if n_eff < n_eff_probe_min:
        return {
            "state_hint": "DRY_RUN",
            "risk_multiplier": 0.0,
            "entropy": 1.0,
            "amplitude": float(np.mean(severities)) if len(severities) else 1.0,
        }

    total = float(np.sum(severities))
    if total <= eps or len(severities) <= 1:
        entropy = 0.0
    else:
        p = severities / total
        p = p[p > eps]
        entropy = -float(np.sum(p * np.log(p)) / np.log(len(severities)))
    amplitude = float(np.mean(severities)) if len(severities) else 1.0
    c_n = np.clip(
        (float(n_eff) - float(n_eff_probe_min))
        / max(float(n_eff_ready_min) - float(n_eff_probe_min), eps),
        0.0,
        1.0,
    )
    raw_multiplier = ((1.0 - entropy) ** 2) * ((1.0 - amplitude) ** 2) * c_n
    risk_multiplier = float(np.clip(raw_multiplier, 0.0, max_probe_multiplier))
    return {
        "state_hint": "VALIDATING_PROBE" if risk_multiplier > 0 else "DRY_RUN",
        "risk_multiplier": risk_multiplier,
        "entropy": entropy,
        "amplitude": amplitude,
    }
