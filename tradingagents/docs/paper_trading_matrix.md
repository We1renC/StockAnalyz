# SMC Quant Trading System: Paper Trading Reference Matrix (§16 / Appendix B)

This document provides a comparative matrix of national (domestic) and international paper trading/simulation environments, outlining their features, rules, and integration feasibility with our SMC automated execution agent.

---

## Paper Trading Platforms Comparison

| Platform | Market Coverage | API Integration | Matching Fidelity | Key Strengths / Rules | Integration Limitations |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Binance Spot Testnet** | Cryptocurrencies | High (REST/WS via CCXT) | Medium | - Real-time quotes<br>- No capital risk<br>- Strict endpoint rate limits | - Liquidity depth is thin<br>- Fills might drift from mainnet orderbook |
| **FTMO / Prop Firm Demo** | FX, Crypto, US Stocks | Medium (MetaTrader 4/5) | High | - Imposes strict daily loss (-5%) & max loss (-10%) rules<br>- Standard drawdown calculations | - Requires Bridge software (e.g. MT5-Python) for automated API routing |
| **TradingView Simulation** | Global Stocks, FX, Crypto | None (Manual/Webhooks) | High | - Excellent visual mapping of SMC chart drawings<br>- Easy alerting | - Lacks direct API order routing back from Python scripts |
| **Custom Local Paper Trader** | Market-Agnostic | High (In-Process Python calls) | Customizable | - Full control over slippage simulation, commissions, and funding fees<br>- Immediate logging to `trades.parquet` | - Requires active market data feeds proxying real-time books |
| **Fugle / KGI Simulation (TW)** | Taiwan Stocks | Low | Medium | - Matches TW stock session limits and tick rules | - Rate limits on test APIs; single session limits (09:00 - 13:30) |

---

## Deployment Recommendation for SMC System

To ensure safety and rigor before live capital allocation, the SMC system validation pipeline follows these integration checkpoints:

1. **Local Paper Trader (Dry-Run)**:
   - Run the system live in memory, matching trades against raw incoming WS price feeds.
   - Simulate worst-case slippage (e.g., 2 ticks on crypto, 0.5% on altcoins).
   - Log execution features and parameters immediately to `trades.parquet`.
2. **Prop Firm Simulation / Testnet**:
   - Route authenticated orders to Binance Testnet or a simulated MetaTrader bridge.
   - Enforce the **Rule Enforcement Layer** (`rule_engine.py`) to simulate account lockouts when drawdown hits thresholds.
   - Run in this stage for **at least 30 days and 50 trades** with an expectancy R of $\ge +1$ before transitioning to Small-Capital Live.
