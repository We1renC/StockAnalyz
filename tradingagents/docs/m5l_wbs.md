# SMC Quant Trading System: M5L Module & WBS Grounding (§18.8)

This document details the architecture, file layout, and Work Breakdown Structure (WBS) for the closed-loop win/loss learning extraction module (M5L), as specified in Appendix D (§18).

---

## 1. M5L Module Codebase Integration

The learning module sits in the `tradingagents/web/learning/` directory or is packaged directly within the trading agents module. The main entry points reside in [smc_quant.py](file:///Users/w.rc/stockAnalyz/tradingagents/web/smc_quant.py):

- **Trade Store Manager:** Standardizes trade schemas, saving and reading performance outcomes to `trades.parquet` (or SQLite).
- **Attribution Engine:** Performs expected value lift calculations per confluence factor, MAE/MFE stop-loss recommendations, and R-multiple histograms.
- **Machine Learning Model:** Custom Logistic Regression gradient descent solver for estimating factor predictive coefficients and calculating exact instance-level SHAP attributions.
- **Out-of-Sample Validator:** Time-series validation via purged k-fold split walks to prevent data leakage and selection bias (PBO & Deflated Sharpe).

---

## 2. Work Breakdown Structure (WBS) Checklist

### WBS L.1: Trade Database Schema (`trade_store`)
- [x] Create standardized trade dictionaries matching the features/labels specification:
  - **Features ($X$):** Entry models, confluence score, individual indicators (killzone, sweep, bias, CVD flow).
  - **Labels ($Y$):** Exit trigger (TP/SL), net return, R-multiple, MAE, MFE.
- [x] Local storage capability to save backtest/live records in parquet format.

### WBS L.2: Edge Attribution Analysis (`attribution`)
- [x] Factor expectancy lift: compute win rate/average R with vs. without each factor.
- [x] Winner MAE distribution: recommend widening stop-losses if noise triggers early exits.
- [x] Winner MFE distribution: analyze profit claw-backs.
- [x] Expectancy histograms: plot R-multiple count distributions.

### WBS L.3 & L.4: Calibrator & OOS Validator (`calibration`)
- [x] Calculate Kelly Criterion fractional sizing based on real win rate and win/loss odds.
- [x] Time-series out-of-sample k-fold walkthrough validation to verify weight updates.
- [x] Implement Deflated Sharpe Ratio (DSR) to filter out parameter optimization overfitting.
- [x] Control Type I errors under multiple factor scans using Bonferroni corrections.
- [x] Output strategy proposals comparing old configuration weights against new weights.
