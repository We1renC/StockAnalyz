# Cryptocurrency Trading API Planning Document

## 1. Document Purpose

This document defines the functional scope, API modules, data structures, permission model, real-time messaging, risk controls, and operational interfaces required for a cryptocurrency trading API.

The API is positioned as:

> A unified trading API for frontend trading interfaces, automated trading services, internal trading systems, or external clients.

This API does not implement trading strategy logic, such as DCA, TWAP, Grid Trading, arbitrage, or backtesting. Strategy services should be implemented as a separate branch and may use this trading API for order placement, cancellation, querying, and risk validation.

---

## 2. Functional Scope

### 2.1 Features to Be Implemented in This Stage

The API should implement the following core functions:

| Module | Implemented | Description |
|---|---:|---|
| API Authentication and Authorization | Yes | API keys, request signing, permissions, IP whitelist |
| Market Data | Yes | Markets, ticker, order book, trades, candlesticks |
| Account Assets | Yes | Balances, locked assets, ledger records |
| Order Trading | Yes | Market orders, limit orders, conditional orders, cancellations, replacements |
| Order Querying | Yes | Single order query, open orders, historical orders |
| Fill Querying | Yes | Fill records, fees, average execution price |
| Risk Controls | Yes | Pre-trade validation, limits, price deviation checks, kill switch |
| WebSocket | Yes | Real-time market data, order updates, fill updates, balance updates |
| Webhook | Yes | Order, fill, and risk event notifications |
| Audit Logs | Yes | API requests, order creation, cancellations, errors, risk events |
| System Status | Yes | Health checks, trading status, maintenance status |
| Multi-exchange Abstraction | Recommended | Normalize formats and error codes across exchanges |

---

### 2.2 Features Not Implemented in This Stage

The following features are outside the scope of this planning document:

| Feature | Description |
|---|---|
| Withdrawal Functions | No withdrawal requests, withdrawal addresses, withdrawal approvals, or on-chain transfers |
| Strategy Trading | No DCA, TWAP, Grid Trading, arbitrage, or market-making strategy logic |
| Backtesting System | No historical strategy simulation |
| Strategy Performance Analytics | No strategy return, win rate, or drawdown analysis |
| Lending Services | No borrowing, lending, or interest-rate functions |
| NFT / On-chain Wallet Functions | No non-trading asset management |
| Futures Trading | Can be planned for a later version; not included in the first stage |
| Margin Trading | Can be planned for a later version; not included in the first stage |

---

## 3. API Design Principles

### 3.1 Communication Protocols

| Type | Purpose |
|---|---|
| REST API | Queries, order placement, cancellation, risk controls, configuration |
| WebSocket API | Real-time market data, order updates, fill updates, balance changes |
| Webhook | Asynchronous event notifications, such as fills, cancellations, and risk triggers |

---

### 3.2 Data Format

All APIs use JSON format.

All amount, price, and quantity fields must be represented as strings to avoid floating-point precision issues.

```json
{
  "price": "68000.25",
  "quantity": "0.015",
  "fee": "0.12345678"
}
```

Do not use:

```json
{
  "price": 68000.25,
  "quantity": 0.015
}
```

---

### 3.3 Time Format

All timestamps use ISO 8601 UTC format.

```json
{
  "created_at": "2026-06-04T10:00:00Z"
}
```

---

### 3.4 API Versioning

API paths must include a version number.

```text
/v1/orders
/v1/balances
/v1/markets
```

For future breaking changes, use:

```text
/v2/orders
```

---

### 3.5 Pagination Rules

Large query APIs should use cursor-based pagination.

Request:

```http
GET /v1/orders?limit=100&cursor=eyJpZCI6...
```

Response:

```json
{
  "data": [],
  "pagination": {
    "limit": 100,
    "next_cursor": "eyJpZCI6...",
    "has_more": true
  }
}
```

---

### 3.6 Unified Response Format

Successful response:

```json
{
  "success": true,
  "data": {},
  "request_id": "req_123456789"
}
```

Failed response:

```json
{
  "success": false,
  "error": {
    "code": "INSUFFICIENT_BALANCE",
    "message": "Available balance is insufficient.",
    "details": {
      "asset": "USDT",
      "required": "1000",
      "available": "500"
    }
  },
  "request_id": "req_123456789"
}
```

---

## 4. Authorization and Authentication Design

### 4.1 API Key Permission Scopes

API key permissions must be separated. A single API key should not automatically grant access to all functions.

| Scope | Description |
|---|---|
| `read:market` | Read market data |
| `read:account` | Read account and balance data |
| `read:orders` | Read orders |
| `read:fills` | Read fill records |
| `trade:spot` | Place, cancel, and replace spot orders |
| `risk:read` | Read risk limits |
| `risk:write` | Modify risk limits and operate the kill switch |
| `webhook:read` | Read webhook settings |
| `webhook:write` | Create, update, and delete webhooks |
| `audit:read` | Read audit logs |
| `admin:keys` | Manage API keys |
| `admin:system` | Query or adjust system status |

---

### 4.2 API Signature Headers

Private APIs must include the following headers:

```text
X-API-Key: api_key_xxx
X-Timestamp: 1780567200000
X-Nonce: 8f3b0e91-xxxx-xxxx
X-Signature: hmac_signature
Idempotency-Key: client_unique_request_id
```

---

### 4.3 Signature Payload

Recommended signature format:

```text
timestamp + method + path + query_string + body
```

Example:

```text
1780567200000POST/v1/orders{"symbol":"BTC-USDT","side":"buy"}
```

Signature algorithm:

```text
HMAC-SHA256(payload, api_secret)
```

---

### 4.4 Replay Protection

The system must validate the following:

| Check | Rule |
|---|---|
| Timestamp | Difference from server time must not exceed 30 seconds |
| Nonce | Must not be reused under the same API key |
| Signature | Must be verified successfully |
| Idempotency-Key | Retried requests must not create duplicate orders |

---

## 5. REST API Planning

---

## 5.1 System API

### 5.1.1 Query System Health

```http
GET /v1/health
```

Auth: Not required

Response:

```json
{
  "status": "ok",
  "timestamp": "2026-06-04T10:00:00Z"
}
```

---

### 5.1.2 Query Server Time

```http
GET /v1/server-time
```

Auth: Not required

Response:

```json
{
  "server_time": "2026-06-04T10:00:00Z",
  "server_time_ms": 1780567200000
}
```

---

### 5.1.3 Query Trading System Status

```http
GET /v1/system/status
```

Auth: Not required

Response:

```json
{
  "status": "online",
  "trading_status": "trading",
  "message": null,
  "updated_at": "2026-06-04T10:00:00Z"
}
```

System status enum:

| status | Description |
|---|---|
| `online` | System is operating normally |
| `degraded` | Some functions are degraded |
| `maintenance` | System is under maintenance |
| `offline` | System is unavailable |

Trading status enum:

| trading_status | Description |
|---|---|
| `trading` | Normal trading is allowed |
| `cancel_only` | Only order cancellations are allowed |
| `post_only` | Only maker-only orders are allowed |
| `halted` | Trading is suspended |
| `maintenance` | Trading is unavailable due to maintenance |

---

## 5.2 API Key Management API

### 5.2.1 Create API Key

```http
POST /v1/api-keys
```

Auth: Required  
Permission: `admin:keys`

Request:

```json
{
  "name": "trading-bot-main",
  "scopes": [
    "read:market",
    "read:account",
    "read:orders",
    "read:fills",
    "trade:spot"
  ],
  "ip_whitelist": [
    "203.0.113.10"
  ],
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Response:

```json
{
  "api_key_id": "key_123",
  "api_key": "api_xxx",
  "api_secret": "secret_xxx",
  "name": "trading-bot-main",
  "scopes": [
    "read:market",
    "read:account",
    "trade:spot"
  ],
  "status": "active",
  "created_at": "2026-06-04T10:00:00Z"
}
```

Note:

> `api_secret` must only be displayed once during creation and must not be retrievable afterward.

---

### 5.2.2 Query API Key List

```http
GET /v1/api-keys
```

Auth: Required  
Permission: `admin:keys`

Response:

```json
{
  "data": [
    {
      "api_key_id": "key_123",
      "name": "trading-bot-main",
      "scopes": [
        "read:market",
        "trade:spot"
      ],
      "ip_whitelist": [
        "203.0.113.10"
      ],
      "status": "active",
      "created_at": "2026-06-04T10:00:00Z",
      "expires_at": "2026-12-31T23:59:59Z",
      "last_used_at": "2026-06-04T10:30:00Z"
    }
  ]
}
```

---

### 5.2.3 Update API Key

```http
PATCH /v1/api-keys/{api_key_id}
```

Auth: Required  
Permission: `admin:keys`

Request:

```json
{
  "name": "trading-bot-main-v2",
  "scopes": [
    "read:market",
    "read:account",
    "read:orders",
    "trade:spot"
  ],
  "ip_whitelist": [
    "203.0.113.10",
    "203.0.113.11"
  ],
  "status": "active"
}
```

---

### 5.2.4 Disable API Key

```http
POST /v1/api-keys/{api_key_id}/disable
```

Auth: Required  
Permission: `admin:keys`

Response:

```json
{
  "api_key_id": "key_123",
  "status": "disabled",
  "updated_at": "2026-06-04T11:00:00Z"
}
```

---

### 5.2.5 Delete API Key

```http
DELETE /v1/api-keys/{api_key_id}
```

Auth: Required  
Permission: `admin:keys`

Response:

```json
{
  "api_key_id": "key_123",
  "deleted": true
}
```

---

## 5.3 Market Data API

Market Data APIs are public by default, but an API key may be required depending on the business model.

---

### 5.3.1 Query Market List

```http
GET /v1/markets
```

Auth: Not required  
Permission: `read:market`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `status` | No | `trading`, `halted`, `maintenance` |
| `base_asset` | No | For example, `BTC` |
| `quote_asset` | No | For example, `USDT` |

Response:

```json
{
  "data": [
    {
      "symbol": "BTC-USDT",
      "base_asset": "BTC",
      "quote_asset": "USDT",
      "status": "trading",
      "price_precision": 2,
      "quantity_precision": 6,
      "min_quantity": "0.00001",
      "max_quantity": "100",
      "min_notional": "5",
      "tick_size": "0.01",
      "lot_size": "0.000001",
      "maker_fee_rate": "0.001",
      "taker_fee_rate": "0.001"
    }
  ]
}
```

---

### 5.3.2 Query Single Market

```http
GET /v1/markets/{symbol}
```

Example:

```http
GET /v1/markets/BTC-USDT
```

Response:

```json
{
  "symbol": "BTC-USDT",
  "base_asset": "BTC",
  "quote_asset": "USDT",
  "status": "trading",
  "price_precision": 2,
  "quantity_precision": 6,
  "min_quantity": "0.00001",
  "max_quantity": "100",
  "min_notional": "5",
  "tick_size": "0.01",
  "lot_size": "0.000001",
  "allowed_order_types": [
    "market",
    "limit",
    "stop_market",
    "stop_limit"
  ],
  "allowed_time_in_force": [
    "GTC",
    "IOC",
    "FOK"
  ]
}
```

---

### 5.3.3 Query Single Ticker

```http
GET /v1/ticker?symbol=BTC-USDT
```

Response:

```json
{
  "symbol": "BTC-USDT",
  "last_price": "68000.25",
  "best_bid": "67999.50",
  "best_ask": "68001.00",
  "high_24h": "69000.00",
  "low_24h": "66000.00",
  "volume_24h": "1234.5678",
  "quote_volume_24h": "84500000.12",
  "price_change_24h": "1200.25",
  "price_change_percent_24h": "1.80",
  "timestamp": "2026-06-04T10:00:00Z"
}
```

---

### 5.3.4 Query Multiple Tickers

```http
GET /v1/tickers
```

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbols` | No | Comma-separated list, for example `BTC-USDT,ETH-USDT` |

Response:

```json
{
  "data": [
    {
      "symbol": "BTC-USDT",
      "last_price": "68000.25",
      "volume_24h": "1234.5678",
      "price_change_percent_24h": "1.80",
      "timestamp": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

### 5.3.5 Query Order Book

```http
GET /v1/orderbook?symbol=BTC-USDT&depth=50
```

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | Yes | Trading pair |
| `depth` | No | 5, 10, 20, 50, 100; default is 50 |

Response:

```json
{
  "symbol": "BTC-USDT",
  "last_update_id": 987654321,
  "bids": [
    ["67999.50", "0.2500"],
    ["67998.00", "1.1000"]
  ],
  "asks": [
    ["68001.00", "0.3000"],
    ["68002.50", "0.8000"]
  ],
  "timestamp": "2026-06-04T10:00:00Z"
}
```

---

### 5.3.6 Query Recent Trades

```http
GET /v1/trades?symbol=BTC-USDT&limit=100
```

Response:

```json
{
  "data": [
    {
      "trade_id": "mtrade_123",
      "symbol": "BTC-USDT",
      "price": "68000.25",
      "quantity": "0.0100",
      "side": "buy",
      "executed_at": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

### 5.3.7 Query Candlesticks

```http
GET /v1/klines?symbol=BTC-USDT&interval=1m&start_time=2026-06-04T00:00:00Z&end_time=2026-06-04T10:00:00Z
```

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | Yes | Trading pair |
| `interval` | Yes | `1m`, `5m`, `15m`, `1h`, `4h`, `1d` |
| `start_time` | No | Start time |
| `end_time` | No | End time |
| `limit` | No | Default 500, maximum 1000 |

Response:

```json
{
  "symbol": "BTC-USDT",
  "interval": "1m",
  "data": [
    {
      "open_time": "2026-06-04T10:00:00Z",
      "open": "68000.00",
      "high": "68100.00",
      "low": "67950.00",
      "close": "68050.00",
      "volume": "12.3456",
      "quote_volume": "840000.12",
      "trade_count": 350,
      "close_time": "2026-06-04T10:00:59Z"
    }
  ]
}
```

---

## 5.4 Account API

Account APIs require API key authentication and the corresponding read permissions.

---

### 5.4.1 Query Account Information

```http
GET /v1/account
```

Auth: Required  
Permission: `read:account`

Response:

```json
{
  "account_id": "acct_123",
  "account_type": "spot",
  "status": "active",
  "trading_enabled": true,
  "created_at": "2026-06-04T10:00:00Z"
}
```

---

### 5.4.2 Query Balances

```http
GET /v1/balances
```

Auth: Required  
Permission: `read:account`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `asset` | No | Single asset |
| `hide_zero` | No | Hide zero balances; default is `true` |

Response:

```json
{
  "data": [
    {
      "asset": "USDT",
      "available": "1500.00",
      "locked": "500.00",
      "total": "2000.00",
      "updated_at": "2026-06-04T10:00:00Z"
    },
    {
      "asset": "BTC",
      "available": "0.050000",
      "locked": "0.010000",
      "total": "0.060000",
      "updated_at": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

### 5.4.3 Query Single Asset Balance

```http
GET /v1/balances/{asset}
```

Example:

```http
GET /v1/balances/USDT
```

Response:

```json
{
  "asset": "USDT",
  "available": "1500.00",
  "locked": "500.00",
  "total": "2000.00",
  "updated_at": "2026-06-04T10:00:00Z"
}
```

---

### 5.4.4 Query Ledger Records

```http
GET /v1/ledger
```

Auth: Required  
Permission: `read:account`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `asset` | No | Asset |
| `type` | No | `trade`, `fee`, `adjustment`, `rebate` |
| `start_time` | No | Start time |
| `end_time` | No | End time |
| `limit` | No | Default 100 |
| `cursor` | No | Pagination cursor |

Response:

```json
{
  "data": [
    {
      "ledger_id": "led_123",
      "asset": "USDT",
      "type": "trade",
      "amount": "-680.00",
      "balance_after": "1320.00",
      "reference_type": "fill",
      "reference_id": "fill_123",
      "created_at": "2026-06-04T10:00:00Z"
    }
  ],
  "pagination": {
    "limit": 100,
    "next_cursor": null,
    "has_more": false
  }
}
```

---

### 5.4.5 Query Trading Fees

```http
GET /v1/fees
```

Auth: Required  
Permission: `read:account`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | No | Specific trading pair |

Response:

```json
{
  "data": [
    {
      "symbol": "BTC-USDT",
      "maker_fee_rate": "0.001",
      "taker_fee_rate": "0.001",
      "fee_tier": "standard",
      "updated_at": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

## 5.5 Order API

The Order API is the core trading API.

---

### 5.5.1 Supported Order Types

The first stage should support:

| Order Type | `type` | Description |
|---|---|---|
| Market Order | `market` | Executes immediately at the best available market price |
| Limit Order | `limit` | Places an order at a specified price |
| Stop Market Order | `stop_market` | Sends a market order after the trigger price is reached |
| Stop Limit Order | `stop_limit` | Sends a limit order after the trigger price is reached |
| Take Profit Market Order | `take_profit_market` | Sends a market order after the take-profit trigger price is reached |
| Take Profit Limit Order | `take_profit_limit` | Sends a limit order after the take-profit trigger price is reached |

Strategy-type orders are not included:

| Not Included | Reason |
|---|---|
| DCA | Handled by the strategy branch |
| TWAP | Handled by the strategy branch |
| Grid Trading | Handled by the strategy branch |
| Arbitrage | Handled by the strategy branch |
| Market Making | Handled by the strategy branch |

---

### 5.5.2 Time in Force

Supported values:

| Value | Description |
|---|---|
| `GTC` | Good Till Cancelled; remains active until filled or cancelled |
| `IOC` | Immediate Or Cancel; fills immediately and cancels the remainder |
| `FOK` | Fill Or Kill; must fill completely immediately or be cancelled |

---

### 5.5.3 Order Statuses

| Status | Description |
|---|---|
| `pending` | Request received but not yet sent |
| `accepted` | Passed risk validation and sent |
| `open` | Open on the order book |
| `partially_filled` | Partially filled |
| `filled` | Fully filled |
| `cancel_pending` | Cancellation in progress |
| `cancelled` | Cancelled |
| `rejected` | Order rejected |
| `expired` | Order expired |
| `failed` | System error |
| `unknown` | Status requires reconciliation |

---

### 5.5.4 Create Order

```http
POST /v1/orders
```

Auth: Required  
Permission: `trade:spot`

Headers:

```text
Idempotency-Key: bot-20260604-000001
```

Request:

```json
{
  "client_order_id": "bot-20260604-000001",
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "price": "68000.00",
  "quantity": "0.01",
  "time_in_force": "GTC",
  "post_only": false,
  "self_trade_prevention": "cancel_newest"
}
```

Field descriptions:

| Field | Required | Description |
|---|---:|---|
| `client_order_id` | Yes | Client-side order ID; must be unique |
| `symbol` | Yes | Trading pair |
| `side` | Yes | `buy` or `sell` |
| `type` | Yes | Order type |
| `price` | Depends on type | Required for limit orders |
| `quantity` | Yes | Order quantity |
| `quote_quantity` | Depends on type | Market order amount calculated by quote currency |
| `stop_price` | Depends on type | Trigger price for conditional orders |
| `time_in_force` | No | Default is `GTC` |
| `post_only` | No | Maker-only order |
| `self_trade_prevention` | No | Self-trade prevention mode |

Response:

```json
{
  "order_id": "ord_123456",
  "client_order_id": "bot-20260604-000001",
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "status": "open",
  "price": "68000.00",
  "quantity": "0.01",
  "filled_quantity": "0",
  "remaining_quantity": "0.01",
  "average_price": null,
  "time_in_force": "GTC",
  "post_only": false,
  "created_at": "2026-06-04T10:00:00Z",
  "updated_at": "2026-06-04T10:00:00Z"
}
```

---

### 5.5.5 Create Test Order

A test order only performs validation and is not actually submitted.

```http
POST /v1/orders/test
```

Auth: Required  
Permission: `trade:spot`

Request:

```json
{
  "client_order_id": "test-001",
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "price": "68000.00",
  "quantity": "0.01"
}
```

Response:

```json
{
  "valid": true,
  "estimated_notional": "680.00",
  "estimated_fee": "0.68",
  "checks": [
    {
      "name": "balance_check",
      "passed": true
    },
    {
      "name": "min_notional_check",
      "passed": true
    },
    {
      "name": "price_precision_check",
      "passed": true
    }
  ]
}
```

---

### 5.5.6 Query Single Order

```http
GET /v1/orders/{order_id}
```

Auth: Required  
Permission: `read:orders`

Response:

```json
{
  "order_id": "ord_123456",
  "exchange_order_id": "987654321",
  "client_order_id": "bot-20260604-000001",
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "status": "partially_filled",
  "price": "68000.00",
  "quantity": "0.01",
  "filled_quantity": "0.004",
  "remaining_quantity": "0.006",
  "average_price": "67980.00",
  "fee": "0.27",
  "fee_asset": "USDT",
  "created_at": "2026-06-04T10:00:00Z",
  "updated_at": "2026-06-04T10:03:00Z"
}
```

---

### 5.5.7 Query Order by Client Order ID

```http
GET /v1/orders/by-client-id/{client_order_id}
```

Auth: Required  
Permission: `read:orders`

Response: Same as single order query.

---

### 5.5.8 Query Order List

```http
GET /v1/orders
```

Auth: Required  
Permission: `read:orders`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | No | Trading pair |
| `side` | No | `buy`, `sell` |
| `type` | No | Order type |
| `status` | No | Order status |
| `start_time` | No | Start time |
| `end_time` | No | End time |
| `limit` | No | Default 100 |
| `cursor` | No | Pagination cursor |

Response:

```json
{
  "data": [
    {
      "order_id": "ord_123456",
      "client_order_id": "bot-20260604-000001",
      "symbol": "BTC-USDT",
      "side": "buy",
      "type": "limit",
      "status": "open",
      "price": "68000.00",
      "quantity": "0.01",
      "filled_quantity": "0",
      "created_at": "2026-06-04T10:00:00Z"
    }
  ],
  "pagination": {
    "limit": 100,
    "next_cursor": null,
    "has_more": false
  }
}
```

---

### 5.5.9 Query Open Orders

```http
GET /v1/open-orders
```

Auth: Required  
Permission: `read:orders`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | No | Trading pair |

Response:

```json
{
  "data": [
    {
      "order_id": "ord_123456",
      "symbol": "BTC-USDT",
      "side": "buy",
      "type": "limit",
      "status": "open",
      "price": "68000.00",
      "quantity": "0.01",
      "filled_quantity": "0",
      "remaining_quantity": "0.01",
      "created_at": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

### 5.5.10 Cancel Single Order

```http
POST /v1/orders/{order_id}/cancel
```

Auth: Required  
Permission: `trade:spot`

Request:

```json
{
  "reason": "user_requested"
}
```

Response:

```json
{
  "order_id": "ord_123456",
  "status": "cancel_pending",
  "requested_at": "2026-06-04T10:05:00Z"
}
```

---

### 5.5.11 Cancel Order by Client Order ID

```http
POST /v1/orders/by-client-id/{client_order_id}/cancel
```

Auth: Required  
Permission: `trade:spot`

Response: Same as cancel single order.

---

### 5.5.12 Create Batch Orders

```http
POST /v1/orders/batch
```

Auth: Required  
Permission: `trade:spot`

Request:

```json
{
  "orders": [
    {
      "client_order_id": "batch-001",
      "symbol": "BTC-USDT",
      "side": "buy",
      "type": "limit",
      "price": "67000.00",
      "quantity": "0.01",
      "time_in_force": "GTC"
    },
    {
      "client_order_id": "batch-002",
      "symbol": "ETH-USDT",
      "side": "buy",
      "type": "limit",
      "price": "3500.00",
      "quantity": "0.1",
      "time_in_force": "GTC"
    }
  ]
}
```

Response:

```json
{
  "data": [
    {
      "client_order_id": "batch-001",
      "order_id": "ord_001",
      "status": "open"
    },
    {
      "client_order_id": "batch-002",
      "order_id": "ord_002",
      "status": "open"
    }
  ]
}
```

Rules:

| Rule | Description |
|---|---|
| Maximum orders per request | Recommended 10 to 50 orders |
| Independent result per order | One failure should not fail all orders unless atomic mode is specified |
| Atomic mode support | Optional; either all succeed or all fail |

---

### 5.5.13 Cancel Batch Orders

```http
POST /v1/orders/cancel-batch
```

Auth: Required  
Permission: `trade:spot`

Request:

```json
{
  "order_ids": [
    "ord_001",
    "ord_002"
  ]
}
```

Response:

```json
{
  "data": [
    {
      "order_id": "ord_001",
      "status": "cancel_pending"
    },
    {
      "order_id": "ord_002",
      "status": "cancel_pending"
    }
  ]
}
```

---

### 5.5.14 Cancel All Orders

```http
POST /v1/orders/cancel-all
```

Auth: Required  
Permission: `trade:spot`

Request:

```json
{
  "symbol": "BTC-USDT",
  "reason": "manual_cancel_all"
}
```

Response:

```json
{
  "symbol": "BTC-USDT",
  "cancel_requested_count": 12,
  "requested_at": "2026-06-04T10:10:00Z"
}
```

Behavior:

| Scenario | Behavior |
|---|---|
| `symbol` is specified | Cancel open orders only for that market |
| `symbol` is not specified | Cancel open orders for all markets |
| System is in `cancel_only` | Still allowed |
| System is in `halted` | Whether allowed depends on system configuration |

---

### 5.5.15 Replace Order

```http
POST /v1/orders/{order_id}/replace
```

Auth: Required  
Permission: `trade:spot`

Request:

```json
{
  "new_client_order_id": "replace-001",
  "price": "68100.00",
  "quantity": "0.015"
}
```

Response:

```json
{
  "old_order_id": "ord_123456",
  "new_order_id": "ord_789012",
  "new_client_order_id": "replace-001",
  "status": "open",
  "price": "68100.00",
  "quantity": "0.015",
  "created_at": "2026-06-04T10:12:00Z"
}
```

Implementation methods:

| Method | Description |
|---|---|
| Native amend | Use native amend-order support if the exchange provides it |
| Cancel + New | If unsupported, cancel the old order first and then create a new order |
| Atomicity | If atomic replacement is unsupported, the response must expose the risk |

---

## 5.6 Fill / Trade API

---

### 5.6.1 Query Fill Records

```http
GET /v1/fills
```

Auth: Required  
Permission: `read:fills`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | No | Trading pair |
| `order_id` | No | Order ID |
| `side` | No | `buy`, `sell` |
| `start_time` | No | Start time |
| `end_time` | No | End time |
| `limit` | No | Default 100 |
| `cursor` | No | Pagination cursor |

Response:

```json
{
  "data": [
    {
      "fill_id": "fill_123",
      "order_id": "ord_123456",
      "client_order_id": "bot-20260604-000001",
      "symbol": "BTC-USDT",
      "side": "buy",
      "price": "67980.00",
      "quantity": "0.004",
      "notional": "271.92",
      "fee": "0.27",
      "fee_asset": "USDT",
      "liquidity": "maker",
      "executed_at": "2026-06-04T10:03:00Z"
    }
  ],
  "pagination": {
    "limit": 100,
    "next_cursor": null,
    "has_more": false
  }
}
```

---

### 5.6.2 Query Single Fill

```http
GET /v1/fills/{fill_id}
```

Auth: Required  
Permission: `read:fills`

Response:

```json
{
  "fill_id": "fill_123",
  "order_id": "ord_123456",
  "symbol": "BTC-USDT",
  "side": "buy",
  "price": "67980.00",
  "quantity": "0.004",
  "notional": "271.92",
  "fee": "0.27",
  "fee_asset": "USDT",
  "liquidity": "maker",
  "executed_at": "2026-06-04T10:03:00Z"
}
```

---

### 5.6.3 Query Fill Summary

```http
GET /v1/fills/summary
```

Auth: Required  
Permission: `read:fills`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `symbol` | No | Trading pair |
| `start_time` | Yes | Start time |
| `end_time` | Yes | End time |

Response:

```json
{
  "symbol": "BTC-USDT",
  "start_time": "2026-06-04T00:00:00Z",
  "end_time": "2026-06-04T23:59:59Z",
  "buy_volume": "0.2500",
  "sell_volume": "0.1000",
  "buy_notional": "17000.00",
  "sell_notional": "6900.00",
  "total_fee": "23.90",
  "fee_asset": "USDT",
  "trade_count": 32
}
```

---

## 5.7 Risk API

The Risk API handles pre-trade validation, trading limits, circuit breaking, and emergency stop controls.

---

### 5.7.1 Query Risk Limits

```http
GET /v1/risk/limits
```

Auth: Required  
Permission: `risk:read`

Response:

```json
{
  "max_single_order_notional": "10000",
  "max_daily_notional": "100000",
  "max_open_orders": 100,
  "max_price_deviation_percent": "5",
  "max_asset_exposure": {
    "BTC": "1.5",
    "ETH": "20"
  },
  "allowed_symbols": [
    "BTC-USDT",
    "ETH-USDT"
  ],
  "blocked_symbols": [],
  "updated_at": "2026-06-04T10:00:00Z"
}
```

---

### 5.7.2 Update Risk Limits

```http
PATCH /v1/risk/limits
```

Auth: Required  
Permission: `risk:write`

Request:

```json
{
  "max_single_order_notional": "20000",
  "max_daily_notional": "150000",
  "max_open_orders": 150,
  "max_price_deviation_percent": "3",
  "allowed_symbols": [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT"
  ]
}
```

Response:

```json
{
  "updated": true,
  "updated_at": "2026-06-04T10:20:00Z"
}
```

---

### 5.7.3 Validate Order

```http
POST /v1/risk/validate-order
```

Auth: Required  
Permission: `risk:read`

Request:

```json
{
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "price": "68000.00",
  "quantity": "0.01"
}
```

Response:

```json
{
  "valid": true,
  "estimated_notional": "680.00",
  "estimated_fee": "0.68",
  "checks": [
    {
      "name": "symbol_status",
      "passed": true
    },
    {
      "name": "balance",
      "passed": true
    },
    {
      "name": "min_notional",
      "passed": true
    },
    {
      "name": "price_precision",
      "passed": true
    },
    {
      "name": "quantity_precision",
      "passed": true
    },
    {
      "name": "price_deviation",
      "passed": true
    }
  ]
}
```

---

### 5.7.4 Query Exposure

```http
GET /v1/risk/exposure
```

Auth: Required  
Permission: `risk:read`

Response:

```json
{
  "total_equity_usd": "25000.00",
  "assets": [
    {
      "asset": "BTC",
      "quantity": "0.50",
      "mark_price": "68000.00",
      "value_usd": "34000.00",
      "exposure_percent": "65.00"
    }
  ],
  "updated_at": "2026-06-04T10:00:00Z"
}
```

---

### 5.7.5 Activate Kill Switch

The kill switch immediately stops trading and may optionally cancel all open orders.

```http
POST /v1/risk/kill-switch/activate
```

Auth: Required  
Permission: `risk:write`

Request:

```json
{
  "reason": "abnormal_trading_detected",
  "cancel_all_orders": true
}
```

Response:

```json
{
  "active": true,
  "cancel_all_orders": true,
  "activated_at": "2026-06-04T10:30:00Z"
}
```

Behavior after activation:

| Function | Behavior |
|---|---|
| New order placement | Rejected |
| Order replacement | Rejected |
| Order cancellation | Allowed |
| Queries | Allowed |
| WebSocket | Continues pushing updates |
| Webhook | Continues sending notifications |

---

### 5.7.6 Deactivate Kill Switch

```http
POST /v1/risk/kill-switch/deactivate
```

Auth: Required  
Permission: `risk:write`

Request:

```json
{
  "reason": "manual_recovery"
}
```

Response:

```json
{
  "active": false,
  "deactivated_at": "2026-06-04T11:00:00Z"
}
```

---

### 5.7.7 Query Kill Switch Status

```http
GET /v1/risk/kill-switch
```

Auth: Required  
Permission: `risk:read`

Response:

```json
{
  "active": true,
  "reason": "abnormal_trading_detected",
  "activated_at": "2026-06-04T10:30:00Z",
  "activated_by": "user_123"
}
```

---

## 5.8 Webhook API

Webhooks are used to push asynchronous events to external systems.

---

### 5.8.1 Create Webhook Endpoint

```http
POST /v1/webhooks/endpoints
```

Auth: Required  
Permission: `webhook:write`

Request:

```json
{
  "url": "https://example.com/webhooks/trading",
  "events": [
    "order.created",
    "order.updated",
    "order.cancelled",
    "fill.created",
    "balance.updated",
    "risk.triggered"
  ],
  "secret": "webhook_secret_xxx",
  "status": "active"
}
```

Response:

```json
{
  "webhook_id": "wh_123",
  "url": "https://example.com/webhooks/trading",
  "events": [
    "order.created",
    "order.updated",
    "fill.created"
  ],
  "status": "active",
  "created_at": "2026-06-04T10:00:00Z"
}
```

---

### 5.8.2 Query Webhook Endpoints

```http
GET /v1/webhooks/endpoints
```

Auth: Required  
Permission: `webhook:read`

Response:

```json
{
  "data": [
    {
      "webhook_id": "wh_123",
      "url": "https://example.com/webhooks/trading",
      "events": [
        "order.updated",
        "fill.created"
      ],
      "status": "active",
      "created_at": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

### 5.8.3 Update Webhook Endpoint

```http
PATCH /v1/webhooks/endpoints/{webhook_id}
```

Auth: Required  
Permission: `webhook:write`

Request:

```json
{
  "events": [
    "order.updated",
    "fill.created",
    "risk.triggered"
  ],
  "status": "active"
}
```

---

### 5.8.4 Delete Webhook Endpoint

```http
DELETE /v1/webhooks/endpoints/{webhook_id}
```

Auth: Required  
Permission: `webhook:write`

Response:

```json
{
  "webhook_id": "wh_123",
  "deleted": true
}
```

---

### 5.8.5 Webhook Payload Format

```json
{
  "event_id": "evt_123",
  "event_type": "order.updated",
  "created_at": "2026-06-04T10:00:00Z",
  "data": {
    "order_id": "ord_123456",
    "status": "filled",
    "symbol": "BTC-USDT"
  }
}
```

Webhook headers:

```text
X-Webhook-Id: wh_123
X-Webhook-Timestamp: 1780567200000
X-Webhook-Signature: signature_xxx
```

---

## 5.9 Audit Log API

Audit logs must record all critical actions, including API requests, authentication failures, order creation, order cancellation, order replacement, risk triggers, and permission changes.

---

### 5.9.1 Query Audit Logs

```http
GET /v1/audit-logs
```

Auth: Required  
Permission: `audit:read`

Query:

| Parameter | Required | Description |
|---|---:|---|
| `action` | No | Action type |
| `actor_id` | No | Actor |
| `resource_type` | No | `order`, `api_key`, `risk_limit` |
| `resource_id` | No | Resource ID |
| `start_time` | No | Start time |
| `end_time` | No | End time |
| `limit` | No | Default 100 |
| `cursor` | No | Pagination cursor |

Response:

```json
{
  "data": [
    {
      "audit_id": "audit_123",
      "actor_id": "user_123",
      "api_key_id": "key_123",
      "action": "order.create",
      "resource_type": "order",
      "resource_id": "ord_123456",
      "ip_address": "203.0.113.10",
      "user_agent": "trading-bot/1.0",
      "request_id": "req_123456",
      "result": "success",
      "created_at": "2026-06-04T10:00:00Z"
    }
  ],
  "pagination": {
    "limit": 100,
    "next_cursor": null,
    "has_more": false
  }
}
```

---

## 5.10 Exchange Adapter Management API

If the system needs to connect to multiple exchanges, internal management APIs should be provided. These APIs should be available only to backend or internal services and should not be exposed to normal trading clients.

---

### 5.10.1 Query Exchange List

```http
GET /v1/exchanges
```

Auth: Required  
Permission: `admin:system`

Response:

```json
{
  "data": [
    {
      "exchange": "binance",
      "status": "online",
      "trading_status": "trading",
      "supported_market_types": [
        "spot"
      ],
      "updated_at": "2026-06-04T10:00:00Z"
    },
    {
      "exchange": "coinbase",
      "status": "online",
      "trading_status": "trading",
      "supported_market_types": [
        "spot"
      ],
      "updated_at": "2026-06-04T10:00:00Z"
    }
  ]
}
```

---

### 5.10.2 Query Single Exchange Status

```http
GET /v1/exchanges/{exchange}/status
```

Auth: Required  
Permission: `admin:system`

Response:

```json
{
  "exchange": "binance",
  "status": "online",
  "trading_status": "trading",
  "latency_ms": 35,
  "last_heartbeat_at": "2026-06-04T10:00:00Z",
  "rate_limit": {
    "remaining": 950,
    "reset_at": "2026-06-04T10:01:00Z"
  }
}
```

---

### 5.10.3 Query Exchange Symbol Mapping

```http
GET /v1/exchanges/{exchange}/symbols
```

Auth: Required  
Permission: `admin:system`

Response:

```json
{
  "exchange": "binance",
  "data": [
    {
      "internal_symbol": "BTC-USDT",
      "exchange_symbol": "BTCUSDT",
      "status": "trading"
    }
  ]
}
```

---

## 6. WebSocket API Planning

WebSocket is used for real-time data delivery and prevents trading clients from frequently polling REST APIs.

---

### 6.1 WebSocket URL

```text
wss://api.example.com/ws/v1
```

---

### 6.2 WebSocket Authentication

Public channels do not require authentication.

Private channels require login:

```json
{
  "op": "auth",
  "api_key": "api_xxx",
  "timestamp": 1780567200000,
  "nonce": "nonce_xxx",
  "signature": "signature_xxx"
}
```

Successful response:

```json
{
  "op": "auth",
  "success": true,
  "connection_id": "conn_123"
}
```

---

### 6.3 Subscription Format

```json
{
  "op": "subscribe",
  "channels": [
    {
      "name": "ticker",
      "symbols": [
        "BTC-USDT",
        "ETH-USDT"
      ]
    }
  ]
}
```

Response:

```json
{
  "op": "subscribed",
  "channel": "ticker",
  "symbols": [
    "BTC-USDT",
    "ETH-USDT"
  ]
}
```

---

### 6.4 Public Channels

| Channel | Description |
|---|---|
| `ticker` | Latest price |
| `trades` | Real-time trades |
| `orderbook` | Real-time order book |
| `klines` | Real-time candlesticks |
| `market_status` | Market status |

---

### 6.5 Private Channels

| Channel | Description |
|---|---|
| `orders` | Order creation, updates, fills, cancellations |
| `fills` | Fill notifications |
| `balances` | Balance changes |
| `risk` | Risk events |
| `account` | Account status changes |
| `system` | Private system notifications |

---

### 6.6 Ticker Event

```json
{
  "channel": "ticker",
  "event": "snapshot",
  "symbol": "BTC-USDT",
  "data": {
    "last_price": "68000.25",
    "best_bid": "67999.50",
    "best_ask": "68001.00",
    "volume_24h": "1234.5678",
    "price_change_percent_24h": "1.80"
  },
  "timestamp": "2026-06-04T10:00:00Z"
}
```

---

### 6.7 Order Book Event

Snapshot:

```json
{
  "channel": "orderbook",
  "event": "snapshot",
  "symbol": "BTC-USDT",
  "sequence": 100000,
  "data": {
    "bids": [
      ["67999.50", "0.2500"]
    ],
    "asks": [
      ["68001.00", "0.3000"]
    ]
  },
  "timestamp": "2026-06-04T10:00:00Z"
}
```

Delta:

```json
{
  "channel": "orderbook",
  "event": "delta",
  "symbol": "BTC-USDT",
  "sequence": 100001,
  "prev_sequence": 100000,
  "data": {
    "bids": [
      ["67999.50", "0.1000"]
    ],
    "asks": []
  },
  "timestamp": "2026-06-04T10:00:01Z"
}
```

---

### 6.8 Order Event

```json
{
  "channel": "orders",
  "event": "order.updated",
  "sequence": 200001,
  "data": {
    "order_id": "ord_123456",
    "client_order_id": "bot-20260604-000001",
    "symbol": "BTC-USDT",
    "side": "buy",
    "type": "limit",
    "status": "partially_filled",
    "price": "68000.00",
    "quantity": "0.01",
    "filled_quantity": "0.004",
    "remaining_quantity": "0.006",
    "average_price": "67980.00",
    "updated_at": "2026-06-04T10:03:00Z"
  }
}
```

---

### 6.9 Fill Event

```json
{
  "channel": "fills",
  "event": "fill.created",
  "sequence": 300001,
  "data": {
    "fill_id": "fill_123",
    "order_id": "ord_123456",
    "symbol": "BTC-USDT",
    "side": "buy",
    "price": "67980.00",
    "quantity": "0.004",
    "fee": "0.27",
    "fee_asset": "USDT",
    "liquidity": "maker",
    "executed_at": "2026-06-04T10:03:00Z"
  }
}
```

---

### 6.10 Balance Event

```json
{
  "channel": "balances",
  "event": "balance.updated",
  "sequence": 400001,
  "data": {
    "asset": "USDT",
    "available": "1320.00",
    "locked": "680.00",
    "total": "2000.00",
    "updated_at": "2026-06-04T10:03:00Z"
  }
}
```

---

### 6.11 WebSocket Reliability Requirements

| Item | Requirement |
|---|---|
| Heartbeat | Server sends ping every 15 to 30 seconds |
| Client pong | Client must respond with pong |
| Sequence number | Each channel must provide increasing sequence numbers |
| Reconnect | Client must be able to resubscribe after disconnection |
| Snapshot + Delta | Order book must support snapshot and delta updates |
| Gap detection | Client must reload snapshot when sequence gap is detected |
| Private replay | Private order events must be recoverable through REST queries |
| Duplicate handling | Client must deduplicate using `event_id` or sequence |

---

## 7. Core Data Models

---

### 7.1 Market

```json
{
  "symbol": "BTC-USDT",
  "base_asset": "BTC",
  "quote_asset": "USDT",
  "status": "trading",
  "price_precision": 2,
  "quantity_precision": 6,
  "min_quantity": "0.00001",
  "max_quantity": "100",
  "min_notional": "5",
  "tick_size": "0.01",
  "lot_size": "0.000001"
}
```

---

### 7.2 Balance

```json
{
  "asset": "USDT",
  "available": "1500.00",
  "locked": "500.00",
  "total": "2000.00",
  "updated_at": "2026-06-04T10:00:00Z"
}
```

---

### 7.3 Order

```json
{
  "order_id": "ord_123456",
  "exchange_order_id": "987654321",
  "client_order_id": "bot-20260604-000001",
  "exchange": "binance",
  "symbol": "BTC-USDT",
  "side": "buy",
  "type": "limit",
  "status": "open",
  "price": "68000.00",
  "quantity": "0.01",
  "filled_quantity": "0",
  "remaining_quantity": "0.01",
  "average_price": null,
  "fee": "0",
  "fee_asset": "USDT",
  "time_in_force": "GTC",
  "post_only": false,
  "created_at": "2026-06-04T10:00:00Z",
  "updated_at": "2026-06-04T10:00:00Z"
}
```

---

### 7.4 Fill

```json
{
  "fill_id": "fill_123",
  "order_id": "ord_123456",
  "client_order_id": "bot-20260604-000001",
  "symbol": "BTC-USDT",
  "side": "buy",
  "price": "67980.00",
  "quantity": "0.004",
  "notional": "271.92",
  "fee": "0.27",
  "fee_asset": "USDT",
  "liquidity": "maker",
  "executed_at": "2026-06-04T10:03:00Z"
}
```

---

### 7.5 Ledger

```json
{
  "ledger_id": "led_123",
  "asset": "USDT",
  "type": "trade",
  "amount": "-271.92",
  "balance_after": "1320.00",
  "reference_type": "fill",
  "reference_id": "fill_123",
  "created_at": "2026-06-04T10:03:00Z"
}
```

---

## 8. Error Code Planning

---

### 8.1 Authentication Errors

| Code | HTTP Status | Description |
|---|---:|---|
| `UNAUTHORIZED` | 401 | Not authenticated |
| `INVALID_API_KEY` | 401 | Invalid API key |
| `INVALID_SIGNATURE` | 401 | Invalid signature |
| `TIMESTAMP_EXPIRED` | 401 | Timestamp expired |
| `NONCE_REPLAYED` | 401 | Nonce reused |
| `PERMISSION_DENIED` | 403 | Insufficient permissions |
| `IP_NOT_ALLOWED` | 403 | IP is not whitelisted |

---

### 8.2 Order Errors

| Code | HTTP Status | Description |
|---|---:|---|
| `INVALID_SYMBOL` | 400 | Market does not exist |
| `SYMBOL_NOT_TRADING` | 400 | Market is not tradable |
| `INVALID_ORDER_TYPE` | 400 | Unsupported order type |
| `INVALID_SIDE` | 400 | Invalid buy/sell side |
| `INVALID_PRICE` | 400 | Invalid price format |
| `INVALID_QUANTITY` | 400 | Invalid quantity format |
| `PRICE_PRECISION_EXCEEDED` | 400 | Price precision exceeded |
| `QUANTITY_PRECISION_EXCEEDED` | 400 | Quantity precision exceeded |
| `MIN_NOTIONAL_NOT_MET` | 400 | Minimum notional not met |
| `MIN_QUANTITY_NOT_MET` | 400 | Minimum order quantity not met |
| `INSUFFICIENT_BALANCE` | 400 | Insufficient balance |
| `DUPLICATE_CLIENT_ORDER_ID` | 409 | Duplicate client order ID |
| `ORDER_NOT_FOUND` | 404 | Order not found |
| `ORDER_ALREADY_FINALIZED` | 409 | Order is already filled or cancelled |
| `ORDER_CANCEL_REJECTED` | 400 | Order cancellation rejected |
| `ORDER_REPLACE_REJECTED` | 400 | Order replacement rejected |

---

### 8.3 Risk Control Errors

| Code | HTTP Status | Description |
|---|---:|---|
| `RISK_LIMIT_EXCEEDED` | 400 | Risk limit exceeded |
| `MAX_ORDER_NOTIONAL_EXCEEDED` | 400 | Single-order maximum notional exceeded |
| `MAX_DAILY_NOTIONAL_EXCEEDED` | 400 | Daily trading notional exceeded |
| `MAX_OPEN_ORDERS_EXCEEDED` | 400 | Open order limit exceeded |
| `PRICE_DEVIATION_EXCEEDED` | 400 | Price deviation exceeded |
| `SYMBOL_BLOCKED` | 400 | Market is blocked |
| `KILL_SWITCH_ACTIVE` | 423 | Kill switch is active |

---

### 8.4 System Errors

| Code | HTTP Status | Description |
|---|---:|---|
| `RATE_LIMIT_EXCEEDED` | 429 | Request limit exceeded |
| `EXCHANGE_UNAVAILABLE` | 503 | External exchange unavailable |
| `EXCHANGE_TIMEOUT` | 504 | External exchange timed out |
| `SYSTEM_MAINTENANCE` | 503 | System under maintenance |
| `INTERNAL_ERROR` | 500 | Internal error |
| `SERVICE_DEGRADED` | 503 | Service degraded |

---

## 9. Rate Limit Planning

Rate limits should be enforced by API type, API key, IP address, and account.

| API Type | Recommended Limit |
|---|---|
| Market Data | 600 requests / minute |
| Account API | 120 requests / minute |
| Order API | 60 requests / minute |
| Batch Order API | 10 requests / minute |
| Risk API | 60 requests / minute |
| Audit API | 30 requests / minute |
| WebSocket Connect | 10 connections / minute |
| WebSocket Subscribe | 100 subscriptions / connection |

Rate limit headers:

```text
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 55
X-RateLimit-Reset: 1780567260000
```

---

## 10. Pre-trade Risk Control Rules

Each order must go through the following validations before being submitted:

| Order | Check | Description |
|---:|---|---|
| 1 | API key status | Whether the key is active and not expired |
| 2 | Permission | Whether it has `trade:spot` |
| 3 | IP whitelist | Whether the source IP is allowed |
| 4 | Kill switch | Whether trading has been stopped |
| 5 | System trading status | Whether trading is currently allowed |
| 6 | Market status | Whether the symbol status is `trading` |
| 7 | Order type | Whether this market supports the specified type |
| 8 | Price precision | Whether the price conforms to tick size |
| 9 | Quantity precision | Whether the quantity conforms to lot size |
| 10 | Minimum quantity | Whether it meets min quantity |
| 11 | Minimum notional | Whether it meets min notional |
| 12 | Balance | Whether the balance is sufficient |
| 13 | Max single-order notional | Whether the order exceeds the single-order limit |
| 14 | Max daily notional | Whether the account exceeds the daily trading limit |
| 15 | Open order count | Whether open orders exceed the limit |
| 16 | Price deviation | Whether the price deviates too far from reference price |
| 17 | Client order ID | Whether the client order ID is duplicated |
| 18 | Idempotency | Whether the request is a retry |

---

## 11. Order Consistency and Reconciliation

To avoid inconsistencies between local order status and exchange order status, a reconciliation mechanism must be implemented.

---

### 11.1 Reconciliation Scenarios

| Scenario | Handling |
|---|---|
| WebSocket disconnection | Reconnect and pull missing order data via REST |
| Order placement timeout | Query the exchange by `client_order_id` |
| Cancellation timeout | Query the order status again |
| Partial fill event not received | Periodically pull fills to backfill data |
| Order status is unknown | Move the order to the reconciliation queue |
| Local and exchange status mismatch | Use the exchange final state as the source of truth and preserve audit records |

---

### 11.2 Internal Reconciliation API

Recommended for internal use only.

```http
POST /v1/internal/reconciliation/orders/{order_id}
```

```http
POST /v1/internal/reconciliation/fills
```

```http
GET /v1/internal/reconciliation/issues
```

---

## 12. Recommended Database Table Design

---

### 12.1 `api_keys`

| Field | Type | Description |
|---|---|---|
| `id` | string | API key ID |
| `account_id` | string | Account ID |
| `name` | string | Name |
| `api_key_hash` | string | API key hash |
| `api_secret_hash` | string | Secret hash or encrypted value |
| `scopes` | json | Permissions |
| `ip_whitelist` | json | IP whitelist |
| `status` | string | active / disabled |
| `expires_at` | timestamp | Expiration time |
| `last_used_at` | timestamp | Last used time |
| `created_at` | timestamp | Creation time |

---

### 12.2 `markets`

| Field | Type | Description |
|---|---|---|
| `symbol` | string | BTC-USDT |
| `base_asset` | string | BTC |
| `quote_asset` | string | USDT |
| `status` | string | trading / halted |
| `price_precision` | int | Price precision |
| `quantity_precision` | int | Quantity precision |
| `min_quantity` | decimal | Minimum quantity |
| `min_notional` | decimal | Minimum notional |
| `tick_size` | decimal | Price tick size |
| `lot_size` | decimal | Quantity step size |

---

### 12.3 `balances`

| Field | Type | Description |
|---|---|---|
| `account_id` | string | Account ID |
| `asset` | string | Asset |
| `available` | decimal | Available balance |
| `locked` | decimal | Locked balance |
| `total` | decimal | Total balance |
| `updated_at` | timestamp | Update time |

---

### 12.4 `orders`

| Field | Type | Description |
|---|---|---|
| `id` | string | Internal order ID |
| `account_id` | string | Account ID |
| `exchange` | string | Exchange |
| `exchange_order_id` | string | Exchange order ID |
| `client_order_id` | string | Client order ID |
| `symbol` | string | Trading pair |
| `side` | string | buy / sell |
| `type` | string | Order type |
| `status` | string | Order status |
| `price` | decimal | Price |
| `quantity` | decimal | Quantity |
| `filled_quantity` | decimal | Filled quantity |
| `remaining_quantity` | decimal | Remaining quantity |
| `average_price` | decimal | Average fill price |
| `fee` | decimal | Fee |
| `fee_asset` | string | Fee asset |
| `created_at` | timestamp | Creation time |
| `updated_at` | timestamp | Update time |

---

### 12.5 `fills`

| Field | Type | Description |
|---|---|---|
| `id` | string | Fill ID |
| `order_id` | string | Order ID |
| `account_id` | string | Account ID |
| `exchange_fill_id` | string | Exchange fill ID |
| `symbol` | string | Trading pair |
| `side` | string | buy / sell |
| `price` | decimal | Execution price |
| `quantity` | decimal | Executed quantity |
| `notional` | decimal | Executed notional |
| `fee` | decimal | Fee |
| `fee_asset` | string | Fee asset |
| `liquidity` | string | maker / taker |
| `executed_at` | timestamp | Execution time |

---

### 12.6 `ledger`

| Field | Type | Description |
|---|---|---|
| `id` | string | Ledger ID |
| `account_id` | string | Account ID |
| `asset` | string | Asset |
| `type` | string | trade / fee / adjustment |
| `amount` | decimal | Amount change |
| `balance_after` | decimal | Balance after change |
| `reference_type` | string | order / fill / adjustment |
| `reference_id` | string | Related resource ID |
| `created_at` | timestamp | Creation time |

---

### 12.7 `audit_logs`

| Field | Type | Description |
|---|---|---|
| `id` | string | Audit ID |
| `account_id` | string | Account ID |
| `actor_id` | string | Actor |
| `api_key_id` | string | API key |
| `action` | string | Action |
| `resource_type` | string | Resource type |
| `resource_id` | string | Resource ID |
| `ip_address` | string | IP address |
| `user_agent` | string | User agent |
| `request_id` | string | Request ID |
| `result` | string | success / failed |
| `metadata` | json | Additional information |
| `created_at` | timestamp | Creation time |

---

## 13. Minimum Deliverable API List

The first version should complete at least the following APIs.

---

### 13.1 Public API

```text
GET /v1/health
GET /v1/server-time
GET /v1/system/status

GET /v1/markets
GET /v1/markets/{symbol}
GET /v1/ticker
GET /v1/tickers
GET /v1/orderbook
GET /v1/trades
GET /v1/klines
```

---

### 13.2 Private Account API

```text
GET /v1/account
GET /v1/balances
GET /v1/balances/{asset}
GET /v1/ledger
GET /v1/fees
```

---

### 13.3 Order API

```text
POST /v1/orders
POST /v1/orders/test
GET /v1/orders
GET /v1/orders/{order_id}
GET /v1/orders/by-client-id/{client_order_id}
GET /v1/open-orders
POST /v1/orders/{order_id}/cancel
POST /v1/orders/by-client-id/{client_order_id}/cancel
POST /v1/orders/batch
POST /v1/orders/cancel-batch
POST /v1/orders/cancel-all
POST /v1/orders/{order_id}/replace
```

---

### 13.4 Fill API

```text
GET /v1/fills
GET /v1/fills/{fill_id}
GET /v1/fills/summary
```

---

### 13.5 Risk API

```text
GET /v1/risk/limits
PATCH /v1/risk/limits
POST /v1/risk/validate-order
GET /v1/risk/exposure
GET /v1/risk/kill-switch
POST /v1/risk/kill-switch/activate
POST /v1/risk/kill-switch/deactivate
```

---

### 13.6 Webhook API

```text
POST /v1/webhooks/endpoints
GET /v1/webhooks/endpoints
PATCH /v1/webhooks/endpoints/{webhook_id}
DELETE /v1/webhooks/endpoints/{webhook_id}
```

---

### 13.7 Audit API

```text
GET /v1/audit-logs
```

---

### 13.8 API Key API

```text
POST /v1/api-keys
GET /v1/api-keys
PATCH /v1/api-keys/{api_key_id}
POST /v1/api-keys/{api_key_id}/disable
DELETE /v1/api-keys/{api_key_id}
```

---

## 14. Recommended Development Phases

---

### Phase 1: Basic Trading API

Goal: Complete the basic spot trading flow.

Scope:

| Module | Features |
|---|---|
| Auth | API key, signature, permissions |
| Market | markets, ticker, orderbook, klines |
| Account | balances, ledger, fees |
| Order | market, limit, create, cancel, query |
| Fill | fill query |
| Risk | basic pre-trade validation |
| WebSocket | ticker, orders, fills |
| Audit | order creation, cancellation, login failure, permission changes |

---

### Phase 2: Full Trading Capability

Goal: Support a more complete order lifecycle and trading stability.

Scope:

| Module | Features |
|---|---|
| Order | batch, cancel-all, replace |
| Order Type | stop_market, stop_limit, take_profit |
| Risk | kill switch, exposure query, price deviation checks |
| Webhook | order, fill, balance, risk events |
| WebSocket | sequence, reconnect, snapshot/delta |
| Reconciliation | order and fill reconciliation |
| Rate Limit | endpoint-level rate limiting |

---

### Phase 3: Multi-exchange and Operations Management

Goal: Support multi-exchange connectivity and operational observability.

Scope:

| Module | Features |
|---|---|
| Exchange Adapter | Binance, Coinbase, Kraken, etc. |
| Symbol Mapping | Unified trading pair format |
| Exchange Status | External exchange health status |
| Smart Failover | Stop or switch routing when an exchange is abnormal |
| Audit | Complete audit log query |
| Alerting | Error and risk alerts |
| Monitoring | latency, error rate, order rejection rate |

---

## 15. Recommended System Architecture

```text
Client / Frontend / Bot
        |
        v
API Gateway
        |
        v
Auth Service
        |
        v
Trading API Service
        |
        +---------> Risk Engine
        |
        +---------> Audit Log Service
        |
        v
Order Router
        |
        v
Exchange Adapter Layer
        |
        +---------> Binance Adapter
        +---------> Coinbase Adapter
        +---------> Kraken Adapter
        |
        v
Exchange APIs

Background Workers
        |
        +---------> Order Reconciliation Worker
        +---------> Fill Sync Worker
        +---------> Balance Sync Worker
        +---------> Market Data Worker
        +---------> Webhook Dispatcher

Realtime Layer
        |
        +---------> WebSocket Service
        +---------> Event Bus
```

---

## 16. Key Implementation Requirements

---

### 16.1 Order Requests Must Be Idempotent

When the user retries the same request, duplicate orders must not be created.

The system must support:

```text
Idempotency-Key
client_order_id
```

---

### 16.2 Order Placement, Cancellation, and Fills Must Be Traceable

Each action must retain:

| Data | Description |
|---|---|
| `request_id` | API request ID |
| `client_order_id` | Client-side order ID |
| `order_id` | Internal order ID |
| `exchange_order_id` | Exchange order ID |
| `fill_id` | Fill ID |
| `api_key_id` | API key used |
| `actor_id` | Actor |
| `ip_address` | Source IP address |
| `raw_exchange_response` | Raw exchange response, stored internally |

---

### 16.3 Order Status Should Be Event-driven

Recommended order status update flow:

```text
API Create Order
        |
Risk Validation
        |
Order Accepted
        |
Send to Exchange
        |
Exchange Ack
        |
Order Open
        |
Fill Event / Cancel Event
        |
Order Updated
        |
WebSocket Push + Webhook Push + Audit Log
```

---

### 16.4 Do Not Rely Only on WebSocket

WebSocket connections may disconnect, miss events, or deliver events late. REST reconciliation must be used together with WebSocket events.

Required mechanisms:

| Mechanism | Description |
|---|---|
| WebSocket event | Real-time update |
| REST reconciliation | Periodic correction and backfill |
| Sequence check | Detect event gaps |
| Retry queue | Retry failed tasks |
| Dead letter queue | Manual handling after repeated failures |
| Unknown status | Do not assume success or failure when status is unclear |

---

## 17. Summary

The core goal of this API planning document is to build a stable, secure, and traceable cryptocurrency trading interface.

This stage does not implement withdrawals or trading strategies. It focuses on:

1. Market data queries
2. Account and balance queries
3. Order placement, cancellation, and replacement
4. Order querying
5. Fill querying
6. Risk controls and kill switch
7. WebSocket real-time pushes
8. Webhook event notifications
9. Audit logs
10. Multi-exchange abstraction and reconciliation capability

The first version should prioritize:

```text
Auth
Market Data
Account
Order
Fill
Risk
WebSocket
Audit
```

The success of a trading API does not depend on the number of endpoints. It depends on:

```text
Security
Idempotency
Order state consistency
Amount precision
Exchange failure handling
Event recovery
Audit traceability
```

If these foundations are designed correctly, strategy trading, futures trading, multi-exchange routing, and institutional-grade functions can be extended on top of this trading API in later stages.
