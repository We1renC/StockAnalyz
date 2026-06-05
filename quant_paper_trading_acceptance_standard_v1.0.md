# Quantitative Trading Paper Trading Acceptance Standard: A Complete Checklist from Strategy Testing to Small-Scale Live Trading

## 1. Purpose of This Document

In quantitative trading, paper trading, exchange testnets, and forward testing are not formalities. They are risk-filtering mechanisms before a strategy is allowed to enter real-money trading.

Many strategies perform well in backtests or paper trading but fail immediately after going live. The failure is rarely caused by one single factor. It is usually the result of several issues combined:

* Backtest data is too idealized.
* The execution model is too optimistic.
* Slippage and transaction fees are underestimated.
* Limit order fill rates are overestimated.
* API latency, rate limits, and errors are not properly modeled.
* Unknown order states are handled incorrectly.
* The live exchange account cannot be reconciled with the local ledger.
* There is no effective kill switch.
* Risk control has lower priority than strategy logic.
* Parameters are repeatedly adjusted during paper trading, creating secondary overfitting.
* The strategy works with small capital but deteriorates after capital is scaled due to slippage and market impact.

Therefore, the purpose of paper trading is not to prove that a strategy will definitely make money. Its purpose is to verify whether the strategy can still operate under conditions that are closer to the real market.

A proper paper trading process should verify whether the strategy has the following capabilities:

1. The strategy logic is reproducible.
2. The execution assumptions are conservative enough.
3. Transaction costs are fully included.
4. The system can run stably.
5. The system does not lose control under abnormal conditions.
6. Risk controls can actually prevent risk expansion.
7. Logging, alerting, reconciliation, and shutdown mechanisms are complete.
8. Paper trading results can serve as a benchmark for small-scale live trading.

In one sentence:

> Backtesting validates the logic.
> Testnet validates the system.
> Paper trading validates the assumptions.
> Small-scale live trading validates execution.
> Full capital deployment validates capacity and risk control.

---

# 2. Basic Principles of Paper Trading

## 2.1 Paper Trading Is Not a Profit Demonstration; It Is a Risk Acceptance Process

The passing standard for paper trading is not “the strategy made money in the short term.”

The real standard should be:

> After including realistic transaction costs, slippage, latency, limit order uncertainty, API limits, data errors, and risk controls, the strategy can still run stably without exposing unacceptable systemic risk.

A strategy that makes money in paper trading should still fail the acceptance test if any of the following issues exist:

* The execution model is too idealized.
* Full transaction fees are not deducted.
* Slippage is not included.
* Partial fills are not handled.
* There is no reconciliation mechanism.
* There is no kill switch.
* Logs cannot replay the trading process.
* Parameters are repeatedly changed during paper trading.
* Profit depends on a small number of extreme market events.
* The sample size is insufficient.

## 2.2 Paper Trading Should Be as Close to the Live Trading Architecture as Possible

Paper trading should not be a simplified system that is completely different from the live trading system.

Ideally, paper trading and live trading should share the following modules:

* Market data ingestion module;
* Signal calculation module;
* Risk control module;
* Order generation module;
* Logging module;
* Reconciliation module;
* Monitoring and alerting module;
* Shutdown module.

The only difference should be:

> In paper trading, the final order is not actually sent to the exchange. Instead, it is sent to a local execution simulator or shadow trading module.

If the paper trading codebase is significantly different from the live trading codebase, the value of passing paper trading is greatly reduced.

---

# 3. Pre-Paper-Trading Checks

## 3.1 Strategy Logic Check

Before entering paper trading, the strategy must have clear, reproducible, and auditable trading logic.

### Checklist

* [ ] Entry conditions are clearly defined.
* [ ] Exit conditions are clearly defined.
* [ ] Stop-loss conditions are clearly defined.
* [ ] Take-profit conditions are clearly defined.
* [ ] Position scaling conditions are clearly defined.
* [ ] Position reduction conditions are clearly defined.
* [ ] No-trade conditions are clearly defined, such as low liquidity, high volatility, missing data, or excessive spreads.
* [ ] All strategy parameters are fixed.
* [ ] The strategy does not use future data.
* [ ] The strategy does not treat an unfinished candle as a confirmed signal unless it is explicitly designed as a tick-level or real-time strategy.
* [ ] Backtests include transaction fees.
* [ ] Backtests include basic slippage assumptions.
* [ ] Out-of-sample testing has been completed.
* [ ] Performance is not supported by only a small number of trades.
* [ ] Strategy logic can be fully described in writing and does not depend on subjective judgment.

### Passing Standard

The strategy must be clearly describable, reproducible, and verifiable.
If the strategy logic cannot be explicitly written down, it should not enter paper trading.

---

## 3.2 Trading Instrument Check

Before paper trading, the selected instruments must be checked to ensure they are suitable for the strategy.

### Checklist

* [ ] The trading pair has sufficient trading volume.
* [ ] The bid-ask spread is within an acceptable range.
* [ ] Order book depth is sufficient for the expected order size.
* [ ] The trading pair does not frequently exhibit price spikes, data interruptions, or abnormal prints.
* [ ] The trading pair does not suffer from prolonged liquidity dry-ups.
* [ ] For small-cap coins or illiquid instruments, higher slippage and market impact assumptions are used.
* [ ] For derivatives trading, funding rates, leverage limits, and liquidation rules have been checked.
* [ ] For cross-market or arbitrage strategies, liquidity and transfer constraints on both sides have been checked.

### Passing Standard

The expected order size must not significantly exceed the market’s available liquidity.
If the average expected profit is smaller than the average transaction cost, the strategy should not enter paper trading.

---

## 3.3 Data Source Check

Data quality directly determines whether paper trading results are credible.

### Checklist

* [ ] Market data sources are clearly defined.
* [ ] Real-time exchange market data is used instead of delayed data.
* [ ] Candlestick data, trade data, and order book data come from consistent sources.
* [ ] All market data has timestamps.
* [ ] The system can distinguish between market timestamp, data receipt time, signal generation time, and order generation time.
* [ ] The system can handle data latency.
* [ ] The system can handle missing data.
* [ ] The system can handle duplicate data.
* [ ] The system can handle out-of-order data.
* [ ] WebSocket disconnections can be automatically recovered.
* [ ] Missing data can be backfilled after reconnection.
* [ ] The local server time is synchronized with the exchange time.

### Passing Standard

Paper trading must be able to replay the data used for every signal.
If timestamps are inconsistent or data cannot be traced, the performance statistics are not reliable.

---

# 4. Execution Model Checks

## 4.1 Market Order Execution Model

A market order should not be assumed to execute at the latest traded price.

### Checklist

* [ ] Buy market orders are estimated using ask-side order book depth.
* [ ] Sell market orders are estimated using bid-side order book depth.
* [ ] Large orders consume multiple levels of the order book.
* [ ] Execution price is calculated as a volume-weighted average price, not just the best bid or ask.
* [ ] If order book depth is insufficient, the order may be rejected or partially filled.
* [ ] Slippage assumptions increase when market volatility rises.
* [ ] Market impact assumptions increase for small-cap or illiquid trading pairs.
* [ ] Each market order records the signal price, theoretical execution price, simulated execution price, and slippage.

### Passing Standard

Market order simulation must be more conservative than “executing at the latest traded price.”
If the strategy fails after realistic order book costs are included, it should not enter small-scale live trading.

---

## 4.2 Limit Order Execution Model

Limit orders are one of the most common sources of illusion in paper trading.

A price touching the limit price does not mean your order is filled.

### Checklist

* [ ] Limit orders are not filled solely because the price touched the limit price.
* [ ] Queue position is considered.
* [ ] Actual traded volume at that price level is considered.
* [ ] Partial fills are supported.
* [ ] Unfilled orders are supported.
* [ ] Order timeout and cancellation are supported.
* [ ] Cancellation failure is supported.
* [ ] Post-only rejection is supported.
* [ ] Limit order fill rate is measured.
* [ ] Average waiting time before fill is measured.
* [ ] Opportunity cost after missed fills is measured.
* [ ] Price movement after fill is measured to detect adverse selection.

### Passing Standard

A limit order strategy must prove that its profitability does not rely on overly optimistic fill assumptions.
If the strategy only makes money when using “touch price equals filled,” it should not enter live trading.

---

## 4.3 Slippage and Market Impact Check

Slippage should not be handled only with a fixed percentage. It should be estimated dynamically based on market conditions whenever possible.

### Checklist

* [ ] Each trade records the signal price.
* [ ] Each trade records the theoretical execution price.
* [ ] Each trade records the simulated execution price.
* [ ] Each trade records slippage.
* [ ] Slippage is adjusted based on bid-ask spread.
* [ ] Slippage is adjusted based on order book depth.
* [ ] Slippage is adjusted based on order size.
* [ ] Slippage is adjusted based on market volatility.
* [ ] Slippage is adjusted based on instrument liquidity.
* [ ] Reports show average slippage, maximum slippage, and slippage standard deviation.
* [ ] Reports show how much total profit is eroded by slippage.

### Passing Standard

After slippage is deducted, the strategy should still have a reasonable expected value.
If slippage turns the strategy from positive expectancy to negative expectancy, it should not enter live trading.

---

# 5. Transaction Cost Checks

## 5.1 Fee Check

Transaction fees are one of the most commonly underestimated costs in quantitative trading, especially for high-frequency strategies.

### Checklist

* [ ] Maker fees and taker fees are distinguished.
* [ ] Fees are configured according to the actual exchange fee schedule.
* [ ] VIP tier differences are considered.
* [ ] Fee discounts from exchange tokens are considered if applicable.
* [ ] Fee differences across trading pairs are considered.
* [ ] For derivatives, both opening and closing fees are included.
* [ ] Every trade records its fee.
* [ ] Reports show both gross profit and net profit.
* [ ] Strategy performance is evaluated based on net equity after fees.

### Passing Standard

A strategy must not be judged using performance that excludes transaction fees.
If fees consume most of the strategy’s profit, the strategy should not enter small-scale live trading.

---

## 5.2 Additional Cost Checks for Derivatives Trading

If the strategy trades perpetual futures or other derivatives, margin, leverage, and funding must be handled separately.

### Checklist

* [ ] Funding rates are included.
* [ ] Leverage is included.
* [ ] Margin usage is included.
* [ ] Maintenance margin requirements are included.
* [ ] Liquidation price is calculated.
* [ ] The difference between mark price and last traded price is included.
* [ ] Funding payments are reflected in equity.
* [ ] One-way mode and hedge mode are distinguished.
* [ ] Extreme market scenarios are tested for liquidation risk.
* [ ] Position reduction or stop-loss mechanisms before liquidation are designed.

### Passing Standard

A derivatives strategy cannot be evaluated only by whether the directional call is correct.
Leverage, funding, margin, and liquidation risk must also be controlled.

---

# 6. Order Lifecycle Checks

## 6.1 Order State Check

Every order should have a complete lifecycle.

### Checklist

* [ ] Every order has a unique order ID.
* [ ] Every order has a unique client order ID.
* [ ] Every order records the strategy version.
* [ ] Every order records the parameter version.
* [ ] Every order records the signal source.
* [ ] Every order records trading pair, side, price, quantity, and order type.
* [ ] `new` state is supported.
* [ ] `partially filled` state is supported.
* [ ] `filled` state is supported.
* [ ] `canceled` state is supported.
* [ ] `rejected` state is supported.
* [ ] `expired` state is supported.
* [ ] Cancellation failure is supported.
* [ ] Order placement failure is supported.
* [ ] Partial fills are supported.
* [ ] Unknown order states are supported.

### Passing Standard

Every order must be traceable, replayable, and auditable.
Any order with an unknown state must enter a risk-handling process.

---

## 6.2 Unknown Order State Check

One of the most dangerous real-world situations is sending an order without receiving a clear response.

### Checklist

* [ ] The system can simulate order placement timeout.
* [ ] The system can simulate no confirmation response from the exchange.
* [ ] The system does not blindly resend orders under unknown order states.
* [ ] The system uses client order ID to query order state.
* [ ] If the order cannot be found, the system does not immediately assume failure.
* [ ] If an order was filled but the local state was not updated, reconciliation can correct it.
* [ ] If an order was canceled but the local state still shows it as active, reconciliation can correct it.
* [ ] Unknown order states trigger trading suspension.
* [ ] Unknown order states trigger reconciliation.
* [ ] Unknown order states are written to error logs and alert notifications are sent.

### Passing Standard

When order state is unknown, the first principle is to stop increasing risk, not to continue trading.

---

# 7. Virtual Account and Reconciliation Checks

## 7.1 Virtual Account Check

A paper trading account should resemble a real exchange account as closely as possible. It should not merely calculate buy-and-sell profit.

### Checklist

* [ ] Initial capital is clearly defined.
* [ ] Multi-currency balances are supported.
* [ ] Available balance is supported.
* [ ] Frozen balance is supported.
* [ ] Unrealized PnL is supported.
* [ ] Realized PnL is supported.
* [ ] Fee deduction is supported.
* [ ] Funding fee deduction is supported.
* [ ] Position market value calculation is supported.
* [ ] Equity curve calculation is supported.
* [ ] Leverage and margin calculation are supported.
* [ ] Orders are rejected when balance is insufficient.
* [ ] Exchange minimum notional requirements are enforced.
* [ ] Exchange minimum order quantity requirements are enforced.
* [ ] Exchange price precision and quantity precision requirements are enforced.

### Passing Standard

The system must not produce negative balances, oversized orders, or fills that violate exchange precision rules.

---

## 7.2 Reconciliation Check

Reconciliation must be completed during paper trading. It should not be added only after entering live trading.

### Checklist

* [ ] Local order states are periodically compared.
* [ ] Local position states are periodically compared.
* [ ] Local balance states are periodically compared.
* [ ] Trade records are periodically compared.
* [ ] Reconciliation frequency is clearly defined, such as once every 1 to 5 minutes.
* [ ] Differences are marked as abnormalities.
* [ ] Major differences trigger trading suspension.
* [ ] Reconciliation results are fully logged.
* [ ] Reconciliation abnormalities trigger alerts.
* [ ] Correct state can be restored after reconciliation.

### Passing Standard

Any difference in balance, position, open orders, or trade records must be explainable, traceable, and correctable.

---

# 8. API and System Performance Checks

## 8.1 API Rate Limit Check

API rate limits are not minor engineering details. They are part of the trading system.

### Checklist

* [ ] The system has global API request weight management.
* [ ] The system has order count management.
* [ ] When multiple strategies share the same API infrastructure, request usage is centrally controlled.
* [ ] When rate limit errors occur, the system automatically slows down.
* [ ] When rate limit errors occur, the system does not retry indefinitely.
* [ ] Retry logic uses backoff.
* [ ] Market data uses WebSocket where appropriate instead of high-frequency REST polling.
* [ ] Trading, order queries, and account queries have request priority rules.
* [ ] The system records API latency.
* [ ] The system records API error rate.
* [ ] API abnormalities can pause the strategy.

### Passing Standard

Paper trading must not produce excessive API errors, infinite retries, or uncontrolled requests.
If API management is unstable, the strategy should not enter live trading.

---

## 8.2 Latency Check

Strategy performance may depend heavily on execution speed, so latency must be quantified.

### Checklist

* [ ] Market data arrival latency is recorded.
* [ ] Signal computation time is recorded.
* [ ] Order request latency is recorded.
* [ ] Exchange response latency is recorded.
* [ ] Database write latency is recorded.
* [ ] Strategy loop runtime is recorded.
* [ ] Average latency is recorded.
* [ ] Maximum latency is recorded.
* [ ] P95 latency is recorded.
* [ ] P99 latency is recorded.
* [ ] Latency abnormalities trigger alerts.
* [ ] If latency exceeds the strategy’s tolerance, trading is paused.

### Passing Standard

The strategy must know where it is slow.
If the strategy depends on short-term price movements, it should not enter live trading until latency testing passes.

---

## 8.3 System Stability Check

Paper trading should verify whether the system can run unattended.

### Checklist

* [ ] The system can run continuously for at least 7 days.
* [ ] Memory usage is stable.
* [ ] CPU usage is stable.
* [ ] Disk space is sufficient.
* [ ] Logs do not grow without limits.
* [ ] Database connections are stable.
* [ ] WebSocket reconnection works correctly.
* [ ] The system can recover state after a restart.
* [ ] A strategy process crash does not create incorrect positions.
* [ ] Scheduled tasks execute correctly.
* [ ] System time synchronization works correctly.

### Passing Standard

Seven days of stable operation only proves system stability. It does not prove strategy validity.
If the system requires frequent manual fixes, it should not enter live trading.

---

# 9. Abnormal Scenario Testing

## 9.1 Network Abnormality Check

### Checklist

* [ ] WebSocket disconnection is simulated.
* [ ] REST API timeout is simulated.
* [ ] DNS resolution failure is simulated.
* [ ] Temporary network outage is simulated.
* [ ] Sudden latency increase is simulated.
* [ ] Exchange error responses are simulated.
* [ ] Missing data is simulated.
* [ ] Duplicate data is simulated.
* [ ] Out-of-order data is simulated.
* [ ] The system can automatically reconnect.
* [ ] The system can backfill missing data.
* [ ] The system can pause trading while waiting for recovery.
* [ ] The system can reconcile after recovery.

### Passing Standard

Network abnormalities must not cause duplicate orders, incorrect positions, or uncontrolled trading behavior.

---

## 9.2 Market Abnormality Check

### Checklist

* [ ] Sudden large price jumps are simulated.
* [ ] Sudden widening of bid-ask spread is simulated.
* [ ] Sudden disappearance of order book depth is simulated.
* [ ] Sudden volume collapse is simulated.
* [ ] Extreme one-way markets are simulated.
* [ ] Long sideways markets are simulated.
* [ ] Price spike events are simulated.
* [ ] Consecutive stop-loss events are simulated.
* [ ] Failure to get filled is simulated.
* [ ] Slippage exceeding expectations is simulated.
* [ ] The system can reduce position size during abnormal volatility.
* [ ] The system can stop placing orders when liquidity is insufficient.

### Passing Standard

The strategy may lose money under abnormal market conditions, but it must not lose control.
Risk control must have priority over strategy profitability.

---

## 9.3 Program Abnormality Check

### Checklist

* [ ] Strategy process crash is simulated.
* [ ] Database write failure is simulated.
* [ ] Configuration file read failure is simulated.
* [ ] Out-of-memory condition is simulated.
* [ ] Disk space exhaustion is simulated.
* [ ] Duplicate strategy startup is simulated.
* [ ] Incorrect parameters are simulated.
* [ ] Delisted or non-tradable trading pairs are simulated.
* [ ] Exchange precision rule violations are simulated.
* [ ] Insufficient balance is simulated.
* [ ] The system can stop safely.
* [ ] The system can preserve error logs.
* [ ] The system can avoid continuing trading in an invalid state.

### Passing Standard

No program abnormality should directly turn into unlimited trading risk.

---

# 10. Risk Control Checks

## 10.1 Risk Control Priority

The risk control module must have higher authority than the strategy module.

A strategy may generate trade requests, but whether an order is actually allowed must be decided by the risk control layer.

### Checklist

* [ ] Strategy signals cannot bypass risk control.
* [ ] Every order must pass risk checks before being submitted.
* [ ] If risk control rejects an order, the strategy cannot resubmit it on its own.
* [ ] If risk control stops the system, the strategy cannot restart itself.
* [ ] Risk parameters have version control.
* [ ] Risk events are fully logged.
* [ ] Risk events trigger alerts.

### Passing Standard

The risk control layer must have higher priority than the strategy layer.
Any architecture that allows strategies to bypass risk control is unacceptable.

---

## 10.2 Position Risk Check

### Checklist

* [ ] Maximum order size is defined.
* [ ] Maximum position per trading pair is defined.
* [ ] Total position limit is defined.
* [ ] Maximum leverage is defined.
* [ ] Maximum margin usage is defined.
* [ ] Maximum number of open orders is defined.
* [ ] Maximum number of scaling-in operations is defined.
* [ ] Maximum directional exposure is defined.
* [ ] Aggregate exposure limit for correlated instruments is defined.
* [ ] New orders are rejected when position limits are exceeded.
* [ ] Abnormal positions trigger alerts.

### Passing Standard

No strategy signal may bypass position risk controls.

---

## 10.3 Loss Risk Check

### Checklist

* [ ] Maximum loss per trade is defined.
* [ ] Maximum daily loss is defined.
* [ ] Maximum weekly loss is defined.
* [ ] Maximum total drawdown is defined.
* [ ] Maximum consecutive loss count is defined.
* [ ] Maximum consecutive erroneous trade count is defined.
* [ ] Strategy shutdown conditions are defined.
* [ ] When the loss limit is reached, new trades are automatically stopped.
* [ ] When the loss limit is reached, the system can either flatten positions or only stop new entries.
* [ ] All shutdown events are recorded.

### Passing Standard

Loss controls must be actually triggered and tested during paper trading, not only documented.

---

## 10.4 Kill Switch Check

A kill switch is a minimum requirement before any real-money trading.

### Checklist

* [ ] Manual one-click shutdown is supported.
* [ ] Automatic shutdown is supported.
* [ ] After shutdown, no new orders are allowed.
* [ ] After shutdown, all open orders can be canceled.
* [ ] After shutdown, existing positions can be retained.
* [ ] After shutdown, positions can be immediately closed if required.
* [ ] Shutdown events are written to logs.
* [ ] Shutdown events trigger alerts.
* [ ] Manual confirmation is required before restarting after shutdown.
* [ ] Shutdown procedures have been tested successfully during paper trading.

### Passing Standard

A strategy without a kill switch must not enter real-money trading.

---

# 11. Monitoring and Alerting Checks

## 11.1 Monitoring Dashboard Check

### Checklist

* [ ] Real-time account equity is displayed.
* [ ] Real-time available balance is displayed.
* [ ] Current positions are displayed.
* [ ] Open orders are displayed.
* [ ] Daily PnL is displayed.
* [ ] Total PnL is displayed.
* [ ] Maximum drawdown is displayed.
* [ ] Strategy status is displayed.
* [ ] Data connection status is displayed.
* [ ] API error rate is displayed.
* [ ] Recent trade records are displayed.
* [ ] Risk control status is displayed.

### Passing Standard

The operator must be able to quickly determine whether the system is healthy.
If system status can only be understood by reading raw logs, monitoring is not sufficient.

---

## 11.2 Alerting Mechanism Check

### Checklist

* [ ] Notification is sent when the strategy starts.
* [ ] Notification is sent when the strategy stops.
* [ ] Notification is sent when an order fails.
* [ ] Notification is sent when API error rate is too high.
* [ ] Notification is sent when WebSocket disconnects.
* [ ] Notification is sent when reconciliation abnormalities occur.
* [ ] Notification is sent when slippage is excessive.
* [ ] Notification is sent when loss reaches warning levels.
* [ ] Notification is sent when the kill switch is triggered.
* [ ] Notification is sent when the system crashes.
* [ ] Alert messages include error reason, timestamp, strategy name, trading pair, and severity.

### Passing Standard

Major abnormalities must not only be written to logs. They must actively notify the operator.

---

# 12. Performance and Trade Quality Checks

## 12.1 Basic Performance Metrics

Paper trading performance must not be evaluated only by return.

### Checklist

* [ ] Total return.
* [ ] Net profit.
* [ ] Gross profit.
* [ ] Total transaction fees.
* [ ] Total slippage.
* [ ] Maximum drawdown.
* [ ] Win rate.
* [ ] Average win.
* [ ] Average loss.
* [ ] Win-loss ratio.
* [ ] Profit factor.
* [ ] Sharpe ratio.
* [ ] Sortino ratio.
* [ ] Calmar ratio.
* [ ] Number of trades.
* [ ] Average holding time.
* [ ] Maximum consecutive losses.
* [ ] Maximum consecutive wins.

### Passing Standard

A strategy with high return but high drawdown, high slippage, or unstable behavior should not be considered qualified.
If the paper trading period is short, Sharpe, Sortino, and Calmar ratios should be treated as references only, not as sole launch criteria.

---

## 12.2 Trade Quality Metrics

### Checklist

* [ ] Average slippage.
* [ ] Maximum slippage.
* [ ] Slippage standard deviation.
* [ ] Average execution latency.
* [ ] Maximum execution latency.
* [ ] Fill rate.
* [ ] Partial fill ratio.
* [ ] Cancellation ratio.
* [ ] Rejection ratio.
* [ ] Order timeout ratio.
* [ ] Average waiting time for limit orders.
* [ ] Average market impact for market orders.
* [ ] Difference between theoretical price and simulated execution price.
* [ ] Difference between signal price and execution price.
* [ ] Price movement after missed fills.

### Passing Standard

If the strategy’s profitability mainly comes from idealized execution rather than stable trading logic, it should not enter live trading.

---

# 13. Strategy Behavior Deviation Check

Paper trading should not only evaluate profitability. It should also check whether the strategy behavior matches the backtest and research assumptions.

For example, suppose the backtest expected the strategy to trade 20 times per day, with a 45% win rate, 1.8 win-loss ratio, and 30-minute average holding time.

If paper trading produces only 2 trades per day, a 70% win rate, 0.6 win-loss ratio, and 5-hour average holding time, then even if the strategy is profitable in the short term, its behavior has deviated from the original design.

### Checklist

* [ ] Paper trading trade frequency is close to backtest expectations.
* [ ] Paper trading win rate is within a reasonable range.
* [ ] Paper trading win-loss ratio is close to backtest expectations.
* [ ] Paper trading average holding time is close to backtest expectations.
* [ ] Paper trading maximum drawdown is close to stress-test expectations.
* [ ] Paper trading slippage is not materially higher than backtest assumptions.
* [ ] Paper trading fill rate is not materially lower than backtest assumptions.
* [ ] The unfilled order ratio is not abnormal.
* [ ] The strategy does not trade frequently in market regimes where it was not designed to trade.
* [ ] The strategy does not exhibit behaviors that rarely appeared in backtests.
* [ ] The strategy does not rely on a small number of abnormal winning trades.
* [ ] The strategy does not change its risk profile after consecutive losses.

### Passing Standard

Paper trading results do not need to exactly match backtests, but the core trading behavior must not materially deviate from the original research assumptions.
If there is significant deviation, the cause must be explained before moving to small-scale live trading.

---

# 14. Shadow Trading Check

Shadow trading is a mode that is closer to live trading than ordinary paper trading.

The core idea is:

> The strategy generates real trading signals, passes through live risk controls and the order generation process, but the final order is not sent to the exchange. Instead, the system records what would likely have happened if the order had been submitted.

### Checklist

* [ ] The same market data source as live trading is used.
* [ ] The same signal calculation process as live trading is used.
* [ ] The same risk control module as live trading is used.
* [ ] The same order generation module as live trading is used.
* [ ] The same logging and alerting modules as live trading are used.
* [ ] The only difference is that the final order is not sent to the exchange.
* [ ] Theoretical order submission time is recorded.
* [ ] The order book at that time is recorded.
* [ ] The likely execution price is recorded.
* [ ] Subsequent price behavior after the shadow order is recorded.
* [ ] Shadow orders can be compared against later small-scale live orders.

### Passing Standard

Shadow mode should share as much of the live architecture as possible.
If the paper trading system is too different from the live trading system, paper trading results become less credible.

---

# 15. Sample Size and Testing Period Check

Paper trading should not be judged only by the number of days it has run. Sample size, market regimes, and trading frequency must also be considered.

### Checklist

* [ ] Paper trading runs through at least one complete trading cycle.
* [ ] High-frequency strategies accumulate enough trade samples.
* [ ] Medium- and low-frequency strategies cover enough market conditions.
* [ ] The testing period does not only cover a one-way market.
* [ ] The testing period includes volatility expansion and volatility contraction.
* [ ] The testing period includes periods when the strategy does not trade.
* [ ] The testing period includes consecutive loss periods.
* [ ] The testing period includes periods of weaker liquidity.

### Reference Standards

| Strategy Type           | Recommended Paper Trading Requirement                                                             |
| ----------------------- | ------------------------------------------------------------------------------------------------- |
| High-frequency strategy | At least hundreds to thousands of trade samples                                                   |
| Intraday strategy       | At least 4 to 8 weeks, with sufficient trade samples                                              |
| Swing strategy          | At least 2 to 3 months, combined with long-term out-of-sample backtesting                         |
| Low-frequency strategy  | Should not rely only on paper trading; long-term historical samples and stress tests are required |

### Passing Standard

The paper trading sample must be sufficient to expose the strategy’s weaknesses.
If the number of trades is too small, short-term profitability should not be considered sufficient evidence.

---

# 16. Research Discipline and Prevention of Secondary Overfitting

Paper trading should be validation, not continued optimization.

If parameters are changed whenever the strategy performs poorly, and the final profitable version is declared as having “passed” paper trading, this is still overfitting.

### Checklist

* [ ] Strategy logic is frozen before paper trading starts.
* [ ] Strategy parameters are frozen before paper trading starts.
* [ ] Risk control parameters are frozen before paper trading starts.
* [ ] Parameters are not adjusted during paper trading based on short-term performance.
* [ ] If the strategy is modified during paper trading, statistics must restart.
* [ ] All modifications must record their reasons.
* [ ] Failed versions must also be retained.
* [ ] Only the best-performing test results must not be selectively retained.
* [ ] Paper trading results must not be repeatedly used for parameter tuning while still being presented as out-of-sample validation.
* [ ] Strategy version, parameter version, and execution model version must all map to the corresponding test results.

### Passing Standard

Once a strategy is repeatedly adjusted during paper trading, the result should be treated only as research data, not as an acceptance result for deployment.

---

# 17. API Key and Permission Security Check

Even before entering small-scale live trading, API security must be handled properly.

### Checklist

* [ ] API keys only have the necessary permissions.
* [ ] Spot strategies do not enable futures permissions.
* [ ] Withdrawal permission is disabled unless absolutely required.
* [ ] IP whitelist is enabled.
* [ ] API keys are not hardcoded into source code.
* [ ] API keys are stored using environment variables or a secrets management tool.
* [ ] Logs do not output API keys, secrets, or tokens.
* [ ] Different strategies use different API keys where appropriate.
* [ ] Test and live environments use separate keys.
* [ ] API keys can be revoked immediately when a strategy is disabled.
* [ ] There is a revocation and shutdown procedure in case of server compromise.

### Passing Standard

If API key security is not acceptable, the strategy must not enter any real-money environment.
Strategy risk and account security risk must be managed separately.

---

# 18. Capacity and Capital Scaling Rules

A strategy that works with small capital does not necessarily work with larger capital.

Strategy capacity depends on:

* Trading volume;
* Order book depth;
* Execution speed;
* Order size;
* Trading frequency;
* Market impact;
* Whether the strategy reveals trading intent;
* Whether it can be front-run or competed against by other bots.

### Checklist

* [ ] Single order size does not exceed a defined percentage of top-level order book depth.
* [ ] Single order size does not exceed a defined percentage of average volume over the last N minutes.
* [ ] Total strategy position does not exceed market capacity.
* [ ] Slippage is re-estimated after capital is scaled.
* [ ] Fill rate is re-estimated after capital is scaled.
* [ ] Maximum drawdown is re-estimated after capital is scaled.
* [ ] Each capital increase is limited to a predefined multiple of the previous stage.
* [ ] After each capital increase, the strategy is observed again.
* [ ] If slippage, rejection rate, or drawdown deteriorates significantly, scaling is stopped.

### Suggested Capital Stages

| Stage                    | Capital Allocation | Main Purpose                                  |
| ------------------------ | -----------------: | --------------------------------------------- |
| Paper trading            |                 0% | Validate strategy assumptions and system flow |
| Small-scale live trading |              1%–5% | Validate real fills, latency, and slippage    |
| Small live allocation    |            10%–20% | Validate strategy stability                   |
| Medium allocation        |            25%–50% | Validate capacity and market impact           |
| Full allocation          |               100% | Only after all previous stages pass           |

### Passing Standard

Capital must be scaled in stages.
After every scaling step, slippage, fill rate, drawdown, and system stability must be rechecked.

---

# 19. Paper Trading vs. Small-Scale Live Trading Comparison

After paper trading ends, its results should become the benchmark for small-scale live trading.

Small-scale live trading is not a fresh start. It is used to verify:

> Whether the paper trading model can reasonably predict the real execution environment.

### Comparison Table

| Metric                                 | Paper Trading | Small-Scale Live Trading | Deviation | Acceptable? |
| -------------------------------------- | ------------: | -----------------------: | --------: | ----------- |
| Average slippage                       |               |                          |           |             |
| Maximum slippage                       |               |                          |           |             |
| Fill rate                              |               |                          |           |             |
| Partial fill ratio                     |               |                          |           |             |
| Rejection rate                         |               |                          |           |             |
| API latency                            |               |                          |           |             |
| Average holding time                   |               |                          |           |             |
| Trade frequency                        |               |                          |           |             |
| Win rate                               |               |                          |           |             |
| Win-loss ratio                         |               |                          |           |             |
| Maximum drawdown                       |               |                          |           |             |
| Fees as percentage of gross profit     |               |                          |           |             |
| Slippage as percentage of gross profit |               |                          |           |             |

### Passing Standard

If small-scale live trading deviates materially from paper trading, determine whether the issue is strategy degradation or an overly optimistic paper trading model.
Capital must not be scaled before this deviation analysis is completed.

---

# 20. Quantitative Passing and Failing Thresholds

Paper trading should not only list what to check. It must also define clear thresholds.

| Check Item        | Passing Standard                                      | Failing Condition                                      |
| ----------------- | ----------------------------------------------------- | ------------------------------------------------------ |
| System stability  | Runs for at least 7 days without major errors         | Frequent crashes or manual repair required             |
| Order tracking    | Every order can be fully replayed                     | Orders cannot be traced                                |
| Reconciliation    | Differences are explainable and correctable           | Unexplained balance or position differences            |
| Fees              | Fully deducted                                        | Performance only evaluated before costs                |
| Slippage          | Strategy remains positive expectancy after slippage   | Strategy turns negative after slippage                 |
| Limit order fills | Queue, partial fills, and missed fills are considered | Assumes touch price equals filled                      |
| API errors        | Controlled and do not cause trading loss of control   | Infinite retries, duplicate orders, or missed orders   |
| Risk control      | Tested and can prevent risk expansion                 | Risk controls only exist in documentation              |
| Kill switch       | Successfully tested                                   | Manual or automatic shutdown unavailable               |
| Sample size       | Meets strategy type requirement                       | Too few samples but still considered valid             |
| Strategy behavior | Broadly consistent with backtest assumptions          | Material deviation without explanation                 |
| Logging           | Every trade can be fully replayed                     | Decision and execution process cannot be reconstructed |
| Alerting          | Major abnormalities actively notify operator          | Abnormalities only written to logs                     |
| Security          | API permissions minimized                             | High-permission API keys without IP restriction        |
| Capacity          | Slippage and impact after scaling are estimated       | Directly scales from small capital to large capital    |

---

# 21. Conditions That Prohibit Live Trading

If any of the following occur, the strategy should not enter small-scale live trading:

* [ ] Paper trading is profitable, but transaction fees are not included.
* [ ] Paper trading is profitable, but slippage is not included.
* [ ] The execution model is overly idealized.
* [ ] Limit orders assume touch price equals filled.
* [ ] Order states cannot be fully tracked.
* [ ] The system has produced duplicate orders.
* [ ] The system has produced incorrect positions.
* [ ] Balance or position differences cannot be explained.
* [ ] Reconciliation is not implemented.
* [ ] Risk controls have not been tested.
* [ ] The kill switch has not been tested.
* [ ] API errors can cause the strategy to lose control.
* [ ] WebSocket disconnection cannot be recovered.
* [ ] State becomes inconsistent after program restart.
* [ ] The strategy only looks good after repeated parameter changes during paper trading.
* [ ] The paper trading sample size is too small.
* [ ] Profit mainly comes from a single extreme event.
* [ ] The strategy averages down after losses without strict limits.
* [ ] The operator cannot clearly explain when the strategy should stop.
* [ ] Logging is incomplete.
* [ ] Alerting is missing.
* [ ] There are no capital limits or loss limits.
* [ ] API key permissions are excessive or security controls are insufficient.
* [ ] Paper trading results cannot be compared with small-scale live trading results.

---

# 22. Final Paper Trading Acceptance Report Format

A formal acceptance report should be produced after each paper trading period.

## 22.1 Basic Information

* Strategy name:
* Strategy version:
* Parameter version:
* Risk control version:
* Execution model version:
* Testing period:
* Exchange:
* Trading pair:
* Initial capital:
* Order type:
* Fee setting:
* Slippage setting:
* Data source:
* Whether shadow trading was used:
* Whether parameter changes were allowed during testing:

## 22.2 Performance Summary

* Total return:
* Net profit:
* Gross profit:
* Total transaction fees:
* Total slippage:
* Maximum drawdown:
* Win rate:
* Win-loss ratio:
* Profit factor:
* Sharpe ratio:
* Number of trades:
* Average holding time:
* Maximum consecutive losses:

## 22.3 Trade Quality

* Average slippage:
* Maximum slippage:
* Average execution latency:
* Maximum execution latency:
* Fill rate:
* Partial fill ratio:
* Cancellation ratio:
* Rejection ratio:
* Order timeout count:
* Missed fill count:

## 22.4 Strategy Behavior Deviation

* Whether trade frequency matched expectations:
* Whether win rate matched expectations:
* Whether win-loss ratio matched expectations:
* Whether average holding time matched expectations:
* Whether maximum drawdown matched expectations:
* Whether slippage was higher than expected:
* Whether fill rate was lower than expected:
* Whether abnormal trading behavior occurred:
* Explanation of deviations:

## 22.5 System Stability

* Continuous system running time:
* Number of WebSocket disconnections:
* Number of API errors:
* Number of reconciliation abnormalities:
* Number of program restarts:
* Number of alerts:
* Description of major errors:

## 22.6 Risk Control Trigger Records

* Whether per-trade loss limit was triggered:
* Whether daily loss limit was triggered:
* Whether maximum drawdown limit was triggered:
* Whether position limit was triggered:
* Whether slippage limit was triggered:
* Whether kill switch was tested:
* Whether shutdown process worked correctly:

## 22.7 Security Check

* Whether API key permissions were minimized:
* Whether IP whitelist was enabled:
* Whether withdrawal permission was disabled:
* Whether test and live environments were separated:
* Whether logs avoided sensitive information:
* Whether API key revocation process exists:

## 22.8 Abnormal Event Records

Each abnormal event should include:

* Time of occurrence;
* Type of abnormality;
* Scope of impact;
* System response;
* Whether it caused incorrect trades;
* Whether code modification is required;
* Whether it has been fixed;
* Fix version;
* Whether paper trading must restart.

## 22.9 Final Conclusion

The final conclusion should fall into one of four categories:

1. **Passed. The strategy may enter small-scale live trading.**
   All core checks have passed. Risk control, reconciliation, alerting, logging, and execution model are acceptable.

2. **Conditionally passed. Specific issues must be fixed before small-scale live trading.**
   The strategy logic is broadly acceptable, but non-fatal issues remain and require additional testing after correction.

3. **Failed. Paper trading must be repeated.**
   Significant issues exist in execution, risk control, system stability, reconciliation, logging, or sample size.

4. **Strategy invalidated. Development should pause or return to research stage.**
   After costs, the strategy has no positive expectancy, or paper trading behavior materially contradicts the research assumptions.

The conclusion must not simply say “performance was good.”
It must clearly state whether the strategy is qualified to enter the next stage.

---

# 23. Final Acceptance Principles

The value of paper trading is not to give the trader confidence.
Its value is to remove false confidence.

A qualified paper trading process should expose the following issues as much as possible:

* Whether the strategy is merely a backtest illusion;
* Whether execution assumptions are too idealized;
* Whether slippage is underestimated;
* Whether API behavior may lose control;
* Whether orders can be fully tracked;
* Whether the system can recover from abnormalities;
* Whether risk controls actually work;
* Whether the strategy has the capacity to scale;
* Whether paper trading results can reasonably predict small-scale live trading.

The final question should not be:

> Did this strategy make money in paper trading?

The correct question is:

> After including costs, slippage, latency, errors, risk controls, reconciliation, and abnormal scenario testing, is this strategy still controllable, explainable, executable, and qualified for small-scale live trading?

Only when the strategy passes cost, execution, risk control, system, logging, alerting, reconciliation, security, and abnormal scenario tests in paper trading does it have the basic qualification to enter real-money small-scale live trading.
