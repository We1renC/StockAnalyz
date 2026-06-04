# Smart Money Concept (SMC) Quantitative Trading System Implementation Plan

**Version:** 1.0  
**Date:** 2026-06-03  
**Applicable Markets:** Taiwan Stocks, US Stocks, Cryptocurrencies (Multi-asset universal, including market-specific adjustments)  
**Positioning:** Transform SMC/ICT (Inner Circle Trader) institutional trading concepts from "subjective drawing" to "quantifiable, backtestable, and automated" algorithmic strategies, and integrate them into the existing multi-role AI architecture of the "Taiwan Stock Trading Team".

---

## Table of Contents
1. [Document Purpose and Scope](#1-document-purpose-and-scope)
2. [SMC Core Concepts and Terminology Definitions](#2-smc-core-concepts-and-terminology-definitions)
3. [SMC Concepts List and Algorithm Detection Specifications](#3-smc-concepts-list-and-algorithm-detection-specifications-mandatory-core)
4. [Multi-Timeframe Top-Down Analysis Process](#4-multi-timeframe-top-down-analysis-process)
5. [Entry Models and Confluence Combinations](#5-entry-models-and-confluence-combinations)
6. [Risk Control and Position Management](#6-risk-control-and-position-management)
7. [System Architecture and Module Decomposition](#7-system-architecture-and-module-decomposition)
8. [Mandatory Items and Sub-items (WBS - Work Breakdown Structure)](#8-mandatory-items-and-sub-items-wbs---work-breakdown-structure)
9. [Data Requirements and Multi-Market Differentiation Adjustments](#9-data-requirements-and-multi-market-differentiation-adjustments)
10. [Backtesting and Verification Methodology](#10-backtesting-and-verification-methodology)
11. [Integration with the Existing AI Trading Team](#11-integration-with-the-existing-ai-trading-team)
12. [Risks, Limitations, and Known Pitfalls](#12-risks-limitations-and-known-pitfalls)
13. [Development Milestones](#13-development-milestones)
14. [References](#14-references)
15. [Appendix A: Chart Analysis (Visualization) Implementation List](#15-appendix-a-chart-analysis-visualization-implementation-list)
16. [Appendix B: Reference and Decision Matrix for Domestic and International Virtual/Demo Trading Practices](#16-appendix-b-reference-and-decision-matrix-for-domestic-and-international-virtualdemo-trading-practices)
17. [Appendix C: Cryptocurrency-Exclusive SMC Enhancements (Primary Application Scope)](#17-appendix-c-cryptocurrency-exclusive-smc-enhancements-primary-application-scope)
18. [Appendix D: Trade Backtesting and Win/Loss Learning Extraction System (Closed-Loop Learning)](#18-appendix-d-trade-backtesting-and-winloss-learning-extraction-system-closed-loop-learning)

---

## 1. Document Purpose and Scope

### 1.1 Purpose
*   **Primary Application Scope:** Cryptocurrencies (reasons detailed in §17.1: 24/7 continuous trading, purest imbalances, derivative data making liquidity "visible", large backtesting sample size). Taiwan Stocks and US Stocks are secondary/extensible markets, sharing the same core engine. Crypto-specific enhancements are detailed in Appendix C (§17).
*   Complete audit of all SMC concepts that must be implemented, providing programmable detection rules, inputs, outputs, and parameters for each.
*   Provide a modular system architecture so that the strategy can share the same core engine across Cryptocurrencies (primary), US Stocks, and Taiwan Stocks, with differences only in the data layer and market parameters.
*   Use a WBS (Work Breakdown Structure) to list mandatory items/sub-items and Definition of Done (DoD) to serve as a schedule baseline for development.
*   Establish a backtesting and verification framework to avoid the common SMC pitfalls of "cherry-picking" and "repainting bias".

### 1.2 Scope Boundaries

| In Scope | Out of Scope (Current Phase) |
| :--- | :--- |
| Structure Analysis (BOS/CHoCH), OB, FVG, Liquidity, Premium/Discount, OTE, Session/Killzone | Order Flow tick-by-tick micro-analysis |
| Multi-Timeframe Top-Down signal confluence | Options Greeks, Market Maker pricing models |
| Event-driven backtesting, risk control, position management | High-Frequency Trading (HFT), millisecond-latency arbitrage |
| Crypto (primary) / US Stocks / Taiwan Stocks data ingestion | Automated order execution (focus on signaling and backtesting first; live execution postponed to later stages) |
| Crypto Derivatives Data: Liquidation Heatmap, Open Interest (OI), Funding Rates, CVD, Cross-exchange Premium (§17) | — |

### 1.3 Relationship between SMC and Existing Technical Strategies
The existing `tech_executor_strategy.skill` uses moving averages (5/20/60MA), MACD, and price-volume. SMC does not replace but complements these strategies by improving entry and exit precision:
*   Moving averages/MACD determine trend direction and momentum (trend filter).
*   SMC provides precise entry zones (OB/FVG/OTE) and stop-loss logic (structural invalidation points), improving the Risk-to-Reward (RR) ratio.
*   Confluence of the two = "trend-following + precise institutional footprint entry", aligning with the team's core rule of "win rate > 60%, risk-reward ratio > 1:1.5".

---

## 2. SMC Core Concepts and Terminology Definitions

SMC Assumption: Prices are driven by large orders from institutions (banks, funds, market makers); institutions require liquidity (retail orders) to fill large positions, meaning price is "designed" to sweep retail stop-losses and fill imbalance zones before moving in the true direction. SMC's job is to identify these institutional footprints and trade alongside them.

| Terminology | English / Abbreviation | One-sentence Definition |
| :--- | :--- | :--- |
| **Market Structure** | Market Structure | A sequence of highs and lows formed by consecutive swing highs/lows |
| **Break of Structure** | BOS | Breakout in the direction of the trend above the previous swing high (bullish) or below the previous swing low (bearish) $\to$ Trend continuation |
| **Change of Character** | CHoCH | Breakout counter to the trend below the previous swing low (bullish to bearish transition) or above the previous swing high (bearish to bullish transition) $\to$ Trend reversal |
| **Order Block** | OB | A supply/demand zone located at the "last opposite candle" before a strong trend move starts |
| **Fair Value Gap / Imbalance** | FVG / Imbalance | A price gap formed in a 3-candle sequence where the high of the 1st candle and the low of the 3rd candle (or vice versa) do not overlap |
| **Liquidity** | Liquidity (BSL / SSL) | Zones where stop-loss orders accumulate: above equal highs (BSL) or below equal lows (SSL) |
| **Liquidity Sweep / Grab** | Liquidity Sweep / Grab | Price pierces a liquidity zone and quickly rejects (wicks back), reversing after sweeping the stops |
| **Premium / Discount** | Premium / Discount | Using the 50% level of the dealing range as equilibrium: the upper half is Premium (sell zone) and the lower half is Discount (buy zone) |
| **Optimal Trade Entry** | OTE | The Fibonacci 0.62–0.79 retracement zone, with 0.705 being the sweet spot |
| **Breaker Block** | Breaker Block | A failed OB that is broken through and then flipped into an opposite supply/demand zone |
| **Mitigation Block** | Mitigation Block | Price returning to an OB zone to "mitigate" unfilled orders |
| **Trading Sessions** | Session / Killzone | Specific high-activity trading hours (Asia/London/New York; crypto is 24/7 but still has killzones) |

---

## 3. SMC Concepts List and Algorithm Detection Specifications (Mandatory Core)

Every concept below is a mandatory implementation item. The schema for all columns is standardized as: **Purpose / Input / Parameters / Detection Rules (Pseudocode) / Output Fields / Implementation Notes.**

You can refer to the industry standard open-source implementation `joshyattridge/smart-money-concepts` (which processes pandas OHLC DataFrames) to align function interfaces for ease of validation.

### 3.1 Swing Highs / Lows — The Foundation of All Structure
*   **Purpose:** Pre-requisite dependency for all structural analysis (BOS / CHoCH / OB / Liquidity / Retracements).
*   **Input:** OHLC DataFrame.
*   **Parameters:** `swing_length` (number of candles to compare before and after; default is `5`; daily charts can use `3-5`, crypto 1H can use `10-50`).
*   **Detection Rules:**
    *   `swing_high(i)`: `high[i] == max(high[i-n : i+n+1])`
    *   `swing_low(i)`: `low[i] == min(low[i-n : i+n+1])`
*   **Output:** `HighLow` (`1` = High, `-1` = Low), `Level` (price).
*   **Implementation Notes (Critical):** Since we need to confirm `i+n` future candles, the confirmation point lags by `n` candles. In backtesting, you MUST record the "confirmation index" to avoid lookahead bias. See §12.2 for details.

### 3.2 Market Structure: BOS and CHoCH
*   **Purpose:** Determine trend continuation (BOS) or reversal (CHoCH) to establish market bias (bullish/bearish).
*   **Input:** OHLC + swing highs/lows output.
*   **Parameters:** `close_break` (Boolean: `True` = confirmed by candle close, stricter; `False` = confirmed by high/low wick piercing).
*   **Detection Rules:**
    *   *In a Uptrend:* Price close breaks above previous swing high $\to$ `BOS(+1)` (continuation)
    *   *In a Uptrend:* Price close breaks below previous swing low $\to$ `CHoCH(-1)` (reversal to bearish)
    *   *In a Downtrend:* Price close breaks below previous swing low $\to$ `BOS(-1)` (continuation)
    *   *In a Downtrend:* Price close breaks above previous swing high $\to$ `CHoCH(+1)` (reversal to bullish)
*   **Output:** `BOS(±1)`, `CHOCH(±1)`, `Level` (broken structural price level), `BrokenIndex` (the index of the candle performing the break).
*   **Implementation Notes:** It is recommended to distinguish between **Internal Structure** (minor structure) and **Swing Structure** (major structure using larger `swing_length`), which is the core of multi-timeframe analysis.

### 3.3 Order Block (OB) + Breaker / Mitigation
*   **Purpose:** Institutional supply/demand zones, used as backtest entry areas.
*   **Input:** OHLC + swing highs/lows.
*   **Parameters:** `close_mitigation` (Boolean: whether mitigation is triggered by close price or high/low price).
*   **Detection Rules (Bullish OB):**
    *   *Bullish OB* = The body `[open, close]` or full range `[high, low]` of the last bearish candle before a strong upward move (which causes a BOS).
    *   *Bearish OB* = The last bullish candle before a strong downward move.
    *   *Validity:* The OB must be accompanied by strong displacement / cause a BOS after its formation.
*   **Output:** `OB(±1)`, `Top`, `Bottom`, `OBVolume` (cumulative volume in the zone), `Percentage` (strength ratio), `MitigatedIndex`.
*   **3-Filter Validity Check** (Adapted from Chinese community practice "Full-Time Dad", see Appendix B):
    1.  Swept liquidity immediately prior to formation (sweeping previous high/low).
    2.  Accompanied by displacement / caused a BOS.
    3.  Unmitigated.
    *   An OB meeting all three criteria = **Grade-A OB**, prioritized for entry.
*   **Extensions (Mandatory):**
    *   **Breaker Block:** A failed OB that is broken validly in the opposite direction, flipping it into an opposite OB (ref. Appendix A, Diagram ②).
    *   **Mitigation Block:** Mark OBs as `mitigated` or `unmitigated` when price returns to the OB zone. Unmitigated OBs have the highest priority.
    *   **Refined Entry / Consequent Encroachment** (ref. Appendix A, Diagram ⑦): Instead of placing limit orders at the edge of the OB zone, use the 50% mid-line (mean threshold) of the OB as a more precise entry price to reduce stop-loss size and maximize RR.
        ```python
        ob_mid = (OB.Top + OB.Bottom) / 2     # 50% mid-line
        refined_entry = ob_mid               # Entry triggered only at the mid-line
        ```

### 3.4 Fair Value Gap (FVG / Imbalance) + Inverse FVG
*   **Purpose:** Price imbalance zones acting as magnet targets and potential entry areas.
*   **Input:** OHLC.
*   **Parameters:** `join_consecutive` (Boolean: whether to merge consecutive FVGs).
*   **Detection Rules:**
    *   *Bullish FVG (with candle `i` as the middle candle):* `low[i+1] > high[i-1]` $\to$ Gap range = `[high[i-1], low[i+1]]`
    *   *Bearish FVG:* `high[i+1] < low[i-1]` $\to$ Gap range = `[high[i+1], low[i-1]]`
*   **Output:** `FVG(±1)`, `Top`, `Bottom`, `MitigatedIndex` (the index of the candle that fills the gap).
*   **Extensions (Mandatory):**
    *   **Inverse FVG (IFVG):** When price completely pierces and closes past an FVG, it flips to become opposite support/resistance.
    *   **Displacement Confirmation:** The middle candle forming the FVG must be a large-bodied candle (displacement) to filter out noise gaps.

### 3.5 Liquidity (Liquidity: BSL / SSL, Equal Highs/Lows, Sweep)
*   **Purpose:** Locate stop-loss clusters (institutional targets) and predict sweep reversals.
*   **Input:** OHLC + swing highs/lows.
*   **Parameters:** `range_percent` (proximity threshold for clustering; default `0.01` = 1%; can be wider for crypto).
*   **Detection Rules:**
    *   *BSL (Buy-Side Liquidity):* Accumulates above multiple adjacent swing highs (equal highs).
    *   *SSL (Sell-Side Liquidity):* Accumulates below multiple adjacent swing lows (equal lows).
    *   *Equal Highs / Equal Lows:* `|level_a - level_b| / price <= range_percent`
    *   *Sweep:* Price pierces a level and closes back within the range during the same or subsequent candles $\to$ Mark as `Swept`.
*   **Output:** `Liquidity(±1)`, `Level`, `End`, `Swept` (index of the candle that sweeps the liquidity).
*   **Internal vs. External Liquidity** (ref. Appendix A, Diagram ⑤): External liquidity = main highs/lows of the dealing range (range high/low); internal liquidity = minor equal highs/lows and FVGs within the range. The standard market rhythm is "sweep internal liquidity $\to$ run towards external liquidity," combined with §3.6 Premium/Discount to determine the entry direction.
*   **Mandatory Additional Liquidity Targets:** Previous Day's High/Low (PDH/PDL), Previous Week's High/Low (PWH/PWL), round numbers, and pre-market/opening range highs and lows.
*   **Sweep vs. Run Discrimination Rule:** If the Higher Timeframe (HTF) bias is in the same direction as the swept liquidity $\to$ Expect a "Run" (continuation); if opposite $\to$ Expect a "Sweep" (fakeout reversal).
*   **DOL (Draw on Liquidity / Liquidity Magnet**; adapted from Chinese community practice, see Appendix B): Before entering any trade, you MUST specify the "target liquidity price is most likely to be attracted to" (opposite equal highs/lows, PDH/PDL, unmitigated FVG) as the profit-taking target. Do not enter trades without a clear DOL. Output: `DOL_target`.

### 3.6 Premium / Discount Zones & Equilibrium
*   **Purpose:** Only buy in the discount zone and sell in the premium zone to optimize the risk-reward ratio.
*   **Input:** A valid dealing range (the most recent matching swing high $\to$ swing low leg).
*   **Detection Rules:**
    *   `range = [swing_low, swing_high]`
    *   `equilibrium = 50%`
    *   `discount = Price < 50%` (preferred buy zone)
    *   `premium = Price > 50%` (preferred sell zone)
*   **Output:** `zone` (premium/discount/equilibrium), price levels of various Fib retracements.

### 3.7 Optimal Trade Entry (OTE, Fibonacci)
*   **Purpose:** Find the most precise entry price with the best risk-reward ratio during a retracement.
*   **Detection Rules:**
    *   Plot Fibonacci retracement on the impulse leg (0 = termination point, 1 = starting point).
    *   `OTE Zone` = `0.62` to `0.79`, with `0.705` being the optimal entry.
    *   *Extension targets:* Use `-0.27` / `-0.62` Fib extensions as profit-taking references.
*   **Output:** `OTE_zone` (top/bottom), `entry_0705`, `stop_ref` (outside the start of the impulse leg), `tp1/tp2`.
*   **Confluence Rule (Mandatory):** The OTE zone must overlap with an HTF OB, FVG, or major liquidity pool to be considered high-probability; otherwise, it is only a reference.

### 3.8 Previous High / Low (Multi-Timeframe)
*   **Purpose:** Multi-timeframe liquidity reference levels and targets (PDH/PDL, PWH/PWL, PMH/PML).
*   **Input:** OHLC + `time_frame` (`15m`/`1H`/`4H`/`1D`/`1W`/`1M`).
*   **Output:** `PreviousHigh`, `PreviousLow`, `BrokenHigh(0/1)`, `BrokenLow(0/1)`.

### 3.9 Trading Sessions / Killzones (Sessions)
*   **Purpose:** Filter out low-quality signals, entering trades only during high-liquidity periods (especially critical for FX/Crypto).
*   **Input:** OHLC (with timezone awareness) + session definitions + timezone.
*   **Built-in Sessions:** Sydney / Tokyo (Asia) / London / New York. **Killzones:** London Open, NY Open, NY Close/Afternoon.
*   **Output:** `Active(0/1)`, `SessionHigh`, `SessionLow`.
*   **Market Differences:**
    *   *Taiwan Stocks:* Single continuous session 09:00–13:30 (morning session 09:00–10:00 is the main killzone), no overnight session concept.
    *   *US Stocks:* 09:30–16:00 ET, opening range (first 30 mins) is the most critical.
    *   *Crypto:* 24/7, adapts London/NY killzones (highest liquidity), weekends weighted lower.

### 3.10 Retracements
*   **Purpose:** Quantify the current/deepest retracement percentage to determine if price has entered OTE or if the structure remains healthy.
*   **Input:** OHLC + swing highs/lows.
*   **Output:** `Direction(±1)`, `CurrentRetracement%`, `DeepestRetracement%`.

### 3.11 Displacement & Order Flow Confirmation
*   **Purpose:** Differentiate "institution-driven moves" from "retail noise".
*   **Detection Rules:** One or multiple candles with large bodies (body size > $N \times \text{ATR}$, or candle body accounting for > 70% of total range), often accompanied by an FVG. OBs and FVGs are only valid when accompanied by displacement.

### 3.12 Judas Swing (Fakeout Reversal, ref. Appendix A, Diagram ⑥)
*   **Purpose:** Identify "initial session fakeouts" — institutions push price in the opposite direction first to trap retail traders and sweep stops, then reverse to the true direction ("Initial false move $\to$ REAL MOVE"). This is the session-level specific form of a liquidity sweep.
*   **Input:** OHLC (tz-aware) + swing highs/lows + sessions (§3.9).
*   **Detection Rules:**
    *   During the early stage of a session/killzone (e.g., 1–2 hours before NY Open, first 30 mins of Taiwan Stock Open):
        1.  Price pierces a previous reference high/low (PDH/PDL or pre-market range) to form a fakeout.
        2.  A displacement reversal + LTF CHoCH (§3.2) occurs $\to$ Confirming Judas (fakeout).
        3.  The real direction = opposite of the fakeout.
    *   *Discrimination:* Align with §3.5 "Sweep vs. Run" — if the HTF bias is opposite to the fakeout direction $\to$ Treat as Judas.
*   **Output:** `Judas(±1` marking the real direction), `FalseMoveHigh/Low`, `ConfirmIndex`.
*   **Implementation Notes:** Most typical for crypto (NY/London killzones) and US stocks (market open ORB); corresponds to the first 30 minutes of fakeouts in Taiwan stocks. This pattern feeds directly into §5.1 Entry Model 1 (Sweep + CHoCH).

### 3.13 SMT Divergence (Smart Money Technique Divergence; adapted from international ICT community)
*   **Purpose:** Validate liquidity sweeps using divergence between highly correlated assets — when two highly correlated instruments diverge at a key liquidity zone, it indicates that institutions are "rejecting" that price level, signaling a high-quality reversal.
*   **Input:** OHLC of two correlated assets (time-aligned).
*   **Correlated Pairs Example:** US Stocks: ES vs NQ (or SPY vs QQQ); Crypto: BTC vs ETH (or BTC vs TOTAL market cap); Taiwan Stocks: TSMC vs Weighted Index/0050, or Leading Stock vs Runner-Up Stock in the same sector.
*   **Detection Rules:**
    *   Asset A makes a new high, while Asset B fails to make a new high (at the same liquidity zone) $\to$ Bearish SMT (`-1`).
    *   Asset A makes a new low, while Asset B fails to make a new low $\to$ Bullish SMT (`+1`).
*   **Output:** `SMT(±1)`, `divergence_level`, `paired_symbol`.
*   **Implementation Notes:** Requires a "correlated asset data alignment" sub-module (handling time index alignment and missing values); serves as the core confluence item for the §5.3 Unicorn model.

---

## 4. Multi-Timeframe Top-Down Analysis Process

SMC's high win rate stems from multi-timeframe confluence. The standard 3-tier process is:

| Timeframe | Role | Taiwan/US Stock Example | Crypto Example | Output |
| :--- | :--- | :--- | :--- | :--- |
| **HTF (Higher Timeframe)** | Direction / Bias | Weekly, Daily | 1D, 4H | Bullish/bearish bias, swing structure, HTF OB/FVG, HTF liquidity targets |
| **MTF (Medium Timeframe)** | Zone / POI | 60m, Daily | 1H, 15m | POI (Point of Interest): OB/FVG entry zones, Premium/Discount |
| **LTF (Lower Timeframe)** | Trigger / Entry | 15m, 5m | 5m, 1m | CHoCH/BOS confirmation + precise entry at OTE/FVG + stop-loss |

**Process Pseudocode:**
1.  **HTF:** Mark major swing structure (BOS/CHoCH) $\to$ Determine bias (bullish/bearish/neutral).
2.  **HTF:** Mark unmitigated OBs, unfilled FVGs, and BSL/SSL liquidity targets.
3.  Wait for price to enter an HTF POI and align with the Discount zone (for longs) or Premium zone (for shorts).
4.  **MTF:** Confirm price shows a reaction in the POI (liquidity sweep + displacement).
5.  **LTF:** Wait for confluence of CHoCH (micro-structural shift) + LTF FVG/OB + OTE.
6.  **Entry:** Place limit orders at LTF FVG/OB/OTE, with stop-loss placed outside structural invalidation levels.
7.  **Target (DOL):** The next HTF liquidity pool (opposite equal highs/lows, PDH/PDL).

---

## 5. Entry Models and Confluence Combinations

### 5.1 Three Standard Entry Models (Mandatory)
*   **Liquidity Sweep Reversal Model (Sweep + CHoCH, including Judas Swing §3.12):** Sweep of SSL/BSL (or session fakeout) $\to$ Displacement $\to$ LTF CHoCH $\to$ Retest of OB/FVG (OB 50% refined) $\to$ Entry (highest probability reversal).
*   **OB/FVG Continuation Model (Continuation):** HTF BOS confirms the trend $\to$ Retest of unmitigated OB or unfilled FVG (situated in discount/premium) $\to$ Trend-following entry.
*   **OTE Retracement Model (Fibonacci OTE):** Impulse leg $\to$ Retracements to 0.62–0.79 (ideal 0.705) overlapping with OB/FVGs $\to$ Entry.

### 5.2 Confluence Scoring System (Quantifying Subjectivity; Mandatory)
To minimize subjectivity, each trade signal is quantified using a weighted confluence score. Execution is triggered only when a threshold is met:

| Confluence Factor | Weight (Example) |
| :--- | :--- |
| HTF bias alignment | +2 |
| Positioned on the correct side of Premium/Discount | +2 |
| Overlaps with an unmitigated OB | +2 |
| Overlaps with an unfilled FVG | +1 |
| Liquidity sweep just occurred | +2 |
| LTF CHoCH confirmed | +2 |
| Enters OTE 0.62–0.79 zone | +1 |
| Positioned within Killzone / active session | +1 |
| Volume/Displacement confirmed | +1 |
| **Entry Threshold** | **$\ge$ 8 points** |

*Weights must be calibrated via backtesting (§10) and are not fixed. Output the score and triggering factor details for each signal to facilitate auditing and optimization.*

### 5.3 Advanced High-Confluence Models (Adapted from international ICT community, see Appendix B)
The following are "named, enhanced variants" of the three basic models in §5.1, scored uniformly using the §5.2 confluence framework as optional configurations:

| Model | Component | Applicable Markets/Periods | Implementation Value |
| :--- | :--- | :--- | :--- |
| **Power of Three (AMD)** | 3 intraday phases: Accumulation $\to$ Manipulation (fakeout) $\to$ Distribution (true move) | All markets intraday, defines bias and timing | Systematizes "fakeout first, real move second", aligned with Judas Swing (§3.12) |
| **Silver Bullet** | Fixed time window: Liquidity sweep $\to$ FVG $\to$ Retest FVG $\to$ Entry | Crypto / US Stocks (NY 10–11am); Taiwan Stocks (09:00–10:00) | Time-filtered, noise reduction, schedulable, "one precise sniper trade a day" |
| **Unicorn Model** | Overlap of Breaker Block and FVG (+ SMT divergence bonus) | All markets, high-precision reversals | Highest confluence, minimal stop-loss, optimal RR |
| **SMT Divergence Model** | Correlation divergence at key liquidity zone (§3.13) | Markets with correlated pairs | Cross-asset confirmation of institutional rejection, filtering fake sweeps |

*Securing Strategy: First implement and backtest the three basic models in §5.1. Then, stack the four advanced models above as "confluence bonuses/toggleable settings" to compare their respective win rates and expectancy R (§10 calibration).*

---

## 6. Risk Control and Position Management

| Item | Rules | Alignment with Existing Project |
| :--- | :--- | :--- |
| **Stop-Loss Placement** | Outside the structural invalidation point (below sweep low / outside OB boundary / outside leg origin) | Replaces/complements the fixed -5% rule |
| **Risk-to-Reward Ratio (RR)** | Pre-entry RR $\ge$ 1:2 (minimum 1:1.5) | Aligns with "odds > 1:1.5" rule |
| **Single-Trade Risk** | 0.5%–1% of account size (determine position size based on ATR / stop-loss distance) | Aligns with the single-stock -5% and overall -NT$50k maximum cap |
| **Position Size Calculation** | $\text{qty} = \frac{\text{Account} \times \text{Risk\%}}{\text{Entry Price} - \text{Stop-Loss Price}}$ | Integrated with Kelly Criterion limits (academic authority role) |
| **Partial Profit Taking** | TP1: Exit 1/2 at the previous liquidity target and move SL to breakeven | Aligns with the short-term hunter "+8% exit half position" logic |
| **Max Simultaneous Positions** | Bound by market correlation (leverage must be reduced for highly correlated crypto positions) | Aligns with the 3–4 asset diversification rule |
| **Crypto-Specific** | Leverage caps, funding rate costs, liquidation price monitoring | New feature |

---

## 7. System Architecture and Module Decomposition

```
smc_strategy/
├── data/                     # Data Layer (multi-market adapters)
│   ├── providers/
│   │   ├── crypto.py         # [Primary] Crypto OHLCV: ccxt (Binance/OKX/Bybit) + websocket
│   │   ├── crypto_derivs.py  # [Primary] Liquidation heatmaps/OI/funding/L-S: Coinglass/Coinalyze/Coinank (§17.2)
│   │   ├── crypto_flow.py    # [Primary] CVD, Coinbase premium, BTC.D, on-chain flows (§17.3-17.4)
│   │   ├── us_stock.py       # US Stocks: yfinance/Polygon/Alpaca
│   │   └── tw_stock.py       # Taiwan Stocks: TWSE/yfinance/FinMind
│   ├── normalizer.py         # Normalizes to lowercase OHLCV + tz-aware index (UTC daily boundary for Crypto)
│   └── cache.py              # Local cache (parquet format)
├── core/                     # SMC Core Indicator Engine (Market-Agnostic)
│   ├── swings.py             # 3.1 Swing Highs / Lows
│   ├── structure.py          # 3.2 BOS/CHoCH (internal + swing)
│   ├── order_blocks.py       # 3.3 OB / Breaker / Mitigation
│   ├── fvg.py                # 3.4 FVG / IFVG
│   ├── liquidity.py          # 3.5 BSL/SSL / Equal Highs/Lows / Sweep
│   ├── premium_discount.py   # 3.6 Premium/Discount / Equilibrium
│   ├── ote.py                # 3.7 OTE / Fibonacci
│   ├── prev_high_low.py      # 3.8 Previous High/Low (multi-timeframe)
│   ├── sessions.py           # 3.9 Session / Killzone
│   ├── retracements.py       # 3.10 Retracements
│   ├── displacement.py       # 3.11 Displacement Filter
│   ├── judas.py              # 3.12 Judas Swing (session fakeout)
│   └── smt.py                # 3.13 SMT Divergence (correlated asset alignment)
├── crypto/                   # [Primary] Crypto-Specific Enhancements (Appendix C §17)
│   ├── liquidations.py       # 17.2 Liquidation clusters -> Real BSL/SSL, OI drop squeeze determination
│   ├── cvd.py                # 17.3 CVD divergence, spot vs. perp order flow confirmation
│   ├── cross_market.py       # 17.4 Coinbase premium, BTC.D, cross-exchange SMT, CME Gaps
│   └── adaptive_params.py    # 17.6 ATR-adaptive parameters, asset volatility classification
├── analysis/
│   ├── mtf_engine.py         # Section 4: Multi-Timeframe Top-Down Orchestration
│   ├── poi.py                # POI screening and prioritization (including DOL targets)
│   └── confluence.py         # Section 5: Confluence Scoring System
├── signals/
│   ├── entry_models.py       # 5.1 Three basic models + 5.3 Advanced models (AMD/Silver Bullet/Unicorn/SMT)
│   └── signal.py             # Standard Signal Object (including score, SL, TP, DOL, RR, model name)
├── risk/
│   ├── position_sizing.py    # Section 6: Position sizing calculations
│   ├── risk_manager.py       # RR filters, single/portfolio risk, crypto leverage/liquidation
│   └── rule_engine.py        # 10.5 Rule Enforcement Layer (4-number check, limit-triggered locking)
├── backtest/
│   ├── engine.py             # Event-Driven Backtesting Engine (lookahead-proof)
│   ├── metrics.py            # Win rate/RR/Profit Factor/Max Drawdown/Expectancy R
│   ├── walk_forward.py       # Walk-forward validation and parameter optimization
│   ├── journal.py            # 10.5 Forward testing / paper trading log (screenshots, scores, emotions)
│   └── trades.parquet        # 18.2 Trade records (features + outcomes), shared across backtest/paper/live trading
├── learning/                 # Appendix D §18: Closed-Loop Learning & Extraction
│   ├── trade_store.py        # 18.2 Trade records schema read/write
│   ├── attribution.py        # 18.3 Single factor lift / clustering / MAE-MFE / R-distribution
│   ├── feature_importance.py # 18.4 LogReg/XGBoost + SHAP (auxiliary validation)
│   ├── calibration.py        # 18.4 Weight/threshold/regime/Kelly calibration -> strategy.yaml
│   ├── regime.py             # Market regime classifier (trending/ranging/volatile/BTC.D)
│   ├── cross_val.py          # 18.6 Purged/Combinatorial Purged CV, PBO, Deflated Sharpe
│   └── decay_monitor.py      # 18.6 Edge decay monitoring
├── report/
│   ├── charts.py             # K-line overlay plots (OB/FVG/Liquidity/Structure)
│   └── html_report.py        # Generates daily_report_YYYYMMDD.html (aligned with existing formats)
├── config/
│   ├── markets.yaml          # Market-specific parameters (session times, tick size, limits, fees)
│   └── strategy.yaml         # Strategy-specific parameters (swing_length, weights, RR threshold)
├── skills/
│   └── smc_structure_analyst.skill   # New AI Agent Role (see Section 11)
└── tests/                    # Unit tests + chart pattern regression tests
```

**Design Principles:**
*   **Market-Agnostic Core Engine:** All functions in `core/` accept only normalized OHLCV data, shared across Taiwan, US, and Crypto markets.
*   **Differences Isolated in Config:** Session hours, tick size, daily price limits (±10% for Taiwan stocks), transaction fees, and funding rates are all configured within `config/markets.yaml`.
*   **Stateless Pure Functions:** Every indicator function takes a DataFrame as input and outputs a mutated DataFrame, facilitating unit testing and exact backtest replication.

---

## 8. Mandatory Items and Sub-items (WBS - Work Breakdown Structure)

The checkboxes below can serve as a project progress tracker. Each item includes its Definition of Done (DoD).

### M0. Project Infrastructure
*   [ ] **0.1 Environment & Dependencies:** Python 3.11, pandas, numpy, ccxt, yfinance, TA-Lib/pandas-ta, matplotlib/plotly, pytest. Fix dependency versions in `requirements.txt`.
*   [ ] **0.2 Project Skeleton:** Create the directory structure and empty modules according to Section 7.
*   [ ] **0.3 Configuration Files:** Draft `config/markets.yaml` and `config/strategy.yaml`.
*   *DoD:* `pip install -r requirements.txt` executes successfully, all modules can be imported, and empty tests run without errors.

### M1. Data Layer (Crypto-Focused)
*   [ ] **1.1 [Primary] Crypto OHLCV Provider:** Ingest multi-timeframe (1m to 1W) data from Binance/OKX/Bybit via ccxt, with real-time websocket support.
*   [ ] **1.2 [Primary] Crypto Derivatives Provider:** Ingest liquidation heatmaps, Open Interest (OI), funding rates, and Long/Short ratios from Coinglass/Coinalyze/Coinank (§17.2).
*   [ ] **1.3 [Primary] Crypto Flow Provider:** Ingest CVD, Coinbase premium, BTC.D, and on-chain exchange net flows (§17.3-17.4).
*   [ ] **1.4 Normalizer:** Standardize to lowercase OHLCV field names and tz-aware index (using UTC 00:00 as the daily boundary for Crypto).
*   [ ] **1.5 Multi-Exchange Aggregator:** Aggregate OHLCV data across major exchanges to filter out fake sweeps caused by single-exchange spikes (§17.9).
*   [ ] **1.6 Cache:** Establish local parquet caching and incremental updates.
*   [ ] **1.7 (Next Phase) US/Taiwan Stock Providers:** Ingest data via yfinance/Polygon/Alpaca and TWSE/FinMind.
*   *DoD:* BTC/ETH multi-timeframe OHLCV + liquidation/OI/funding/CVD data are retrieved, aligned, free of missing values, and time-zone corrected.

### M2. SMC Core Indicator Engine (ref. Section 3; All Mandatory)
*   [ ] **2.1 Swing Highs/Lows (§3.1):** Includes confirmation indexes and lookahead-proofing.
*   [ ] **2.2 BOS / CHoCH (§3.2):** Standardizes internal and swing structure levels.
*   [ ] **2.3 Order Blocks (§3.3):** Includes Breaker, Mitigation, unmitigated flags, and OB 50% refined entry.
*   [ ] **2.4 FVG / IFVG (§3.4):** Includes displacement filtering and fill indexes.
*   [ ] **2.5 Liquidity (§3.5):** Identifies BSL/SSL, equal highs/lows, sweeps, PDH/PDL/round numbers, internal/external liquidity, and DOL targets.
*   [ ] **2.6 Premium/Discount + Equilibrium (§3.6).**
*   [ ] **2.7 OTE / Fibonacci (§3.7):** Includes extension targets.
*   [ ] **2.8 Previous High/Low (Multi-Timeframe) (§3.8).**
*   [ ] **2.9 Sessions / Killzones (§3.9):** Incorporates session settings for all three markets.
*   [ ] **2.10 Retracements (§3.10).**
*   [ ] **2.11 Displacement Filter (§3.11).**
*   [ ] **2.12 Judas Swing (§3.12):** Detects session-level fakeouts.
*   [ ] **2.13 SMT Divergence (§3.13):** Includes the correlated asset alignment sub-module.
*   *DoD:* Unit tests written for every function + cross-validation against `joshyattridge/smart-money-concepts` (variance < threshold), regression validated using 1-2 manually annotated charts.

### M2C. Crypto-Specific Enhancements Engine (ref. §17; Primary Path, Prioritized)
*   [ ] **C.1 Liquidation Clusters $\to$ Real BSL/SSL:** Classify liquidation heatmap clusters as high-priority liquidity levels and DOL targets (§17.2).
*   [ ] **C.2 OI / Funding / L-S Ratio Integration:** Assess whether a sweep is a "leverage squeeze" vs. "genuine breakout" (§17.2).
*   [ ] **C.3 CVD / Order Flow Confirmation:** CVD divergence and spot vs. perp divergence (§17.3).
*   [ ] **C.4 Cross-Market Institutional Footprint:** Ingest Coinbase premium, BTC.D, cross-exchange SMT, and CME Gaps (with 24/7 validity caveats) (§17.4).
*   [ ] **C.5 Volatility-Adaptive Parameters:** Classify asset volatility using dynamic ATR parameters (§17.6).
*   [ ] **C.6 Crypto Confluence Scoring:** Integrate crypto factors into the §5.2 confluence matrix (§17.10).
*   *DoD:* BTC/ETH liquidations, OI, funding, and CVD are correctly annotated and integrated into the confluence scoring; multi-exchange aggregation successfully filters out single-exchange wick anomalies; ATR parameters adapt dynamically based on token volatility.

### M3. Analysis and Signal Layer
*   [ ] **3.1 MTF Ingestion Engine:** Connects HTF $\to$ MTF $\to$ LTF workflows and propagates bias.
*   [ ] **3.2 POI Filtering:** Rank priority order for unmitigated OBs / unfilled FVGs / liquidity targets.
*   [ ] **3.3 Confluence Scoring System:** Configurable weights + threshold, outputting details of scores and active factors.
*   [ ] **3.4 Three Entry Models:** Sweep + CHoCH, OB/FVG continuation, and OTE.
*   [ ] **3.5 Advanced High-Confluence Models (Toggleable):** Power of Three/AMD, Silver Bullet, Unicorn, and SMT Divergence (§5.3).
*   [ ] **3.6 Unified Signal Object:** Incorporates direction, entry, stop-loss, TP1/TP2, DOL target, RR, score, timestamp, market, and model name.
*   *DoD:* Outputs a structured signal list (containing all fields) for any given slice of historical data.

### M4. Risk Control and Position Management
*   [ ] **4.1 Stop-Loss Rules Engine:** Calculates structural invalidation levels.
*   [ ] **4.2 Position Sizing:** Computes target sizing based on risk percentage and stop-loss distance.
*   [ ] **4.3 RR Filter:** Automatically filters out signals with RR < 1:1.5.
*   [ ] **4.4 Portfolio Risk Cap:** Aligns with existing NT$50k / -5% portfolio loss caps; adds leverage and liquidation checks for crypto.
*   [ ] **4.5 Rule Enforcement Layer:** Automatically outputs 4 key metrics (equity, daily loss limit buffer, max drawdown buffer, days traded) before order submission, locking order entry if limits are breached (§10.5).
*   *DoD:* Every signal attaches correct position size and risk amount; limit-exceeding signals are blocked.

### M5. Backtesting Engine
*   [ ] **5.1 Event-Driven Backtesting Core:** Bar-by-bar progression, restricting data access to confirmed bars to prevent lookahead/painting bias.
*   [ ] **5.2 In-fill Models:** Simulates slippage, commission, Taiwan stock taxes, crypto maker/taker fees, and funding rates.
*   [ ] **5.3 Performance Metrics:** Compiles win rate, risk-reward ratio, Profit Factor, expectancy R, maximum drawdown, and Sharpe ratio.
*   [ ] **5.4 Walk-Forward / Parameter Optimization:** Applies walk-forward processes to prevent overfitting.
*   [ ] **5.5 Multi-Market Batch Testing:** Executes backtests across Taiwan stock lists, US stocks, and major crypto pairs.
*   [ ] **5.6 Forward Testing & Trading Journal:** Executes demo/paper trading for $\ge$ 1 month (recording 50-100 trades with screenshots, scores, and emotions) and cross-references results with backtests (§10.5).
*   *DoD:* Replicates industry benchmark metrics (e.g., sample of ~2,600 trades yielding ~61% win rate, PF 2.17, avg +2.27R) and outputs reports; identical parameters yield identical results; deviation between forward testing and backtesting remains within tolerance.

### M5L. Win/Loss Learning Extraction Closed-Loop (ref. §18 Appendix D; Continuous Optimization)
*   [x] **L.1 Trade Records Schema:** Save all backtest, paper, and live trades to a unified database (`trades.parquet`) storing features + outcomes (§18.2).
*   [x] **L.2 Attribution Analysis:** Calculates single-factor lift, clustering analysis, MAE/MFE statistics, and R-multiple distributions (§18.3).
*   [x] **L.3 Calibrator:** Automatically generates proposed updates to `strategy.yaml` (weights, thresholds, regime mappings, Kelly caps; §18.4).
*   [x] **L.4 Out-of-Sample Validation:** Validates using Purged/Combinatorial Purged CV, walk-forward testing, and PBO/Deflated Sharpe ratio analyses (§18.6).
*   [x] **L.5 Feature Importance (Auxiliary):** Uses SHAP values to evaluate feature relevance and prevent overfitting (§18.4).
*   [x] **L.6 Monthly Strategy Health Report:** Monitors strategy edge decay (§18.7).
*   *DoD:* Automatically generates "positive/negative expectancy factor rankings + MAE/MFE stop-loss recommendations + candidate weights" from `trades.parquet`; updates to `strategy.yaml` are allowed only after passing out-of-sample validation.

### M6. Reporting and Visualization
*   [ ] **6.1 K-Line Overlay Plots:** Automatically plots OB, FVG, liquidity levels, structural markers, Judas Swings, and trade entries on K-line charts (automating the "8-quadrant SMC chart interpretation"). Full chart specifications detailed in Appendix A (C1–C12).
*   [ ] **6.2 Daily HTML Report:** Generates `daily_report_YYYYMMDD.html` matching existing file naming and CSS styles.
*   [ ] **6.3 Signal Summary Table:** Compiles today's SMC signals across all markets, including confluence scores and entry/exit targets.
*   *DoD:* One-click generation of the HTML report containing charts and signal tables, fully aligned with the team's visual style.

### M7. AI Team Integration (ref. Section 11)
*   [ ] **7.1 Agent Role Creation:** Create `smc_structure_analyst.skill`.
*   [ ] **7.2 Workflow Integration:** Register in `load_skills.py` and the main `stock_team.skill` workflow.
*   [ ] **7.3 Confluence Logic:** Implement signal confluence rules with `tech_executor`, `short_term_hunter`, and `institutional_analyst`.
*   *DoD:* The SMC analyst successfully generates and cross-validates signals in morning brief, intraday execution, and post-market review workflows.

### M8. Automation and Scheduling (Optional Future Milestones)
*   [ ] **8.1 Cron Scheduling:** Automatically fetches data and generates daily reports pre- and post-market.
*   [ ] **8.2 Real-time Alerts:** Pushes notifications when price enters HTF POIs or sweeps occur.
*   [ ] **8.3 Live Execution Broker Interface:** Connects to Taiwan broker APIs and crypto ccxt execution endpoints (preceded by paper trading).

---

## 9. Data Requirements and Multi-Market Differentiation Adjustments

| Dimension | Taiwan Stocks | US Stocks | Cryptocurrencies |
| :--- | :--- | :--- | :--- |
| **Data Sources** | TWSE, yfinance `.TW`, FinMind, CMoney | yfinance, Polygon, Alpaca | ccxt (Binance/OKX/Bybit) |
| **Trading Hours** | 09:00–13:30 Single session | 09:30–16:00 ET (+ pre/post-market) | 24/7 |
| **Session/Killzone** | Morning session 09:00–10:00 primary | Opening Range (first 30m), Noon | London/NY killzones, weekends weighted lower |
| **Daily Price Limit** | ±10% (affects sweep/gap logic) | None (except circuit breakers) | None (but subject to high-volatility spikes) |
| **Gaps** | Overnight gaps common (not FVG) | Overnight gaps common | Almost no overnight gaps (continuous) |
| **Settlement/Costs** | T+2, 0.3% transaction tax, commissions | T+2, low transaction fees | Immediate, maker/taker fees, funding rates |
| **Liquidity Targets** | PDH/PDL, limit-up/down limit order books | PDH/PDL, round numbers, ORB | PDH/PDL, round numbers, swing highs/lows, liquidation pools |
| **Institutional Inflows** | Three Major Institutional net buys/sells (strong, existing project edge) | 13F filings, Dark Pools | On-chain flows, exchange net inflows, Open Interest (OI) |
| **Specific Risks** | Illiquid stocks yield excessive fakeouts | Earnings release gaps | High-volatility spikes, liquidation cascades, funding fees |

**Implementation Points:**
*   **Daily price limits** "flatten" gaps and sweeps $\to$ Taiwan stock implementations must treat the ±10% limit price as artificial liquidity and structural boundaries.
*   **Crypto's 24/7 continuous trading** with no overnight gaps represents the purest environment for FVG/liquidity definitions. It is recommended to use crypto as the primary backtesting target for the core engine (clean data, large sample size, fast verification).
*   Taiwan stock's unique **Three Major Institutional Net Buys/Sells** data can serve as external validation of SMC's "institutional footprints" (comparing SMC-predicted direction vs. actual institutional buying/selling).

---

## 10. Backtesting and Verification Methodology

### 10.1 Converting Subjective Concepts into Binary Rules
SMC's greatest risk is subjectivity. Every concept must be translated into programmable Boolean conditions (e.g., "OB unmitigated AND overlaps with discount zone AND RR $\ge$ 2"). Testing isolated OBs or FVGs typically yields poor results; they must be evaluated under confluence rules.

### 10.2 Bias Prevention Checklist (Mandatory)
*   **Lookahead Bias:** Swings require $n$ future bars for confirmation. Backtests must only use swing data *after* the confirmation bar index. Never draw structure using future data.
*   **Repainting Bias:** OBs/FVGs/structures can be redefined by subsequent candles. The strategy must freeze the "currently visible" state for decision-making.
*   **Survivorship Bias:** Stock baskets must include delisted tickers; crypto assets must include delisted pairs.
*   **Data Quality:** Use consolidated tickers or exchange-grade historical feeds to avoid bid-ask spread distortions.
*   **Overfitting:** Use Walk-Forward + Out-of-Sample (OOS) validation. Do not fit parameters to a single market regime.

### 10.3 Performance Acceptance Criteria (Target Reference)

| Metric | Minimum Acceptable Threshold |
| :--- | :--- |
| **Win Rate** | $\ge$ 55% (industry average for SMC backtesting is ~61%) |
| **Profit Factor** | $\ge$ 1.5 (excellent is $\ge$ 2.0) |
| **Avg. Gain per Trade** | $\ge$ +1R (industry average is ~+2.27R) |
| **Max Drawdown** | $\le$ 20% |
| **Sample Size** | $\ge$ several hundred trades (across multiple symbols/markets) |

### 10.4 Verification Pipeline
1.  **Unit Testing** (indicators verification) $\to$ 2. **Cross-validation** against open-source libraries.
3.  **Single-asset backtest** $\to$ 4. **Batch multi-asset, multi-market backtesting**.
5.  **Walk-Forward Out-of-Sample validation** $\to$ 6. **Paper trading** real-time validation.
7.  **Small-capital live trading** $\to$ 8. **Full production deployment**.

### 10.5 Forward Testing and Trading Discipline (Adapted from international prop firm practices, see Appendix B)
*   **Forward/Paper Testing:** Run a demo/simulation account under identical live trading conditions (same capital, leverage, assets) for at least 1 month. Confirm real-time metrics match backtested metrics. Automated bots must pass $\ge$ 1 month of paper testing before production (preventing overfitting/repainting from appearing live).
*   **Journaling:** Record 50-100 paper trades. Each trade must include entry/exit screenshots (corresponding to Appendix A C10 signal charts), confluence score, trade rationale, and current emotional state. Compile actual win rates and expectancy R to cross-reference with backtests.
*   **Rule Enforcement Layer:** Prop firm statistics show that ~90% of failures stem from rule violations rather than poor strategies. Before placing any order, the system must check and output 4 numbers, automatically disabling execution if limits are breached:
    1.  Current account equity.
    2.  Daily loss limit buffer.
    3.  Max drawdown limit buffer.
    4.  Active days traded.
    *   This directly aligns with the team's existing rules: **single-stock loss limit -5% / overall loss limit -NT$50k / shift to defensive mode upon reaching +NT$80k**.

### 10.6 Closed-Loop Learning & Win/Loss Extraction (ref. Appendix D, §18)
Every trade executed in backtesting, paper trading, or live trading is recorded as a unified "feature $\to$ outcome" sample in `trades.parquet`. The `learning/` module evaluates win/loss attribution (identifying which features generated edge) $\to$ calibrates confluence weights and parameters $\to$ executes out-of-sample validation $\to$ updates live settings. This upgrades the strategy from static rules to a continuously self-optimizing engine. Rigorous cross-validation methods (Purged CV, PBO, MAE/MFE) are detailed in §18.6.

---

## 11. Integration with the Existing AI Trading Team

### 11.1 New Agent Role: SMC Structure Analyst (`smc_structure_analyst.skill`)
*   **Positioning:** Institutional footprint interpreter, enhancing existing technical indicators (moving averages/MACD) in terms of "entry precision" and "stop-loss placement".
*   **Core Responsibilities:**
    1.  Perform daily Top-Down (Weekly $\to$ Daily $\to$ Hourly) structural analysis on watchlist symbols, marking bias, POIs, and liquidity targets.
    2.  Output structured SMC signals (entry zones, stop-losses, TPs, RR, and confluence scores).
    3.  Cross-validate SMC-inferred institutional directions against actual net buys/sells of the Three Major Institutions.
*   **Collaborative Workflows:**

| Output Shared With | Content Provided | Intended Use |
| :--- | :--- | :--- |
| **Technical Executor (`tech_executor`)** | OB/FVG precise entry zones + structural stop-losses | Replaces fixed -5% stops, optimizing RR |
| **Short-Term Hunter** | Liquidity sweep + CHoCH reversal signals | Spotting institutional footprints before short-term breakout |
| **Institutional Analyst** | SMC institutional bias projections | Cross-validates with net buys/sells data |
| **Wall Street Chief** | HTF structures and macro bias | Aids in macro direction anchoring |

### 11.2 Integrating into the Existing Workflow (`stock_team.skill` workflow)
*   `daily_morning_briefing`: Add the `smc_structure_analyst` step $\to$ providing structural bias and POIs for watchlists.
*   `intraday_execution`: Send SMC alert triggers to execution agents when price enters HTF POIs or sweeps occur.
*   `after_market_review`: Evaluate today's SMC signal hit rates, feeding results back to optimize confluence weights.

### 11.3 Report Integration
Integrate SMC charts and signal tables into `daily_report_YYYYMMDD.html`, sitting alongside institutional flows, PTT sentiment, and classic technical analysis to form a cohesive "Macro + Inflow + Technical + SMC + Sentiment" 5-in-1 Daily Report.

---

## 12. Risks, Limitations, and Known Pitfalls

### 12.1 Conceptual Risks
*   **High Subjectivity:** Different individuals draw different OBs/structures from the same chart $\to$ Must resolve using deterministic algorithms + weighted confluence scoring (§5.2).
*   **Hindsight Bias:** Standard SMC tutorials cherry-pick winning charts, leading to overstated live win rates $\to$ Resolve via strict out-of-sample backtesting.
*   **Marketing Claims Warning (Appendix B-⑫):** Public materials often advertise "90–95% win rates," which are almost exclusively cherry-picked. This plan adheres to the rigorous backtests outlined in §10, with realistic win rate expectations of 55–65%.
*   **Isolated Factor Failure:** Single isolated factors (like solo OBs or FVGs) have low win rates $\to$ Orders must only be executed under multi-confluence settings.

### 12.2 Technical Implementation Pitfalls (Easiest to Miss)
*   **Lookahead/Repainting:** Swing and structural definitions rely on "future validation," which is the primary source of inflated backtesting metrics. You must freeze states at the execution boundary.
*   **Parameter Sensitivity:** Settings like `swing_length` and `range_percent` heavily influence results. Calibrate these variables per market and timeframe; do not apply uniform parameters.
*   **Daily Price Limits and Gaps:** Taiwan stocks' ±10% limits and overnight gaps disrupt FVG/sweep definitions. These require special edge-case handling.
*   **Crypto Spikes/Wicks:** Exchange-specific cascades can trigger fake sweeps. Resolve using volume filters or multi-exchange price aggregation.

### 12.3 Risk Management Red Lines (Aligning with Team Rules)
*   Single position stop-loss capped at -5% (or structural invalidation level, whichever is closer); overall account drawdown capped at -NT$50k triggers system lock; shift to defensive mode upon reaching +NT$80k.
*   *Crypto additions:* Leverage limits, liquidation distance $\ge$ stop-loss distance $\times$ 2, and funding fees accounted for as cost.

---

## 13. Development Milestones

| Phase | Content | Corresponding WBS | Priority / Estimate |
| :--- | :--- | :--- | :--- |
| **Phase 1** | Crypto Data Layer + Core Indicator Engine (all in §3) | M0–M2 | Core, High Priority |
| **Phase 1.5** | Crypto-Specific Enhancements Engine (liquidations/OI/funding/CVD/cross-market, §17) | M2C | Key Path Critical |
| **Phase 2** | Analysis/Signal Layer + Risk Control (MTF, confluence, entry models, rules enforcement) | M3–M4 | Medium Priority |
| **Phase 3** | Backtesting Engine + Validation (large crypto sample, funding/liquidation simulation) | M5, §10 | Key Validation |
| **Phase 4** | Report Visualization (C1–C13 + liquidation overlay) + AI Team Integration | M6–M7 | Deployment |
| **Phase 5** | Automation Scheduling + Forward/Paper Trading $\to$ Live Execution (final) | M8 | Launch |
| **Phase 5.5** | Win/Loss Learning Extraction Closed-Loop (`trades.parquet` $\to$ attribution $\to$ calibration $\to$ OOS validation) | M5L, §18 | Continuous Optimization |
| **Phase 6** | (Next Stage) Expansion to US/Taiwan Stocks, handling gaps and price limit cases | M1.7, etc. | Expansion |

*Proposed Execution Path (Crypto-First): First run the core engine of §3 on BTC/USDT 1H/15m (clean, high-volume, continuous 24/7). Immediately overlay §17 liquidation heatmaps + OI + funding + CVD to make liquidity "visible," then proceed to §10 backtesting validation. Once stable, expand backwards to US and Taiwan stocks.*

---

## 14. References

**SMC Concepts and Strategies:**
*   *Smart Money Concepts (SMC) Trading Strategy: Complete 2025 Guide* — SignalWavesAI
*   *A Strategist's Guide to Smart Money Concepts (SMC)* — Daolien, Medium
*   *SMC Trading Strategy Explained* — NordFX
*   *Smart Money Concepts (SMC) Core Principles* — The5ers

**Algorithms & Open-Source Implementations:**
*   `joshyattridge/smart-money-concepts` (Python Package, basis for function interfaces in this document) — GitHub
*   `Smart Money Concepts (SMC) Python Package` — PyPI

**Liquidity / BSL / SSL / Sweeps:**
*   *Buy Side & Sell Side Liquidity (BSL & SSL)* — TradingFinder
*   *Liquidity Sweep — SMC and ICT Trading Concept* — Writofinance
*   *Liquidity — The Real Reason Price Moves* — TradingStrategyGuides

**Premium/Discount / OTE / Fibonacci:**
*   *ICT Premium and Discount Zone* — InnerCircleTrader.net
*   *Understanding ICT Optimal Trade Entry (OTE) With Fibonacci* — TradingStrategyGuides
*   *The Fibonacci Framework That Defines Every Entry* — ICT Flow

**Backtesting and Verification:**
*   *Backtesting Smart Money Concepts* — HorizonAI Learn
*   *I Backtested 2,600 Trades Using Smart Money Concepts* — Quantum Algo, Medium
*   *Backtesting AI Crypto Strategies — Avoiding Overfitting / Lookahead / Data Leakage* — Blockchain Council

**Advanced ICT Models (International Community, ref. Appendix B):**
*   *ICT Power of 3 — Accumulation, Manipulation, Distribution* — InnerCircleTrader.net
*   *ICT Unicorn Model — Breaker Block & FVG Overlap* — InnerCircleTrader.net
*   *Most Important ICT Concepts — Complete List* (includes Silver Bullet / SMT)

**Forward Testing and Trading Discipline (Prop Firm Practices, ref. Appendix B):**
*   *Forward Testing of Trading Strategies* — FTMO Academy
*   *How Paper Trading Prepares You for Real Markets* — FTMO
*   *Best FTMO Trading Journal: Track Rules & Stats* — Traders Second Brain

**Automation Frameworks and Quant Implementations (ref. Appendix B):**
*   `freqtrade` — Open-source crypto trading/backtesting framework — GitHub
*   `starckyang/smc_quant` — SMC Quant Trading — GitHub
*   *Automating ICT 2022 Model with Python and AI* — Farnam Rami, Medium

**Chinese Community (Domestic) SMC Practices (ref. Appendix B):**
*   *SMC Basic Tutorial: What is Smart Money? Liquidity, Order Block, FVG* — Full-Time Dad (全職奶爸)
*   *SMC Advanced: Order Block (OB) Validity 3-Filter Check* — Full-Time Dad (全職奶爸)
*   *SMC Advanced: Buy/Sell Liquidity, Hunting, and Liquidity Magnet DOL* — Full-Time Dad (全職奶爸)
*   *Crypto SMC Strategy: Order Blocks and Technical Analysis* — Gugu Business School (Gugu商學院)
*   *“SMC Trading Strategy” 95% Win Rate? Is Smart Money Really That Easy? (Risk Warning)* — BlockTempo (動區動趨)

**Trade Learning/Extraction & Overfitting Prevention (ref. Appendix D / §18):**
*   *MAE & MFE: How to Optimize Exits with Trade Excursion Data* — Traders Second Brain
*   *Trade Journal Metrics: R-multiple / Expectancy / MAE-MFE* — Forex Mechanics
*   *Purged Cross-Validation (López de Prado)* — Wikipedia
*   *A Rigorous Walk-Forward Validation Framework* (arXiv)
*   *Predicting Trade Profitability with Machine Learning (Ernie Chan)* — Better System Trader

**Crypto-Specific Data Sources & Practices (ref. Appendix C / §17):**
*   *CoinGlass* — Liquidation Heatmaps/OI/Funding/CVD/Order Flow
*   *CoinGlass Liquidation Heatmap* — BTC Liquidation Heatmap
*   *Coinalyze* — Open Interest / Funding / Liquidations Charts
*   *CME Bitcoin Gap Trading Guide (approx. 77% gap fill)* — Phemex Academy
*   *CME transitions to 24/7, rendering Bitcoin's most common technical indicator obsolete (2026-05)* — Crypto Times
*   *How to Trade Bitcoin CME Gaps* — Whaleportal

---

## 15. Appendix A: Chart Analysis (Visualization) Implementation List

This appendix specifies the chart visualizations that the SMC system must generate. Every chart represents the automated annotation of the outputs from §3 indicators onto actual candlestick charts. **Tech Stack:** Use Plotly for interactive charts and `mplfinance`/`matplotlib` for static plots. Output both PNG images and interactive HTML files, integrated into the daily report (corresponding to WBS M6).

### 15.0 Unified Visual Standards (Defined upfront, shared across all charts)
*   **Candlesticks:** Green-up/Red-down by default; can toggle to Taiwan stock conventions (Red-up/Green-down), controlled via `markets.yaml`.
*   Use blue/green tones for **bullish elements** and orange/red tones for **bearish elements**.
*   **Zones (OB/FVG):** Semi-transparent rectangles with borders. Unmitigated/unfilled = solid border; mitigated/filled = dashed border and greyed out.
*   **Horizontal Liquidity:** Dashed lines with right-aligned text labels (BSL / SSL / PDH / PDL / PWH / PWL).
*   **Structure Labels:** Mark `HH`/`HL`/`LH`/`LL` at swing pivots; mark `BOS`/`CHoCH` at break points.
*   **Execution Markers:** Entry $\blacktriangle$, stop-loss $\mathbf{\times}$, profit target $\bullet$, with RR and confluence score noted adjacent.
*   **Anti-Repainting Rule (Strictly enforced):** Only annotate "currently confirmed" structures on charts. Replays in backtesting must plot bar-by-bar to avoid lookahead visualization bias (see §12.2).

### 15.1 Mandatory Chart List (C1–C13)

| ID | Chart Name | Primary Content | Data Source (§3 Output Fields) | Timeframe | WBS Reference |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **C1** | **Structure Map** | Swing high/low connections, HH/HL/LH/LL labels, BOS/CHoCH breakout lines | 3.1 `HighLow`/`Level`, 3.2 `BOS`/`CHOCH`/`Level`/`BrokenIndex` | HTF + MTF | 2.1, 2.2, 6.1 |
| **C2** | **Order Block Map** | OB rectangles (green bullish, red bearish), mitigated/unmitigated flags, 50% mid-lines, Breaker/Mitigation zones | 3.3 `OB`, `Top`, `Bottom`, `MitigatedIndex`, `Percentage` | All | 2.3, 6.1 |
| **C3** | **FVG Gap Map** | FVG rectangles, unfilled/filled markers, IFVG lines | 3.4 `FVG`, `Top`, `Bottom`, `MitigatedIndex` | All | 2.4, 6.1 |
| **C4** | **Liquidity Map** | BSL/SSL horizontal lines, equal highs/lows, PDH/PDL/PWH/PWL, sweep markers $\bigstar$ | 3.5 `Liquidity`/`Level`/`Swept`, 3.8 `Previous*`/`Broken*` | HTF | 2.5, 2.8, 6.1 |
| **C5** | **Premium/Discount Map** | Dealing range boundaries, 50% Equilibrium line, premium/discount background shading | 3.6 `zone` + Fib levels | MTF | 2.6, 6.1 |
| **C6** | **OTE / Fibonacci Entry Map** | Impulse leg Fib levels, OTE 0.62–0.79 zone bands, 0.705 line, extension targets | 3.7 `OTE_zone`/`entry_0705`/`tp1`/`tp2` | LTF | 2.7, 6.1 |
| **C7** | **Session/Killzone + Judas Map** | Session background blocks, killzones, Judas fakeout and real direction arrows | 3.9 `Active`/`High`/`Low`, 3.12 `Judas`/`FalseMoveHigh`/`Low` | Intraday/LTF | 2.9, 2.12, 6.1 |
| **C8** | **Sweep Reversal Confirmation Map** | Annotated 3-step sequence: sweep $\to$ displacement $\to$ CHoCH | 3.5 `Swept`, 3.11 `displacement`, 3.2 `CHOCH` | LTF | 2.2, 2.5, 2.11, 6.1 |
| **C9** | **MTF Top-Down Composite Map** | Side-by-side or stacked HTF/MTF/LTF triple charts, displaying bias and POI alignment lines | Section 4 `mtf_engine`, `poi` | 3 Layers | 3.1, 3.2, 6.1 |
| **C10** | **Trade Setup Map** | Entry/SL/TP1/TP2 lines, RR, confluence score, entry model label | `signals.Signal`, 5.2 Confluence Score | Entry TF | 3.4, 3.5, 6.1 |
| **C11** | **Backtest Equity & Trades Map** | Equity curve, drawdown bands, buy/sell entry markers overlaid on K-line, R-multiple distribution histogram | `backtest.metrics` | — | 5.3, 6.1 |
| **C12** | **Daily Multi-Asset Dashboard** | Thumbnail grid of symbols + signal summary table, exported as a consolidated HTML file | All | Daily Report | 6.2, 6.3 |
| **C13** | **[Crypto Primary] Liquidation/Order Flow Overlay** | Liquidation cluster hotspots overlaid on K-line, OI and funding subplots, CVD subplot, CME gap lines | §17.2–17.4 Derivative Data | All | M2C, 6.1 |

### 15.2 Additional Chart Details
*   **C1 Structure Map:** Serves as the base chart for all others. Swings and BOS/CHoCH must be drawn correctly first for other charts to be meaningful. Distinguishes between internal (minor) and swing (major) structures (§3.2).
*   **C2 Order Block Map:** Rectangles drawn from `[Top, Bottom]`, and mid-lines drawn at `(Top+Bottom)/2` (§3.3 Refined Entry); OBs mitigated by price are greyed out, while unmitigated OBs are highlighted (ref. Appendix A, Diagram ②).
*   **C3 FVG Map:** Gap rectangles extend until the fill candle; if flipped into an IFVG after filling, change the color coding (ref. Appendix A, Diagram ④).
*   **C4 Liquidity Map:** Equal highs/lows are connected as horizontal lines, and the sweeping candlestick is marked with a star ($\bigstar$). This is the core chart for judging where institutions are seeking stop-losses (ref. Appendix A, Diagrams ①⑤).
*   **C5 Premium/Discount Map:** Use semi-transparent blocks to distinguish the upper half (Premium/Sell zone) from the lower half (Discount/Buy zone), drawing an equilibrium line at the 50% mark (ref. Appendix A, Diagram ⑤).
*   **C6 OTE Map:** Plot Fib levels from the impulse leg start/end points, highlighting the 0.62–0.79 band and the 0.705 level. Label as "High Probability" only if overlapping with an OB/FVG (ref. Appendix A, Diagram ⑦).
*   **C7 Session/Judas:** Background color-code Tokyo/London/NY sessions and killzones. Mark Judas fakeouts with fake-move arrows + real-move direction arrows (ref. Appendix A, Diagrams ⑥⑧).
*   **C8 Sweep Reversal:** Number and label the three steps on the K-line: "① Liquidate Sweep $\to$ ② Large Displacement $\to$ ③ CHoCH". This serves as the visual audit chart for Entry Model 1.
*   **C9 MTF Composite:** Display three timeframes side-by-side or vertically stacked, with connectors showing how LTF entries align with HTF POIs and Discount/Premium zones $\to$ validating confluence.
*   **C10 Signal Map:** Automatically generate one chart per triggered signal containing entry, exit, RR, and score for pre-market reviews and post-market audits.
*   **C11 Backtest Map:** Plots equity curve + drawdowns to evaluate strategy health. The trade replay chart is used for manual signal auditing and validating lookahead-proofing.
*   **C12 Dashboard:** Displays a thumbnail wall of daily C1–C10 charts sorted by §5.2 confluence score, directly embedded within `daily_report_YYYYMMDD.html`.
*   **C13 [Crypto Primary] Liquidation/Order Flow Overlay:** Main chart overlaps liquidation cluster hot zones (color intensity = liquidation volume) to make "real BSL/SSL" visible. Subplots display OI, funding, CVD, and CME Gaps. This is the core visualization for §17 Crypto Enhancements, representing the largest differentiator compared to stock setups.

### 15.3 Implementation Priority
*   **Batch 1 (Foundational):** C1 Structure Map $\to$ C4 Liquidity Map $\to$ C5 Premium/Discount Map
*   **Batch 2 (Zones):** C2 Order Block Map $\to$ C3 FVG Map $\to$ C6 OTE Map
*   **Batch 3 (Triggers):** C7 Session/Judas Map $\to$ C8 Sweep Reversal Map $\to$ C10 Signal Map
*   **Batch 4 (Integration):** C9 MTF Composite Map $\to$ C11 Backtest Map $\to$ C12 Daily Dashboard

*Recommendation: First plot C1–C6 on a single interactive Plotly chart for a single asset (e.g., BTC/USDT 1H) as a demo. After verifying visual standards and data validity, expand to C7–C12 and multi-market configurations.*

---

## 16. Appendix B: Reference and Decision Matrix for Domestic and International Virtual/Demo Trading Practices

This appendix records the results of auditing "domestic and international virtual/mock trading SMC practices": what was reviewed, what is worth adopting, whether it is introduced, and where it has been incorporated into this plan. Adopted decisions have been updated in the preceding chapters.

### 16.1 Source Comparison & Decision Matrix

| # | Practice / Source (Domestic & Int'l) | Reference Point | Decision | Target Section |
| :--- | :--- | :--- | :--- | :--- |
| **①** | International ICT — Power of Three (AMD) | Systematize "Accumulation $\to$ Manipulation (fakeout) $\to$ Distribution (real move)" for bias and timing | ✅ Adopted | §5.3, §3.12 |
| **②** | International ICT — Silver Bullet | Fixed NY 10–11am time window (Sweep $\to$ FVG $\to$ Entry) for noise reduction and scheduling | ✅ Adopted (Taiwan counterpart: 09:00–10:00) | §5.3, §3.9 |
| **③** | International ICT — Unicorn Model | Overlap of Breaker Block and FVG (+ SMT) for high-precision, low-stop entry | ✅ Adopted | §5.3 |
| **④** | International ICT — SMT Divergence | Divergence of correlated assets (ES/NQ, BTC/ETH, TSMC/Index) to confirm institutional rejection | ✅ Adopted (Added concept + alignment module) | §3.13, §5.3, WBS 2.13 |
| **⑤** | Prop Firm (FTMO) — Forward Testing | Run demo accounts under live conditions for $\ge$ 1 month to verify backtest consistency | ✅ Adopted | §10.5, WBS 5.6 |
| **⑥** | Prop Firm — Trading Journal | Maintain logs of 50–100 demo trades, detailing screenshots, scores, rationale, and emotions | ✅ Adopted | §10.5, WBS 5.6, C10 |
| **⑦** | Prop Firm — Rule Enforcement | Confirm 4 metrics pre-order, lock account upon breach (90% failures stem from violations) | ✅ Adopted (Aligned with -NT$50k / -5%) | §10.5, WBS 4.5 |
| **⑧** | Chinese Community (Full-Time Dad) | A-grade OB selection: Sweep + Displacement + Unmitigated | ✅ Adopted | §3.3 |
| **⑨** | Chinese Community — DOL Magnet | Define target liquidity as profit target pre-entry; no trade without DOL | ✅ Adopted | §3.5, Signal Object |
| **⑩** | Automated Frameworks (`freqtrade` / `smc_quant`) | Open-source backtest/live execution; multi-confirmations; paper test $\ge$ 1 month before live | ✅ Partially Adopted (Backtest architecture reference; optional integration) | §7, §10.5 |
| **⑪** | Crypto Quant (Overfitting Protection) | Strict walk-forward and lookahead protections | ✅ Reinforced | §10.2, §10.4 |
| **⑫** | Marketing Claims of "95% Win Rate" | Exaggerated win rates on public tutorials | ⚠️ Rejected (Flagged as risk warning) | §12.1 |
| **⑬** | Quarterly Theory (Time Theory) | Finer time divisions | ⏸ Postponed (High complexity, unverified edge) | — |
| **⑭** | Auto-execution Bots (TG/WebUI control) | Fully automated execution | ⏸ Postponed (Limited to signals, backtesting, and paper trading) | §8 M8 |

### 16.2 Summary of Structural Changes Post-Adoption
*   **Added 2 Concepts:** §3.13 SMT Divergence, §3.5 DOL Target Liquidity.
*   **Added 1 Group of Advanced Models:** §5.3 (Power of Three / Silver Bullet / Unicorn / SMT), stacked as toggleable settings on top of the three basic models.
*   **Reinforced Validation:** §10.5 Forward Testing + Trading Journal + Rule Enforcement Layer (WBS 4.5, 5.6).
*   **Enhanced OB Quality:** §3.3 3-Filter Validity Check (Grade-A OBs).
*   **New Modules Created:** `core/judas.py`, `core/smt.py`, `risk/rule_engine.py`, `backtest/journal.py`.

### 16.3 Rejected/Postponed Items & Rationale
*   **"95% Win Rate" Claims:** Public claims are post-facto cherry-picked and conflict with rigorous backtesting (§10). Retained only as a risk warning.
*   **Quarterly Theory:** Marginal utility is unclear, adding parameter size and subjectivity. Re-evaluate after core engine validation.
*   **Fully Automated Order Routing:** Ensure signal quality and discipline (passing paper trading) first before implementing live execution to avoid routing unverified strategies.

---

## 17. Appendix C: Cryptocurrency-Exclusive SMC Enhancements (Primary Application Scope)

This project prioritizes cryptocurrencies as its primary application. This appendix outlines the "institutional footprint" data sources and methodologies unique to crypto markets. **Core Insight:** In traditional equity and FX markets, SMC can only "infer" stop-loss clusters. However, crypto derivatives data makes liquidity "visible" (liquidation heatmaps, Open Interest, funding rates, CVD, cross-exchange premiums). This upgrades §3.5 Liquidity from "speculative estimation" to "empirical measurement."

### 17.1 Why Crypto is Most Suited for SMC
*   **24/7 Continuous Trading:** Almost no overnight gaps $\to$ FVG/imbalance definitions are preserved in their purest form.
*   **High Leverage:** Leverage leads to dense clusters of stop-loss and liquidation thresholds $\to$ Liquidity sweeps are highly frequent and distinct, representing prime targets for SMC logic.
*   **Transparent Derivatives Data:** Open access to OI, funding rates, liquidations, and CVD $\to$ Institutional vs. retail positioning is quantifiable, providing a unique edge unavailable in stock markets.
*   **Abundant, Free Historical Data:** Easily backtested $\to$ Accelerating the §10 validation cycle.

### 17.2 Crypto-Specific "Visible Liquidity" Data Sources (Enhancing §3.5)

| Data Type | Source | Significance for SMC | Practical Application |
| :--- | :--- | :--- | :--- |
| **Liquidation Heatmaps** | Coinglass, Coinank | Dense clusters of leveraged stop-losses / liquidations = real BSL/SSL | Treat liquidation clusters as DOL targets; price is drawn to sweep these pools |
| **Open Interest (OI)** | Coinglass, Coinalyze | Detects if a sweep is driven by "new positions" or "short/long squeezes" | Sweep + OI drop = squeeze/liquidation cascade (high probability reversal); sweep + OI rise = genuine breakout |
| **Funding Rates** | Exchanges, Coinglass | Extreme values indicate crowded positions, serving as fuel for sweeps | Excessively positive rate (crowded longs) $\to$ vulnerable to downside sweep; excessively negative rate $\to$ vulnerable to short-squeeze |
| **Long/Short Ratio** | Coinglass | Retail position positioning (contrarian indicator) | Extreme retail bullish bias + price at premium resistance $\to$ distribution warning |

*   **New Detection Rules (Liquidation-Enhanced Sweep):**
    *   If a liquidation cluster (short margin liquidations) exists above a swing high $\to$ label as a high-priority BSL.
    *   Price pierces this cluster + CVD shows selling pressure divergence + OI drops sharply $\to$ High probability bearish sweep (long squeeze followed by reversal).
    *   *(Bullish SSL is symmetrical: liquidation clusters below swing low + CVD buying pressure divergence + OI drops).*

### 17.3 Order Flow Confirmation (Perp vs. Spot) — Validating Sweep Authenticity
*   **CVD (Cumulative Volume Delta) Divergence:** Price makes a new high but CVD fails to make a new high $\to$ buying exhaustion, confirming a fake sweep $\to$ Bearish confirmation. This is crypto's version of order flow validation, reinforcing §3.11 displacement.
*   **Spot vs. Perp Divergence:** Spot-driven rallies represent genuine demand; perpetual-driven moves indicate leveraged speculation (prone to rapid liquidation reversals).
*   **L2/L3 Order Book Depth:** Large limit order blocks act as liquidity magnet walls; supports detection of iceberg orders.

### 17.4 Cross-Market Institutional Footprint & SMT (Enhancing §3.13)
*   **Coinbase Premium (US Institutional Flow):** Coinbase price premium over reference/other exchanges $\to$ US institutions are buying (bullish footprint); discount $\to$ selling. Used for bias and SMT validation.
*   **BTC vs. ETH / Sector Leaders:** Classical SMT divergence pair.
*   **BTC.D (Bitcoin Dominance):** Determines capital rotation between BTC and altcoins. Altcoin moves require supportive BTC.D conditions.
*   **Cross-Exchange Same-Asset SMT:** Binance makes a new low while Coinbase holds above previous low $\to$ Institutional rejection (bullish).
*   **CME Gaps (DOL Target):** Weekend CME closures generate price gaps. Historically, ~77% of these gaps are filled $\to$ classic magnet DOL.
*   *⚠️ Validity Warning (from 2026-05 onwards):* Since CME transitioned to 24/7 trading, new weekend gaps are no longer generated. Pre-existing unfilled gaps may still act as magnets, but this indicator is fading and should not be over-weighted.

### 17.5 Timeframe and Session Adjustments (24/7 Adjustments, Enhancing §3.9)
*   **Daily Boundary Definition:** Standardize to UTC 00:00 as the daily close boundary (aligning with CoinGlass and major exchange standards), affecting PDH/PDL and Power of Three calculations.
*   **Session/Killzones Remain Valid:** TradFi participant activity ensures London and NY opens remain high-volatility periods. Tokyo consolidation $\to$ London/NY open sweep of Asia highs/lows (Judas Swing) is one of the most common crypto price cycles.
*   **Weekend Illiquidity:** High probability of exchange-specific spikes / fake sweeps $\to$ lower weekends' weights or require multi-exchange confirmations.
*   **Recommended Timeframe Combinations:** HTF: 1W/1D/4H (bias) $\to$ MTF: 1H/15m (POI) $\to$ LTF: 5m/1m (entry).

### 17.6 Volatility-Adaptive Parameters
Crypto volatility far exceeds equity markets $\to$ standard parameters for `swing_length`, `range_percent`, and stop-loss sizes must be normalized via ATR:
$$\text{range\_percent\_dyn} = k \times \text{ATR\%} \quad \text{(instead of a fixed 1\% rule)}$$
$$\text{stop} = \text{entry} \pm m \times \text{ATR} \quad \text{($m$ classified by asset volatility limits)}$$
*   *Asset Classification:* Tighten parameters for major assets (BTC/ETH); expand parameter tolerances and lower leverage/position sizing for mid-to-small cap altcoins (which suffer from more severe slippage and wick spikes).

### 17.7 Altcoin Trading Principles (BTC as the Anchor)
*   Only trade altcoins when their direction aligns with BTC's HTF structure (BTC acts as the macro HTF bias for the entire market).
*   During BTC sweeps/reversals, high-beta altcoins show amplified volatility $\to$ select strong altcoins that align with BTC's direction.
*   Evaluate sector rotations via BTC.D: declining BTC.D often marks the onset of altcoin season.

### 17.8 Crypto Risk Management (Enhancing §6)
*   Liquidation distance $\ge$ stop-loss distance $\times$ 2 (ensuring stop-losses execute before liquidation is triggered).
*   *Leverage limits:* Cap leverage at $\le$ 3–5x for major assets, and lower for altcoins. Use isolated margin to segregate trade risk.
*   *Funding rate cost:* Ingest funding costs if holding positions across settlement periods. Avoid entering trades during high-volatility funding rate settlements.
*   *Slippage/Counterparty risk:* Use limit orders or multi-batch entries for altcoins; diversify capital across exchanges to manage counterparty risks.

### 17.9 Crypto Data Infrastructure (Enhancing §7 Data Layer)

| Category | Tools / Data Sources |
| :--- | :--- |
| **OHLCV / Execution** | ccxt (Binance/OKX/Bybit), real-time websockets |
| **Derivatives (Liquidations/OI/Funding)** | CoinGlass API, Coinalyze, Coinank |
| **On-Chain Flows (Exchange Net Inflow, Whales)** | Glassnode, CryptoQuant |
| **Cross-Exchange Premium** | Coinbase/Kraken price deviations against index reference rates |
| **Multi-Exchange Ingestion** | Aggregate OHLCV across multiple venues to filter out single-exchange wick anomalies |

### 17.10 Crypto-Specific Confluence Factors (Adding to §5.2)

| Crypto Confluence Factor | Recommended Weight |
| :--- | :--- |
| Sweep hits a dense liquidation cluster | +2 |
| Sweep accompanied by an OI drop (squeeze confirmed) | +2 |
| CVD divergence confirmed | +2 |
| Extreme funding rate (crowded contrarian positions) | +1 |
| Coinbase premium aligns with trade direction | +1 |
| Altcoin trade aligns with BTC HTF bias | +2 |
| Hits an unfilled CME gap (if applicable) | +1 |

### 17.11 Crypto Implementation Sequence
1.  Connect to ccxt BTC/ETH multi-timeframe OHLCV data $\to$ validate §3 core indicator engine (cleanest data profile).
2.  Overlay liquidation heatmaps + OI + funding + CVD (§17.2–17.3) to make liquidity visible.
3.  Add Coinbase premium / BTC.D / SMT comparisons (§17.4).
4.  Implement ATR-adaptive parameters + crypto risk controls (§17.6, 17.8).
5.  Execute multi-pair batch backtesting (simulating funding costs and liquidations) $\to$ Forward test on demo accounts ($\ge$ 1 month).
6.  (Next Stage) Expand engine to support US and Taiwan stocks, handling gaps and daily price limit exceptions.

---

## 18. Appendix D: Trade Backtesting and Win/Loss Learning Extraction System (Closed-Loop Learning)

*   **Purpose:** Establish a closed-loop system — where every executed trade (backtest, paper, and live) is logged as a supervised learning sample ("features $\to$ outcome"). The system periodically analyzes "which conditions generated wins and which caused losses," feeding findings back to calibrate §5.2 confluence weights, parameters, and model/regime triggers. All modifications are validated via strict out-of-sample testing. This replaces subjective trading feedback loops with data-driven empirical calibration, aligning with the team's principle of "zero emotion, lead with data."

### 18.1 Design Principles
*   Every trade is recorded as a supervised sample: $\text{Feature Vector } X + \text{Outcome Label } Y$.
*   Backtesting, paper trading, and live execution share the identical data structure, enabling cross-referencing and replication.
*   Learned optimizations must pass out-of-sample validation before production deployment (preventing data snooping).
*   Interpretability is prioritized (white-box logic): focus on expected values and factor lift; ML is used for cross-validation rather than black-box execution.

### 18.2 Trade Record Schema
Every trade is recorded in `backtest/trades.parquet` (or SQLite) with the following versioned schema:
*   *Identifiers:* `trade_id`, entry/exit timestamps, market, symbol, direction, entry timeframe.
*   *Entry Context (Features $X$, must be known at the moment of entry):*
    *   **Entry Model:** Sweep+CHoCH / OB Continuation / OTE / PowerOfThree / SilverBullet / Unicorn / SMT.
    *   **Confluence Score & Boolean Factors:** HTF bias, discount/premium zone, unmitigated OB, unfilled FVG, sweep active, CHoCH active, OTE zone, killzone active, displacement active.
    *   **Crypto Factors (§17):** Liquidation cluster hit, OI rate of change, funding rate, CVD divergence, Coinbase premium, BTC.D regime.
    *   **Market Regime:** trending/ranging, ATR volatility level, BTC direction (when trading alts).
    *   **Structural Context:** distance to DOL target, stop-loss distance (ATR multiples), planned RR.
*   *Execution:* entry price, stop-loss, TP1/TP2, position size, leverage (crypto).
*   *Outcomes (Labels $Y$):* exit trigger (TP/SL/structural invalidation/time limit/manual), R-multiple, win/loss flag, return %, MAE (Maximum Adverse Excursion), MFE (Maximum Favorable Excursion), holding duration, DOL hit status.

### 18.3 Win/Loss Attribution (Edge Discovery)
*   **Single-Factor expected value / lift slicing:** For each factor, model, or regime, calculate the win rate, average R, Profit Factor, and sample size when the factor is present vs. absent. Calculate:
$$\text{Lift} = \frac{\text{Expected R with Factor}}{\text{Overall Expected R}}$$
    *   This isolates positive expectancy factors and drags.
*   **Clustering Analysis:** Group trades by market, timeframe, regime, and model to identify "which combination performs best under which market conditions."
*   **MAE / MFE Analysis (High ROI):**
    *   *Optimize stop-losses using winner MAE distributions:* If $\ge$ 30% of winning trades experience an MAE that extends beyond the current stop-loss range, it indicates the stop-loss is set within market noise. Widening the stop-loss (historically shown to improve expectancy from +0.33R to +0.42R, a ~20–30% performance lift) is recommended.
    *   *Optimize profit-taking using winner MFE distributions:* Evaluates how much profit is given back before hitting TP.
*   **R-Multiple Distribution / Expectancy:** expected R = (win rate $\times$ average gain) - (loss rate $\times$ average loss). Plot R-multiple distribution histograms.
*   **Temporal Stability (Edge Decay):** Audit edge metrics monthly or quarterly to evaluate decay.

### 18.4 Learning and Calibration
*   **White-Box Calibration (Primary):** Re-calibrate §5.2 confluence weights using single-factor expectancy and lift metrics (increasing weights of high-lift factors, decreasing or removing negative-expectancy elements). Set regime filters (e.g., limit Silver Bullet trades to NY killzones, altcoin trades to BTC-aligned bias).
*   **Multi-Factor Modeling (Auxiliary Validation):** Train a regularized Logistic Regression or Gradient Boosted tree (XGBoost) to predict $P(\text{win})$. Evaluate feature importance and SHAP values, cross-referencing with single-factor analysis. Restrict features relative to sample size, applying strict regularization.
*   **Position Sizing Calibration:** Calibrate dynamic Kelly Criterion sizing using actual win rates and odds (collaborating with the academic authority role), applying conservative fractional Kelly scaling (e.g., 1/4 or 1/2 Kelly).
*   *Output:* Generate a proposed `strategy.yaml` (weights, thresholds, regime rules, Kelly limits) accompanied by a calibration change log.

### 18.5 Closed-Loop Backtest $\leftrightarrow$ Calibration Process
1.  **Backtest Engine (M5):** Execute historical runs $\to$ write trade logs to `trades.parquet` (features + outcomes).
2.  **Attribution (18.3):** Analyze positive/negative expectancy factors and optimal regimes.
3.  **Calibrate (18.4):** Generate proposed `strategy.yaml`.
4.  **Out-of-Sample Validation (18.6):** Rerun backtest on unseen historical data, verifying the edge persists.
5.  **Pass:** Proceed to paper trading (§10.5) $\to$ launch. **Fail:** Re-evaluate from Step 2.
6.  **Continuous Logging:** Record all new paper and live trades back to `trades.parquet`, running Step 1-5 monthly.

### 18.6 Overfitting Prevention & Validation Rigor (Mandatory, supplementing §10.2)
*   **Train/Test Splitting:** Restrict parameter learning to in-sample data. Validate parameters exclusively on out-of-sample data.
*   **Time-Series Cross-Validation:** Apply Purged Cross-Validation / Combinatorial Purged CV (López de Prado) instead of standard k-fold splitting to prevent data leakage from overlapping target labels. Use walk-forward testing as the final replication test.
*   **Overfitting Quantification:** Report Backtest Overfitting Probability (PBO) and Deflated Sharpe Ratio to ensure parameters are not fitted to noise.
*   **Minimum Sample Sizes:** Only adopt slices containing $\ge$ 30–50 samples. Apply multiple-comparison corrections (e.g., Bonferroni) based on the number of tested parameters.
*   **Lookahead/Repainting:** Ensure features contain only information known at the exact moment of entry (re-confirming §12.2).
*   **Edge Decay Monitoring:** Monitor live performance deviations against backtest expectations; disable the strategy for review if significant underperformance is flagged.

### 18.7 Integration with Existing Systems and Agent Roles
*   Interfaces with `risk/rule_engine.py` (§10.5) and the `academic_authority` role (Kelly sizing, behavioral bias audits, CAPE/factor adjustments).
*   Incorporate the monthly "Strategy Health Report" into existing daily/monthly reports, highlighting edge rankings, decay warnings, weight adjustments, and MAE/MFE stop-loss guidelines.
*   *Crypto-specific attribution:* Segment attribution by crypto metrics: "Liquidation cluster hit vs. miss," "OI drop vs. rise," "CVD divergence vs. convergence" to empirically validate §17 enhancements.

### 18.8 Module and WBS Details (Adding M5L)
*   *Files in `learning/`:*
    *   `trade_store.py`: Schema and read/write utilities for `trades.parquet` (parquet/SQLite).
    *   `attribution.py`: Single factor expected values / clustering / MAE-MFE / R-distribution analysis.
    *   `feature_importance.py`: Logistic Regression / XGBoost + SHAP analysis (validation).
    *   `calibration.py`: Parameter weight / threshold / regime / Kelly calibration $\to$ outputting proposed `strategy.yaml`.
    *   `regime.py`: Market regime classification (trends, ranges, volatility, BTC.D).
    *   `cross_val.py`: Time-series CV utilities (Purged CV, Combinatorial Purged CV, PBO, Deflated Sharpe).
    *   `decay_monitor.py`: Edge monitoring and decay detection.
*   *Tasks:*
    *   **L.1 Trade Record Schema:** Standardize log format across backtest/paper/live runs to `trades.parquet`.
    *   **L.2 Attribution Analysis:** Run single factor lift, cluster analysis, MAE/MFE, and R-distribution reports.
    *   **L.3 Calibrator:** Propose updates to `strategy.yaml` (weights/thresholds/regimes/Kelly caps).
    *   **L.4 Out-of-Sample Validation:** Purged/Combinatorial Purged CV + walk-forward validation + PBO/Deflated Sharpe checks.
    *   **L.5 Feature Importance:** SHAP analysis for feature pruning (number of features $\ll$ sample size).
    *   **L.6 Monthly strategist reports + decay monitor.**
*   *DoD:* Automatically generates "positive/negative expectancy factor rankings + MAE/MFE stop-loss recommendations + candidate weights" from `trades.parquet`; updates to `strategy.yaml` are permitted only after passing out-of-sample validation (low PBO, positive out-of-sample expectancy).
