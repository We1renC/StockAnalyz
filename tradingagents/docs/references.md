# SMC Quant Trading System: Bibliography & References (§14)

This document compiles the academic literature, community resources, and open-source packages referenced during the design and implementation of the SMC Quant Trading System.

---

## 1. Smart Money Concepts (SMC) & ICT Core Theory
- **Inner Circle Trader (ICT) Core Tutorials (2022/2023):** Primary source for Judas Swing, Silver Bullet, Power of Three (AMD), Breaker Blocks, and Optimal Trade Entry (OTE) definitions.
- **SMC Strategy Overview & Guidelines:**
  - *Smart Money Concepts Trading Strategy Guide (2025)* - SignalWavesAI.
  - *Understanding Institutional Footprints in FX and Crypto* - NordFX Research.
- **SMC Open-Source Package Comparisons:**
  - `joshyattridge/smart-money-concepts`: Default reference library for textbook swing pivot validation rules (used in cross-validation).
  - `starckyang/smc_quant`: Structural layout for quantitative SMC patterns in Python.

---

## 2. Statistical Finance & Overfitting Prevention
- **Advances in Active Portfolio Management (López de Prado, 2018):**
  - Section on **Deflated Sharpe Ratio (DSR)** for correcting selection bias under multiple comparisons testing.
  - **Purged Cross-Validation** and Combinatorial Purged CV methods for time-series backtest validation.
- **Multiple Comparisons Corrections:**
  - *Bonferroni, Carlo. "Teoria statistica delle classi e calcolo delle probabilità."* (1936). Application to control the Family-Wise Error Rate (FWER) when checking dozens of potential trading factors.

---

## 3. Cryptocurrency Derivatives & Order Flow Analysis
- **CoinGlass & Coinalyze API Documentation:**
  - Leverage profiles, Open Interest (OI) build-up patterns, and perpetual funding rate anomalies.
  - Cumulative Volume Delta (CVD) divergence mechanics for validating price breakout exhaustion.
- **Coinbase Premium Gap Dynamics:**
  - *The Coinbase Premium Index as an Institutional Flow Indicator* - CryptoQuant Research.
- **C CME Bitcoin Gap Magnetism:**
  - Analysis of weekend futures closure gaps and their subsequent fill rates (historically ~77%). Note the CME transition to 24/7 trading in 2026.

---

## 4. Academic Machine Learning & Feature Attribution
- **SHAP (SHapley Additive exPlanations):**
  - Lundberg, Scott M., and Su-In Lee. *"A unified approach to interpreting model predictions."* (NeurIPS 2017).
  - Formulation of Shapley values for linear/logistic regression models: $\phi_i = \theta_i (x_i - \mu_i)$.
- **Factor Attribution & Kelly Sizing:**
  - *Thorp, Edward O. "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market."* (1997). Application to fractional Kelly sizing for algorithmic risk management.
