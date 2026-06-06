# SMC 自適應量化交易系統：VALIDATING 解鎖與自適應校準開發規格

版本：v1.0  
目標系統：基於 SMC 多因子 Confluence 的加密貨幣自適應量化交易系統  
核心目標：在不放棄防禦性驗證的前提下，避免系統於冷啟動期長期卡死於 `VALIDATING`，並建立非人工干預的前向樣本探索、模型校準、門檻調節與風險縮放機制。

---

## 1. 開發目標摘要

目前系統的主要問題不是單一模型失效，而是驗證機制過度二元化：

```text
新權重未通過 5 Gates → 系統維持 VALIDATING → 禁止模擬送單 → 無法累積前向樣本 → Gates 更難通過
```

本次開發要將現有架構改造成：

```text
Gates 決定是否採用新權重
Gate severity 決定是否允許極小倉位探索
DSR gap 決定 confluence 門檻
sample uniqueness 決定模型訓練權重
MP denoising 降低特徵共線性
FM 學習 SMC 因子二階交互
```

---

## 2. 本次應開發的模組清單

| 模組 | 目的 | 優先級 | 是否阻塞主流程 |
|---|---|---:|---:|
| `SampleUniquenessEngine` | 計算交易重疊度與樣本獨特性權重 | P0 | 是 |
| `PurgedWalkForwardSplitter` | 避免持倉重疊造成 Walk-Forward 洩漏 | P0 | 是 |
| `GateSeverityEvaluator` | 將五大 Gates 從布林結果轉成 severity 訊號 | P0 | 是 |
| `ValidationEntropySizer` | 根據驗證失敗發散度縮放探索倉位 | P0 | 是 |
| `ProbeExecutionController` | 在 `VALIDATING_PROBE` 下以極小倉位進行前向探索 | P0 | 是 |
| `DSRInverseThresholdController` | 根據 DSR gap 自動調整 confluence 下單門檻 | P1 | 否 |
| `MarchenkoPasturDenoiser` | 對 12 個 SMC 特徵做相關矩陣降噪 | P1 | 否 |
| `UniquenessWeightedLR` | 用 uniqueness sample weight 訓練 LR | P1 | 否 |
| `FactorizationMachineClassifier` | 學習 OB、FVG、Discount 等二階交互 | P2 | 否 |
| `AdaptiveCalibrationOrchestrator` | 串接所有模組並輸出 config patch | P0 | 是 |

---

## 3. 狀態機改造

### 3.1 現有問題

現有狀態過於簡化：

```text
VALIDATING → READY
```

只要五大 Gates 未全過，系統就不送單。這會造成冷啟動死鎖。

### 3.2 目標狀態機

應改為：

```text
DRY_RUN
  ↓ 樣本達到最低探索門檻，且無 fatal risk
VALIDATING_PROBE
  ↓ 5 Gates 全過，且有效樣本數足夠
READY
  ↓ 資料異常、下單異常、風控異常
LOCKED
```

### 3.3 狀態定義

| 狀態 | 條件 | 行為 |
|---|---|---|
| `DRY_RUN` | 樣本不足、有效樣本數不足、或 risk multiplier = 0 | 只做紙上模擬，不送單 |
| `VALIDATING_PROBE` | Gates 未全過，但無 fatal error，且有效樣本數達最低門檻 | 允許極小倉位模擬市場 / testnet 探索 |
| `READY` | 5 Gates 全過，且有效樣本數達正式交易門檻 | 允許正常模擬交易 |
| `LOCKED` | 資料缺口、PnL 異常、連續錯單、風控熔斷 | 停止交易與模型更新 |

### 3.4 不可變原則

```text
未通過 5 Gates，不得採用新權重。
未通過 5 Gates，只能進入 probe sizing，不得進入 full sizing。
VALIDATING_PROBE 的交易資料可進 ledger，但必須標記 probe=true。
Probe 樣本不得被視為 IID 樣本，仍需 sample uniqueness weighting。
```

---

## 4. 建議專案目錄結構

```text
src/
  adaptive/
    orchestrator.py
    config_patch.py
    state_machine.py

  validation/
    gate_result.py
    gate_severity.py
    validation_entropy.py
    purged_walk_forward.py
    dsr_inverse.py
    pbo.py
    dsr.py
    edge_decay.py

  sampling/
    concurrency.py
    uniqueness.py
    effective_sample_size.py

  models/
    uniqueness_weighted_lr.py
    factorization_machine.py
    model_registry.py
    feature_denoising.py

  execution/
    probe_controller.py
    risk_sizing.py
    kill_switch.py

  storage/
    ledger_schema.py
    strategy_config.py
    audit_log.py

configs/
  strategy.yaml
  adaptive.yaml

tests/
  test_sample_uniqueness.py
  test_purged_walk_forward.py
  test_validation_entropy.py
  test_dsr_inverse.py
  test_mp_denoising.py
  test_fm_classifier.py
  test_adaptive_orchestrator.py
```

---

## 5. Ledger 欄位規格

### 5.1 必要欄位

所有自適應模組依賴交易 ledger。請先標準化 ledger schema。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `trade_id` | string | 交易唯一 ID |
| `symbol` | string | 交易標的，例如 `BTCUSDT` |
| `side` | string | `long` / `short` |
| `entry_time` | datetime | 進場時間 |
| `exit_time` | datetime | 出場時間 |
| `entry_price` | float | 進場價 |
| `exit_price` | float | 出場價 |
| `stop_price` | float | 停損價 |
| `target_price` | float | 目標價 |
| `pnl_usdt` | float | 損益 USDT |
| `pnl_R` | float | R-multiple 損益 |
| `label` | int | 勝負標籤，贏 = 1，輸 = 0 |
| `confluence_score` | float | 下單時總分 |
| `probe` | bool | 是否為探索單 |
| `model_version` | string | 產生提案的模型版本 |
| `config_hash` | string | 當時策略設定 hash |

### 5.2 SMC 特徵欄位

建議固定 12 個特徵欄位：

```text
bos_score
choch_score
order_block_score
fvg_score
liquidity_sweep_score
premium_discount_score
htf_bias_score
market_structure_score
volume_imbalance_score
session_score
volatility_regime_score
risk_reward_score
```

若某特徵為 binary signal，仍建議轉成 `[0, 1]` 或 `[-1, 1]` 的連續分數，便於 LR / FM 學習。

---

## 6. 模組一：Sample Uniqueness Engine

### 6.1 開發目的

解決交易樣本 non-IID 問題。SMC 交易通常跨越多根 K 線，相鄰交易可能重疊持倉，不能直接把 50 筆交易當作 50 筆獨立樣本。

### 6.2 輸入

```python
ledger: pd.DataFrame
bar_index: pd.DatetimeIndex
```

### 6.3 輸出

```python
pd.Series  # index = ledger.index, value = uniqueness in (0, 1]
```

### 6.4 開發任務

- [ ] 根據 `entry_time` / `exit_time` 對應到 bar index。
- [ ] 計算每根 bar 的 concurrent trades 數量。
- [ ] 對每筆交易計算平均 uniqueness。
- [ ] 將 `sample_uniqueness` 寫回 ledger 或 learning dataset。
- [ ] 將 uniqueness 納入 LR / FM 的 `sample_weight`。
- [ ] 計算有效樣本數 `n_eff`，取代原始樣本數 `N`。

### 6.5 參考實作

```python
import numpy as np
import pandas as pd


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

    for s, e in zip(starts, ends):
        concurrency[s:e + 1] += 1.0

    uniqueness = []

    for s, e in zip(starts, ends):
        c = np.maximum(concurrency[s:e + 1], 1.0)
        uniqueness.append(float(np.mean(1.0 / c)))

    return pd.Series(uniqueness, index=ledger.index, name="sample_uniqueness")


def effective_sample_size(weights: np.ndarray, eps: float = 1e-12) -> float:
    w = np.asarray(weights, dtype=float)
    w = np.maximum(w, 0.0)

    if w.sum() <= eps:
        return 0.0

    return float((w.sum() ** 2) / (np.sum(w ** 2) + eps))
```

### 6.6 驗收條件

- [ ] 完全不重疊交易，`sample_uniqueness` 接近 1。
- [ ] 完全重疊的兩筆交易，各自 uniqueness 約為 0.5。
- [ ] `n_eff <= N`。
- [ ] 訓練 LR / FM 時必須使用 sample weight。

---

## 7. 模組二：Purged Walk-Forward Splitter

### 7.1 開發目的

解決 Walk-Forward 資料洩漏問題。若 train trade 的持倉時間與 test trade 重疊，則 train set 實際上看到了 test period 的市場路徑。

### 7.2 輸入

```python
ledger: pd.DataFrame
n_splits: int
embargo_bars: int
```

### 7.3 輸出

```python
Iterator[tuple[np.ndarray, np.ndarray]]  # train_idx, test_idx
```

### 7.4 開發任務

- [ ] 依時間順序切出 walk-forward folds。
- [ ] 對每個 test fold，移除與 test 持倉區間重疊的 train samples。
- [ ] 對 test fold 後方加入 embargo zone。
- [ ] 若 purge 後 train 樣本過少，該 fold 標記為 unreliable，而不是直接當作策略失敗。

### 7.5 參考實作

```python
import numpy as np
import pandas as pd


class PurgedWalkForwardSplit:
    def __init__(
        self,
        n_splits: int = 4,
        embargo_bars: int = 5,
        entry_col: str = "entry_time",
        exit_col: str = "exit_time",
    ):
        self.n_splits = n_splits
        self.embargo_bars = embargo_bars
        self.entry_col = entry_col
        self.exit_col = exit_col

    def split(self, ledger: pd.DataFrame):
        ledger = ledger.sort_values(self.entry_col).reset_index(drop=True)
        n = len(ledger)

        indices = np.arange(n)
        fold_sizes = np.full(self.n_splits, n // self.n_splits)
        fold_sizes[: n % self.n_splits] += 1

        entries = pd.to_datetime(ledger[self.entry_col]).values
        exits = pd.to_datetime(ledger[self.exit_col]).values

        current = 0

        for fold_size in fold_sizes:
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

            yield train_idx, test_idx

            current = test_end
```

### 7.6 驗收條件

- [ ] train set 與 test set 不存在持倉時間重疊。
- [ ] test fold 後方 embargo 區間不進 train set。
- [ ] 每個 fold 回傳 `train_idx` / `test_idx`。
- [ ] train 樣本不足時輸出 `fold_reliable=false`，不得把統計不足等同於策略失效。

---

## 8. 模組三：Gate Severity Evaluator

### 8.1 開發目的

將五大驗證 Gates 從布林開關改成連續風險訊號。

原本：

```json
{
  "walk_forward": false,
  "pbo": false,
  "dsr": false
}
```

目標：

```json
{
  "walk_forward": {
    "pass": false,
    "metric": -0.03,
    "threshold": 0.0,
    "severity": 0.42,
    "fatal": false
  }
}
```

### 8.2 GateResult schema

```python
from dataclasses import dataclass


@dataclass
class GateResult:
    name: str
    passed: bool
    metric: float
    threshold: float
    severity: float
    fatal: bool = False
    reason: str = ""
```

### 8.3 開發任務

- [ ] 為 Walk-Forward 計算 severity。
- [ ] 為 PBO 計算 severity。
- [ ] 為 DSR 計算 severity。
- [ ] 為 Edge Decay 計算 severity。
- [ ] 為 Closed-Loop Calibration 計算 severity。
- [ ] 若資料缺失、交易損益異常、執行錯誤，直接標記 `fatal=true`。

### 8.4 severity 函數

```python
import numpy as np


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
```

### 8.5 驗收條件

- [ ] 每個 Gate 一定輸出 `pass`、`metric`、`threshold`、`severity`、`fatal`。
- [ ] severity 介於 `[0, 1]`。
- [ ] fatal error 直接使狀態進入 `LOCKED`。
- [ ] Gate pass/fail 邏輯不被放寬；只新增連續控制訊號。

---

## 9. 模組四：Validation Entropy Sizing

### 9.1 開發目的

當 Gates 未全過時，不再一刀切阻斷，而是根據失敗分布與嚴重程度決定是否允許極小倉位探索。

### 9.2 輸入

```python
gate_results: dict[str, GateResult]
n_eff: float
```

### 9.3 輸出

```python
{
  "state_hint": "VALIDATING_PROBE",
  "risk_multiplier": 0.03,
  "entropy": 0.47,
  "amplitude": 0.31
}
```

### 9.4 開發任務

- [ ] 收集五大 Gates 的 severity。
- [ ] 計算 severity entropy。
- [ ] 計算平均失敗幅度 amplitude。
- [ ] 根據 `n_eff` 計算樣本可信度係數。
- [ ] 輸出 risk multiplier。
- [ ] 若任一 Gate fatal，直接輸出 `LOCKED` 與 `risk_multiplier=0`。

### 9.5 參考實作

```python
import numpy as np


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
            "amplitude": float(np.mean(severities)),
        }

    s_sum = float(np.sum(severities))

    if s_sum <= eps:
        entropy = 0.0
    else:
        p = severities / s_sum
        p = p[p > eps]
        entropy = -float(np.sum(p * np.log(p)) / np.log(len(severities)))

    amplitude = float(np.mean(severities))

    c_n = np.clip(
        (n_eff - n_eff_probe_min) / max(n_eff_ready_min - n_eff_probe_min, eps),
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
```

### 9.6 驗收條件

- [ ] Gates 全過且 `n_eff` 足夠時，`risk_multiplier=1.0`。
- [ ] Gates 未全過時，`risk_multiplier <= max_probe_multiplier`。
- [ ] `n_eff` 不足時不得送單。
- [ ] fatal risk 時直接 `LOCKED`。

---

## 10. 模組五：Probe Execution Controller

### 10.1 開發目的

讓系統在 `VALIDATING_PROBE` 狀態下仍能以極小部位進入模擬市場 / testnet，累積前向樣本，但不得影響正式權重採用。

### 10.2 輸入

```python
trade_proposal: dict
risk_multiplier: float
account_equity: float
base_risk_pct: float
stop_distance_pct: float
```

### 10.3 輸出

```python
{
  "allow_order": true,
  "notional_usdt": 5.0,
  "order_mode": "PROBE",
  "reason": "VALIDATING_PROBE sizing"
}
```

### 10.4 開發任務

- [ ] 新增 `order_mode`：`DRY_RUN` / `PROBE` / `NORMAL`。
- [ ] PROBE 單必須寫入 ledger，並標記 `probe=true`。
- [ ] PROBE 單不得超過 `probe_notional_cap_usdt`。
- [ ] PROBE 單每日數量需受限。
- [ ] PROBE 單每日虧損需受限。
- [ ] PROBE 單連續虧損需觸發冷卻。

### 10.5 參考實作

```python

def compute_probe_notional(
    equity_usdt: float,
    base_risk_pct: float,
    risk_multiplier: float,
    stop_distance_pct: float,
    probe_notional_cap_usdt: float = 5.0,
    min_notional_usdt: float = 1.0,
) -> float:
    if risk_multiplier <= 0:
        return 0.0

    stop_distance_pct = max(float(stop_distance_pct), 1e-4)
    risk_budget = equity_usdt * base_risk_pct * risk_multiplier
    risk_based_notional = risk_budget / stop_distance_pct
    notional = min(risk_based_notional, probe_notional_cap_usdt)

    if notional < min_notional_usdt:
        return 0.0

    return float(notional)
```

### 10.6 Kill Switch

```yaml
risk_kill_switch:
  max_probe_orders_per_day: 5
  max_probe_daily_loss_usdt: 10.0
  max_consecutive_probe_losses: 3
  cooldown_minutes_after_probe_loss_streak: 240
  disable_on_data_gap: true
  disable_on_execution_error: true
  disable_on_slippage_outlier: true
```

### 10.7 驗收條件

- [ ] `VALIDATING_PROBE` 不得使用正常倉位。
- [ ] PROBE 單必須可追蹤、可審計、可排除或單獨分析。
- [ ] 任一 kill switch 觸發時，狀態切至 `LOCKED` 或 `DRY_RUN`。
- [ ] PROBE 交易結果不得直接導致權重 adoption。

---

## 11. 模組六：DSR-Inverse Threshold Controller

### 11.1 開發目的

當 DSR 顯著性不足時，不是直接停機，而是提高 confluence 下單門檻，降低雜訊交易進入樣本的比例。

### 11.2 輸入

```python
current_sr: float
sr_benchmark: float
n_eff: float
skew: float
kurtosis: float
current_threshold: float
```

### 11.3 輸出

```python
new_confluence_threshold: float
```

### 11.4 開發任務

- [ ] 實作 DSR probability 計算。
- [ ] 反推 `DSR >= 0.95` 所需最低 Sharpe。
- [ ] 計算 `required_sr - current_sr`。
- [ ] 根據 gap 調高 confluence threshold。
- [ ] 使用 smoothing，避免 threshold 震盪。
- [ ] 設定 threshold 上下界。

### 11.5 參考實作

```python
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


def dsr_probability(
    sr: float,
    sr_benchmark: float,
    n_eff: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    n_eff = max(float(n_eff), 2.0)
    denom = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * (sr ** 2)
    denom = np.sqrt(max(denom, 1e-12))
    z = ((sr - sr_benchmark) * np.sqrt(n_eff - 1.0)) / denom
    return float(norm.cdf(z))


def required_sharpe_for_dsr(
    target_prob: float,
    sr_benchmark: float,
    n_eff: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    target_prob = float(np.clip(target_prob, 1e-6, 1.0 - 1e-6))

    def objective(sr):
        return dsr_probability(sr, sr_benchmark, n_eff, skew, kurtosis) - target_prob

    return float(brentq(objective, -5.0, 10.0))


def update_confluence_threshold_by_dsr(
    current_threshold: float,
    base_threshold: float,
    max_threshold: float,
    current_sr: float,
    required_sr: float,
    k: float = 0.08,
    smoothing: float = 0.20,
) -> float:
    gap = max(0.0, required_sr - current_sr)
    proposed = base_threshold + k * np.tanh(gap)
    proposed = float(np.clip(proposed, base_threshold, max_threshold))

    new_threshold = (1.0 - smoothing) * current_threshold + smoothing * proposed
    return float(np.clip(new_threshold, base_threshold, max_threshold))
```

### 11.6 驗收條件

- [ ] DSR 不足時，threshold 單調上升或維持。
- [ ] DSR 達標時，threshold 不得低於 base threshold。
- [ ] threshold 不得超過 max threshold。
- [ ] 每次 threshold 變更必須寫入 audit log。

---

## 12. 模組七：Marchenko-Pastur Feature Denoising

### 12.1 開發目的

降低 12 個 SMC 特徵之間的多重共線性，使 LR 與 FM 的訓練更穩定。

### 12.2 輸入

```python
X: pd.DataFrame  # shape = [n_trades, 12]
```

### 12.3 輸出

```python
{
  "denoised_corr": np.ndarray,
  "lambda_plus": float,
  "eigvals": list,
  "eigvals_clipped": list
}
```

### 12.4 開發任務

- [ ] 對 12 個 SMC 特徵標準化。
- [ ] 計算 correlation matrix。
- [ ] 特徵分解。
- [ ] 計算 MP noise upper bound `lambda_plus`。
- [ ] 將小於 `lambda_plus` 的 eigenvalues clipping。
- [ ] 重構 denoised correlation matrix。
- [ ] 輸出 diagnostics。

### 12.5 參考實作

```python
import numpy as np
from sklearn.preprocessing import StandardScaler


def marchenko_pastur_eigen_clip(X, eps: float = 1e-8) -> dict:
    X_arr = np.asarray(X, dtype=float)
    T, N = X_arr.shape

    scaler = StandardScaler()
    Xz = scaler.fit_transform(X_arr)

    corr = np.corrcoef(Xz, rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    corr = 0.5 * (corr + corr.T)
    np.fill_diagonal(corr, 1.0)

    eigvals, eigvecs = np.linalg.eigh(corr)

    q = N / max(T, 1)
    lambda_plus = (1.0 + np.sqrt(q)) ** 2

    noise_mask = eigvals <= lambda_plus
    clipped = eigvals.copy()

    if np.any(noise_mask):
        clipped[noise_mask] = float(np.mean(eigvals[noise_mask]))

    clipped = np.maximum(clipped, eps)

    denoised = eigvecs @ np.diag(clipped) @ eigvecs.T
    denoised = 0.5 * (denoised + denoised.T)

    d = np.sqrt(np.maximum(np.diag(denoised), eps))
    denoised_corr = denoised / np.outer(d, d)
    denoised_corr = np.clip(denoised_corr, -1.0, 1.0)
    np.fill_diagonal(denoised_corr, 1.0)

    return {
        "denoised_corr": denoised_corr,
        "raw_corr": corr,
        "eigvals": eigvals,
        "eigvals_clipped": clipped,
        "lambda_plus": float(lambda_plus),
        "scaler": scaler,
    }
```

### 12.6 驗收條件

- [ ] denoised correlation matrix 對稱。
- [ ] 對角線為 1。
- [ ] 不存在 NaN / Inf。
- [ ] diagnostics 寫入 calibration report。

---

## 13. 模組八：Uniqueness-Weighted Logistic Regression

### 13.1 開發目的

用 sample uniqueness、recency weighting、class balance weighting 訓練 LR，使早期小樣本下的權重更新更保守。

### 13.2 輸入

```python
X: pd.DataFrame
y: np.ndarray
sample_uniqueness: np.ndarray
```

### 13.3 輸出

```python
trained_model
proposed_feature_weights
model_diagnostics
```

### 13.4 開發任務

- [ ] 計算 final sample weight。
- [ ] 加入 L2 regularization。
- [ ] 限制權重單次更新幅度。
- [ ] 輸出新權重 proposal。
- [ ] 權重 proposal 不直接寫入 `strategy.yaml`，只交給 Gates 驗證。

### 13.5 sample weight 組成

```text
sample_weight_i = uniqueness_i × recency_weight_i × class_balance_weight_i
```

### 13.6 參考實作

```python
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


def build_trade_sample_weights(
    y: np.ndarray,
    uniqueness: np.ndarray,
    half_life_trades: int = 100,
    class_balance: bool = True,
) -> np.ndarray:
    y = np.asarray(y).astype(int)
    u = np.asarray(uniqueness).astype(float)

    n = len(y)
    age = np.arange(n)[::-1]
    recency = 0.5 ** (age / max(half_life_trades, 1))

    w = u * recency

    if class_balance:
        pos = max(np.sum(y == 1), 1)
        neg = max(np.sum(y == 0), 1)
        class_w = np.where(y == 1, n / (2.0 * pos), n / (2.0 * neg))
        w *= class_w

    w = np.maximum(w, 1e-8)
    w /= np.mean(w)

    return w


def fit_uniqueness_weighted_lr(X, y, sample_weight, C: float = 0.25):
    model = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "lr",
                LogisticRegression(
                    penalty="l2",
                    C=C,
                    solver="lbfgs",
                    max_iter=2000,
                ),
            ),
        ]
    )

    model.fit(X, y, lr__sample_weight=sample_weight)
    return model
```

### 13.7 驗收條件

- [ ] LR 訓練必須使用 sample weight。
- [ ] 每次模型更新都輸出 `n_eff`。
- [ ] 權重變化超過上限時自動 clipping。
- [ ] 新權重 proposal 必須通過 Gates 才能採用。

---

## 14. 模組九：Factorization Machine Classifier

### 14.1 開發目的

取代純 LR 的線性假設，讓模型自動學習 SMC 因子二階交互，例如：

```text
Order Block × FVG
FVG × Discount
Liquidity Sweep × CHoCH
HTF Bias × BOS
Order Block × Premium/Discount
```

### 14.2 開發策略

FM 不應立即取代 LR 成為唯一模型。建議採用 challenger 模式：

```text
LR = baseline model
FM = challenger model

只有當 FM 在 Purged Walk-Forward 下明顯優於 LR，且通過 Gates，才允許 FM 成為 active model。
```

### 14.3 輸入

```python
X: pd.DataFrame
y: np.ndarray
sample_weight: np.ndarray
```

### 14.4 輸出

```python
fm_model
fm_diagnostics
interaction_strength_matrix
```

### 14.5 參考實作

```python
import torch
import torch.nn as nn
import torch.nn.functional as F


class FactorizationMachineClassifier(nn.Module):
    def __init__(self, n_features: int, k: int = 4, init_std: float = 0.01):
        super().__init__()
        self.bias = nn.Parameter(torch.zeros(1))
        self.linear = nn.Linear(n_features, 1, bias=False)
        self.V = nn.Parameter(torch.randn(n_features, k) * init_std)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        linear_part = self.linear(x).squeeze(-1)

        xv = x @ self.V
        interaction = 0.5 * torch.sum(
            xv ** 2 - ((x ** 2) @ (self.V ** 2)),
            dim=1,
        )

        return self.bias + linear_part + interaction
```

### 14.6 訓練要求

- [ ] 使用 sample uniqueness weight。
- [ ] 使用 time-series validation，不得 random split。
- [ ] 使用 early stopping。
- [ ] 使用 weight decay。
- [ ] 使用 gradient clipping。
- [ ] `n_eff < 25` 時不得訓練 FM。
- [ ] embedding dimension 根據 `n_eff` 自動調整。

### 14.7 embedding dimension 規則

```python
def choose_fm_embedding_dim(n_eff: float, n_features: int = 12) -> int:
    if n_eff < 30:
        return 2
    if n_eff < 60:
        return 3
    if n_eff < 120:
        return 4
    return min(6, n_features // 2)
```

### 14.8 驗收條件

- [ ] FM 不得在 `n_eff < 25` 時訓練。
- [ ] FM 不得直接覆蓋 LR。
- [ ] FM 必須作為 challenger 經過 Purged Walk-Forward。
- [ ] FM 的 adoption 必須通過原本五大 Gates。
- [ ] 輸出 interaction diagnostics，供策略審計。

---

## 15. 模組十：Adaptive Calibration Orchestrator

### 15.1 開發目的

統一調度所有自適應模組，將每次 learning tick 的結果轉成 config patch。

### 15.2 輸入

```python
ledger
bar_index
current_strategy_config
gate_results
feature_columns
```

### 15.3 輸出

```python
{
  "state": {...},
  "risk": {...},
  "strategy": {...},
  "model": {...},
  "diagnostics": {...}
}
```

### 15.4 開發任務

- [ ] 讀取 ledger。
- [ ] 檢查資料完整性。
- [ ] 計算 sample uniqueness。
- [ ] 計算 sample weight 與 `n_eff`。
- [ ] 執行 LR baseline 訓練。
- [ ] 視情況執行 FM challenger 訓練。
- [ ] 執行 Purged Walk-Forward。
- [ ] 執行五大 Gates。
- [ ] 計算 Gate severity。
- [ ] 計算 validation entropy sizing。
- [ ] 執行 DSR inverse thresholding。
- [ ] 產生 config patch。
- [ ] 寫入 audit log。
- [ ] 僅在 adoption pass 時寫入正式權重。

### 15.5 orchestrator 流程

```text
1. load ledger
2. validate ledger schema
3. compute uniqueness
4. build sample weights
5. compute n_eff
6. train LR baseline
7. train FM challenger if n_eff >= threshold
8. run purged walk-forward
9. run PBO / DSR / Edge Decay / Calibration Gates
10. convert Gates to severity
11. compute validation entropy sizing
12. update confluence threshold using DSR inverse controller
13. determine state: DRY_RUN / VALIDATING_PROBE / READY / LOCKED
14. emit config patch
15. write audit log
```

### 15.6 config patch 規格

```json
{
  "state": {
    "mode": "VALIDATING_PROBE",
    "adopt_weights": false,
    "n_eff": 34.7,
    "validation_entropy": 0.62,
    "validation_amplitude": 0.41
  },
  "risk": {
    "risk_multiplier": 0.035,
    "probe_notional_cap_usdt": 5.0
  },
  "strategy": {
    "confluence_min_score": 0.73
  },
  "model": {
    "active_model": "uniqueness_lr",
    "challenger_model": "fm",
    "adopt_challenger": false
  },
  "diagnostics": {
    "required_sr_for_dsr": 1.48,
    "current_sr": 0.82,
    "mp_lambda_plus": 2.15,
    "fm_trained": true
  }
}
```

### 15.7 驗收條件

- [ ] orchestrator 不直接修改正式 config，只產生 patch。
- [ ] 正式 config 寫入必須經過 atomic write。
- [ ] 每次 calibration tick 都要有 audit log。
- [ ] patch 需可回放、可比較、可回滾。

---

## 16. 設定檔規格

### 16.1 `configs/adaptive.yaml`

```yaml
adaptive:
  enabled: true

  sample_weighting:
    enabled: true
    use_uniqueness: true
    use_recency: true
    use_class_balance: true
    half_life_trades: 100

  validation:
    n_eff_probe_min: 20
    n_eff_ready_min: 60
    n_splits: 4
    embargo_bars: 5

  probe:
    enabled: true
    max_probe_multiplier: 0.10
    probe_notional_cap_usdt: 5.0
    min_probe_notional_usdt: 1.0
    max_probe_orders_per_day: 5
    max_probe_daily_loss_usdt: 10.0
    max_consecutive_probe_losses: 3

  dsr_thresholding:
    enabled: true
    target_prob: 0.95
    base_threshold: 0.60
    max_threshold: 0.90
    k: 0.08
    smoothing: 0.20

  mp_denoising:
    enabled: true
    min_samples: 30

  models:
    baseline: uniqueness_lr
    challenger: factorization_machine

    lr:
      C: 0.25
      max_weight_delta: 0.15

    fm:
      enabled: true
      min_n_eff: 25
      embedding_dim_min: 2
      embedding_dim_max: 6
      lr: 0.001
      weight_decay: 0.001
      max_epochs: 300
      patience: 30
      gradient_clip_norm: 2.0

risk_kill_switch:
  disable_on_data_gap: true
  disable_on_execution_error: true
  disable_on_slippage_outlier: true
  max_probe_orders_per_day: 5
  max_probe_daily_loss_usdt: 10.0
  max_consecutive_probe_losses: 3
```

---

## 17. 開發階段規劃

## Phase 0：Schema 與安全邊界

目標：先確保資料與風控可控。

### 任務

- [ ] 定義 ledger schema。
- [ ] 新增 `probe` 欄位。
- [ ] 新增 `model_version` 與 `config_hash`。
- [ ] 新增 audit log。
- [ ] 新增 atomic config patch 機制。
- [ ] 新增 kill switch。

### 驗收

- [ ] 任一 trade 可回溯到產生它的 config 與 model。
- [ ] 任一 config patch 可回滾。
- [ ] 下單異常時系統能切到 `LOCKED`。

---

## Phase 1：解除 VALIDATING 死鎖

目標：讓系統在不採用新權重的前提下，允許極小倉位前向探索。

### 任務

- [ ] 開發 `SampleUniquenessEngine`。
- [ ] 開發 `PurgedWalkForwardSplitter`。
- [ ] 開發 `GateSeverityEvaluator`。
- [ ] 開發 `ValidationEntropySizer`。
- [ ] 開發 `ProbeExecutionController`。
- [ ] 改造狀態機，新增 `VALIDATING_PROBE`。

### 驗收

- [ ] Gates 未全過時，不會自動進入 READY。
- [ ] Gates 未全過但符合條件時，可進入 `VALIDATING_PROBE`。
- [ ] `VALIDATING_PROBE` 單筆 notional 不超過 5 USDT。
- [ ] `VALIDATING_PROBE` 單必須標記 `probe=true`。
- [ ] `n_eff` 不足時仍維持 `DRY_RUN`。

---

## Phase 2：自動門檻控制

目標：讓 DSR 失敗不只是阻斷，而是轉化為下單品質門檻。

### 任務

- [ ] 開發 `DSRInverseThresholdController`。
- [ ] 將 current Sharpe、required Sharpe、DSR probability 寫入 diagnostics。
- [ ] 將 threshold patch 寫入 config patch。
- [ ] 新增 threshold smoothing。
- [ ] 新增 threshold audit log。

### 驗收

- [ ] DSR gap 擴大時，confluence threshold 上升。
- [ ] DSR gap 收斂時，threshold 可緩慢回落。
- [ ] threshold 不低於 base threshold。
- [ ] threshold 不高於 max threshold。

---

## Phase 3：模型穩定化

目標：降低小樣本下 LR 權重不穩定。

### 任務

- [ ] 開發 `MarchenkoPasturDenoiser`。
- [ ] 開發 `UniquenessWeightedLR`。
- [ ] 將 MP diagnostics 寫入 calibration report。
- [ ] 將 LR 權重變化加入 clipping。
- [ ] 將 proposed weights 與 current weights 做 diff。

### 驗收

- [ ] LR 訓練使用 sample weight。
- [ ] LR 權重變化不超過設定上限。
- [ ] MP denoising 不產生 NaN / Inf。
- [ ] 模型 proposal 不直接覆蓋正式權重。

---

## Phase 4：FM Challenger

目標：捕捉 SMC 二階因子交互，但避免小樣本過擬合。

### 任務

- [ ] 開發 `FactorizationMachineClassifier`。
- [ ] 建立 FM training pipeline。
- [ ] 使用 uniqueness sample weight。
- [ ] 使用 time-series validation。
- [ ] 輸出 interaction matrix diagnostics。
- [ ] 將 FM 作為 challenger，不直接取代 LR。

### 驗收

- [ ] `n_eff < 25` 時 FM 不訓練。
- [ ] FM 必須通過 Purged Walk-Forward。
- [ ] FM 必須通過五大 Gates 才能成為 active model。
- [ ] FM interaction diagnostics 可供人工審計，但 adoption 不需人工介入。

---

## 18. Learning Tick 最終流程

```text
on_learning_tick():

  1. load strategy.yaml
  2. load adaptive.yaml
  3. load trade ledger
  4. validate ledger schema
  5. compute sample uniqueness
  6. compute sample weights
  7. compute n_eff
  8. train LR baseline
  9. optionally train FM challenger
  10. run purged walk-forward
  11. run PBO
  12. run DSR
  13. run Edge Decay
  14. run Closed-Loop Calibration
  15. convert Gates into severity
  16. calculate validation entropy sizing
  17. calculate DSR inverse threshold
  18. produce config patch
  19. write audit log
  20. if 5 Gates pass and n_eff ready:
        adopt new weights
      else:
        keep current weights
  21. if state_hint == VALIDATING_PROBE:
        allow probe orders only
      elif state_hint == READY:
        allow normal simulated orders
      elif state_hint == DRY_RUN:
        no real orders
      elif state_hint == LOCKED:
        disable trading and learning updates
```

---

## 19. 下單流程最終邏輯

```text
on_trade_signal(signal):

  1. compute SMC confluence score
  2. check confluence_min_score
  3. generate Entry / Stop / Target proposal
  4. check system state

  if state == READY:
      use normal risk sizing
      order_mode = NORMAL

  elif state == VALIDATING_PROBE:
      use entropy-adjusted risk_multiplier
      cap notional by probe_notional_cap_usdt
      order_mode = PROBE

  elif state == DRY_RUN:
      do not send order
      order_mode = DRY_RUN

  elif state == LOCKED:
      reject order
      order_mode = BLOCKED

  5. write decision log
  6. if order sent, write execution log
  7. when trade closes, write ledger row
```

---

## 20. Audit Log 必須記錄的事件

### 20.1 Calibration audit

```json
{
  "timestamp": "2026-01-01T00:00:00Z",
  "event_type": "CALIBRATION_TICK",
  "n_trades": 48,
  "n_eff": 31.4,
  "state_before": "VALIDATING",
  "state_after": "VALIDATING_PROBE",
  "gates": {
    "walk_forward": {"pass": false, "severity": 0.32},
    "pbo": {"pass": false, "severity": 0.28},
    "dsr": {"pass": false, "severity": 0.41},
    "edge_decay": {"pass": true, "severity": 0.0},
    "calibration": {"pass": true, "severity": 0.0}
  },
  "validation_entropy": 0.68,
  "risk_multiplier": 0.027,
  "confluence_threshold_before": 0.68,
  "confluence_threshold_after": 0.72,
  "adopt_weights": false
}
```

### 20.2 Trade decision audit

```json
{
  "timestamp": "2026-01-01T00:05:00Z",
  "event_type": "TRADE_DECISION",
  "symbol": "BTCUSDT",
  "side": "long",
  "confluence_score": 0.76,
  "confluence_threshold": 0.72,
  "state": "VALIDATING_PROBE",
  "order_mode": "PROBE",
  "risk_multiplier": 0.027,
  "notional_usdt": 5.0,
  "allow_order": true
}
```

---

## 21. 測試計畫

### 21.1 Unit Tests

| 測試檔 | 驗證內容 |
|---|---|
| `test_sample_uniqueness.py` | concurrency、uniqueness、n_eff |
| `test_purged_walk_forward.py` | train/test 不重疊、embargo 生效 |
| `test_gate_severity.py` | severity 範圍與 fatal 邏輯 |
| `test_validation_entropy.py` | state_hint 與 risk_multiplier |
| `test_probe_controller.py` | probe notional cap、kill switch |
| `test_dsr_inverse.py` | required Sharpe 與 threshold 更新 |
| `test_mp_denoising.py` | corr matrix 數值穩定 |
| `test_weighted_lr.py` | sample_weight 是否進入 fit |
| `test_fm_classifier.py` | forward pass、weighted loss、early stopping |
| `test_orchestrator.py` | calibration tick 端到端 patch |

### 21.2 Integration Tests

- [ ] 30 筆高度重疊交易，確認 `n_eff < N`。
- [ ] Gates 失敗但 severity 不高，確認進入 `VALIDATING_PROBE`。
- [ ] Gates 失敗且 severity 高，確認維持 `DRY_RUN`。
- [ ] DSR gap 擴大，確認 confluence threshold 上升。
- [ ] Probe daily loss 達上限，確認停止 probe order。
- [ ] 5 Gates 全過且 `n_eff >= n_eff_ready_min`，確認進入 `READY`。

### 21.3 Backtest Replay Tests

用歷史資料 replay learning tick：

```text
for each learning_tick in historical_period:
    run adaptive calibration
    store state transition
    store threshold transition
    store risk multiplier
    store model proposal
    simulate order decision
```

驗收重點：

- [ ] 系統不應長期卡死在 `VALIDATING`。
- [ ] 探索倉位應隨 gate severity 動態變化。
- [ ] threshold 應隨 DSR gap 自動調整。
- [ ] 權重 adoption 次數應少於 proposal 次數。
- [ ] FM 不應在低 `n_eff` 時頻繁取代 LR。

---

## 22. 最小可行版本 MVP

若要快速落地，第一版只做以下項目：

```text
P0 MVP:
  1. ledger schema 補齊 probe / config_hash / model_version
  2. sample uniqueness
  3. effective sample size
  4. purged walk-forward
  5. gate severity
  6. validation entropy sizing
  7. VALIDATING_PROBE 狀態
  8. probe notional cap = 5 USDT
  9. audit log
```

MVP 不需要先做：

```text
暫緩：
  - FM
  - MP denoising
  - 複雜 PBO 改造
  - 多模型 ensemble
  - 自動超參數搜尋
```

原因：目前最急迫問題是狀態機死鎖，不是模型精度不足。

---

## 23. 建議開發順序

```text
第 1 步：Ledger schema + audit log
第 2 步：Sample uniqueness + n_eff
第 3 步：Purged walk-forward
第 4 步：Gate severity evaluator
第 5 步：Validation entropy sizing
第 6 步：VALIDATING_PROBE 狀態與 probe sizing
第 7 步：DSR inverse threshold
第 8 步：MP denoising + weighted LR
第 9 步：FM challenger
第 10 步：完整 replay 測試與參數校準
```

---

## 24. 最終判斷規則

### 24.1 權重採用規則

```python
adopt_weights = (
    walk_forward_pass
    and pbo_pass
    and dsr_pass
    and edge_decay_pass
    and closed_loop_calibration_pass
    and n_eff >= n_eff_ready_min
)
```

### 24.2 探索交易規則

```python
allow_probe = (
    state == "VALIDATING_PROBE"
    and risk_multiplier > 0
    and not kill_switch_triggered
    and confluence_score >= confluence_min_score
)
```

### 24.3 正常交易規則

```python
allow_normal_order = (
    state == "READY"
    and adopt_weights is True
    and not kill_switch_triggered
    and confluence_score >= confluence_min_score
)
```

---

## 25. 結論

本次開發的重點不是降低驗證標準，而是把驗證結果拆成兩種用途：

```text
1. 是否採用新權重：仍然嚴格要求五大 Gates 全過。
2. 是否允許極小倉位探索：根據 gate severity、validation entropy、n_eff 動態控制。
```

這樣系統可以維持防禦性，又不會在冷啟動期因樣本不足而永久停留在 `VALIDATING`。

最優先應完成的是：

```text
Sample Uniqueness
Purged Walk-Forward
Gate Severity
Validation Entropy Sizing
VALIDATING_PROBE
Probe Risk Controller
Audit Log
```

FM 與 Marchenko-Pastur 可以放在第二階段或第三階段，避免在資料問題尚未解決前引入更複雜的模型風險。
