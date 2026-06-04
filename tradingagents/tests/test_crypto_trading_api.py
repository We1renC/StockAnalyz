import pytest
import sqlite3
import tempfile
import os
import time
import hmac
import hashlib
import json
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from fastapi.testclient import TestClient

import app
from crypto_api.auth import get_crypto_db
from crypto_api.executor import price_engine

# Helper to generate auth headers
def get_auth_headers(method: str, path: str, query: str = "", body: str = "", api_key: str = "api_key_xxx", secret: str = "secret_xxx"):
    timestamp = str(int(time.time() * 1000))
    nonce = f"nonce_{time.time()}_{hash(path)}"
    
    payload = f"{timestamp}{method.upper()}{path}{query}{body}"
    sig = hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    return {
        "X-API-Key": api_key,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": sig
    }

# HTTP POST/PATCH helpers for compact JSON serialization matching Starlette/FastAPI's TestClient
def post_json(client: TestClient, path: str, payload: dict, api_key: str = "api_key_xxx", secret: str = "secret_xxx"):
    body_str = json.dumps(payload, separators=(',', ':')) if payload else ""
    headers = get_auth_headers("POST", path, body=body_str, api_key=api_key, secret=secret)
    headers["Content-Type"] = "application/json"
    return client.post(path, headers=headers, content=body_str)

def patch_json(client: TestClient, path: str, payload: dict, api_key: str = "api_key_xxx", secret: str = "secret_xxx"):
    body_str = json.dumps(payload, separators=(',', ':')) if payload else ""
    headers = get_auth_headers("PATCH", path, body=body_str, api_key=api_key, secret=secret)
    headers["Content-Type"] = "application/json"
    return client.patch(path, headers=headers, content=body_str)

@pytest.fixture(autouse=True)
def temp_db():
    """Sets up an isolated, temporary database for testing the Crypto API."""
    fd, temp_db_path = tempfile.mkstemp()
    os.close(fd)
    
    # Patch the global DB path in both app.py and our modules
    with patch("app.DB", Path(temp_db_path)), \
         patch("crypto_api.auth.DB", Path(temp_db_path)), \
         patch("crypto_api.executor.DB", Path(temp_db_path)), \
         patch("crypto_api.webhooks.DB", Path(temp_db_path)), \
         patch("crypto_api.ws.DB", Path(temp_db_path)):
        
        # Initialize and seed database
        app.init_db()
        
        # Adjust price deviation limit for tests so they don't get blocked by default
        conn = sqlite3.connect(temp_db_path)
        conn.execute("UPDATE crypto_risk_limits SET max_price_deviation_percent = '100'")
        conn.commit()
        conn.close()
        
        yield temp_db_path
        
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)

def test_public_system_endpoints():
    client = TestClient(app.app)
    
    # 1. Health check
    res = client.get("/v1/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    
    # 2. Server time
    res = client.get("/v1/server-time")
    assert res.status_code == 200
    assert "server_time" in res.json()
    assert "server_time_ms" in res.json()
    
    # 3. System status
    res = client.get("/v1/system/status")
    assert res.status_code == 200
    assert res.json()["trading_status"] == "trading"

def test_public_market_data():
    client = TestClient(app.app)
    
    # 1. Markets list
    res = client.get("/v1/markets")
    assert res.status_code == 200
    assert res.json()["success"] is True
    markets = res.json()["data"]
    assert len(markets) >= 3
    assert any(m["symbol"] == "BTC-USDT" for m in markets)
    
    # 2. Single market
    res = client.get("/v1/markets/BTC-USDT")
    assert res.status_code == 200
    assert res.json()["base_asset"] == "BTC"
    assert res.json()["quote_asset"] == "USDT"
    
    # 3. Non-existent market
    res = client.get("/v1/markets/XYZ-USDT")
    assert res.status_code == 404
    assert res.json()["detail"]["error"]["code"] == "INVALID_SYMBOL"

    # 4. Ticker
    res = client.get("/v1/ticker?symbol=BTC-USDT")
    assert res.status_code == 200
    assert res.json()["symbol"] == "BTC-USDT"
    assert "last_price" in res.json()

    # 5. Orderbook
    res = client.get("/v1/orderbook?symbol=BTC-USDT&depth=5")
    assert res.status_code == 200
    assert res.json()["symbol"] == "BTC-USDT"
    assert len(res.json()["bids"]) > 0

    # 6. Recent trades
    res = client.get("/v1/trades?symbol=BTC-USDT")
    assert res.status_code == 200
    assert len(res.json()["data"]) > 0

    # 7. Candlesticks (klines)
    res = client.get("/v1/klines?symbol=BTC-USDT&interval=1m&limit=5")
    assert res.status_code == 200
    assert len(res.json()["data"]) > 0

def test_authentication_failures():
    client = TestClient(app.app)
    path = "/v1/account"
    
    # 1. Missing headers
    res = client.get(path)
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["code"] == "UNAUTHORIZED"
    
    # 2. Invalid Key
    headers = get_auth_headers("GET", path, api_key="invalid_key")
    res = client.get(path, headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["code"] == "INVALID_API_KEY"

    # 3. Clock drift / expired timestamp
    headers = get_auth_headers("GET", path)
    headers["X-Timestamp"] = str(int(time.time() * 1000) - 40000) # 40s ago
    res = client.get(path, headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["code"] == "TIMESTAMP_EXPIRED"

    # 4. Nonce replay
    headers = get_auth_headers("GET", path)
    res1 = client.get(path, headers=headers)
    assert res1.status_code == 200
    res2 = client.get(path, headers=headers) # Send again with same nonce
    assert res2.status_code == 401
    assert res2.json()["detail"]["error"]["code"] == "NONCE_REPLAYED"

    # 5. Invalid signature
    headers = get_auth_headers("GET", path)
    headers["X-Signature"] = "wrong_signature"
    res = client.get(path, headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"]["error"]["code"] == "INVALID_SIGNATURE"

def test_private_account_api():
    client = TestClient(app.app)
    
    # 1. Account info
    path = "/v1/account"
    headers = get_auth_headers("GET", path)
    res = client.get(path, headers=headers)
    assert res.status_code == 200
    assert res.json()["account_id"] == "acct_123"

    # 2. Balances list
    path = "/v1/balances"
    headers = get_auth_headers("GET", path)
    res = client.get(path, headers=headers)
    assert res.status_code == 200
    bals = res.json()["data"]
    assert len(bals) > 0
    usdt_bal = [b for b in bals if b["asset"] == "USDT"][0]
    assert Decimal(usdt_bal["available"]) == Decimal("100000.00")

    # 3. Single balance
    path = "/v1/balances/BTC"
    headers = get_auth_headers("GET", path)
    res = client.get(path, headers=headers)
    assert res.status_code == 200
    assert res.json()["asset"] == "BTC"
    assert Decimal(res.json()["available"]) == Decimal("2.0")

    # 4. Fees
    path = "/v1/fees"
    headers = get_auth_headers("GET", path)
    res = client.get(path, headers=headers)
    assert res.status_code == 200
    assert len(res.json()["data"]) >= 3

def test_orders_creation_and_balance_locking():
    client = TestClient(app.app)
    path = "/v1/orders"
    
    # 1. Buy Limit Order (0.01 BTC at 60000.00 USDT -> 600 USDT locked)
    payload = {
        "client_order_id": "test-buy-001",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "60000.00",
        "quantity": "0.01"
    }
    res = post_json(client, path, payload)
    assert res.status_code == 200
    assert res.json()["status"] == "pending" or res.json()["status"] == "open"
    assert res.json()["client_order_id"] == "test-buy-001"
    order_id = res.json()["id"]

    # Check database: USDT balance locked
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_balances WHERE account_id = 'acct_123' AND asset = 'USDT'").fetchone()
    assert Decimal(row["available"]) == Decimal("99400.00") # 100000 - 600
    assert Decimal(row["locked"]) == Decimal("600.00")
    conn.close()

    # 2. Idempotency Check (sending same payload returns same order without creating a new one)
    res2 = post_json(client, path, payload)
    assert res2.status_code == 200
    assert res2.json()["id"] == order_id

    # 3. Cancel Order
    cancel_path = f"/v1/orders/{order_id}/cancel"
    res_cancel = post_json(client, cancel_path, {})
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "cancelled"

    # Check database: USDT unlocked
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_balances WHERE account_id = 'acct_123' AND asset = 'USDT'").fetchone()
    assert Decimal(row["available"]) == Decimal("100000.00")
    assert Decimal(row["locked"]) == Decimal("0.00")
    conn.close()

def test_pre_trade_risk_validation():
    client = TestClient(app.app)
    path = "/v1/orders"
    
    # 1. Exceed single order limit (default 10,000 USDT)
    payload = {
        "client_order_id": "test-buy-risk-001",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "60000.00",
        "quantity": "0.2" # 0.2 * 60000 = 12000 USDT (exceeds 10000 limit)
    }
    res = post_json(client, path, payload)
    assert res.status_code == 400
    assert res.json()["detail"]["error"]["code"] == "MAX_ORDER_NOTIONAL_EXCEEDED"

    # 2. Insufficient balance check (selling more SOL than account has available, keeping notional under 10k)
    payload2 = {
        "client_order_id": "test-buy-risk-002",
        "symbol": "SOL-USDT",
        "side": "sell",
        "type": "limit",
        "price": "40.00",
        "quantity": "200.0" # 200 * 40 = 8000 USDT (limit: 10000), available SOL: 100
    }
    res2 = post_json(client, path, payload2)
    assert res2.status_code == 400
    assert res2.json()["detail"]["error"]["code"] == "INSUFFICIENT_BALANCE"

    # 3. Price deviation check (limit price 75000 is >5% deviation from BTC reference price 68000)
    res_patch_dev = patch_json(client, "/v1/risk/limits", {"max_price_deviation_percent": "5"})
    assert res_patch_dev.status_code == 200
    price_engine.price_cache["BTC-USDT"] = "68000.00"
    payload3 = {
        "client_order_id": "test-buy-risk-003",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "75000.00",
        "quantity": "0.01" # 750 USDT (well under 10000 max single order)
    }
    res3 = post_json(client, path, payload3)
    assert res3.status_code == 400
    assert res3.json()["detail"]["error"]["code"] == "PRICE_DEVIATION_LIMIT_EXCEEDED"

    # 4. Max daily notional limit check
    # Patch daily limit to 9000 USDT and reset price deviation to 100
    patch_payload = {
        "max_daily_notional": "9000",
        "max_price_deviation_percent": "100"
    }
    res_patch = patch_json(client, "/v1/risk/limits", patch_payload)
    assert res_patch.status_code == 200

    # Place order 1: 5000 USDT (succeeds)
    payload4 = {
        "client_order_id": "test-buy-risk-004",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "5000.00",
        "quantity": "1.0"
    }
    res4 = post_json(client, path, payload4)
    assert res4.status_code == 200

    # Place order 2: 5000 USDT (fails because 5000 + 5000 = 10000 > 9000 daily limit)
    payload5 = {
        "client_order_id": "test-buy-risk-005",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "5000.00",
        "quantity": "1.0"
    }
    res5 = post_json(client, path, payload5)
    assert res5.status_code == 400
    assert res5.json()["detail"]["error"]["code"] == "MAX_DAILY_NOTIONAL_EXCEEDED"

@pytest.mark.anyio
async def test_matching_engine_execution():
    client = TestClient(app.app)
    path = "/v1/orders"
    
    # Override price engine cached price for BTC-USDT to 68000.00
    price_engine.price_cache["BTC-USDT"] = "68000.00"
    
    # 1. Limit Buy Order placed at 69000.00 (cross-matched with market price of 68000.00)
    # E.g. placing an order that should instantly match
    payload = {
        "client_order_id": "test-buy-match-001",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "69000.00",
        "quantity": "0.01"
    }
    res = post_json(client, path, payload)
    assert res.status_code == 200
    order_id = res.json()["id"]

    # Trigger manual execution matching for this order to speed up the test
    # (instead of waiting for the background thread)
    conn = get_crypto_db()
    from crypto_api.executor import fill_order
    fill_order(conn, order_id, Decimal("68000.00"), Decimal("0.01"), is_maker=True)
    conn.close()

    # Query order state: should be filled
    query_path = f"/v1/orders/{order_id}"
    headers = get_auth_headers("GET", query_path)
    res_query = client.get(query_path, headers=headers)
    assert res_query.status_code == 200
    assert res_query.json()["status"] == "filled"
    assert Decimal(res_query.json()["filled_quantity"]) == Decimal("0.01")
    assert Decimal(res_query.json()["average_price"]) == Decimal("68000.00")

    # Check final balances: BTC should increase by 0.01, USDT should decrease by 680 + fee (680 * 0.001 = 0.68)
    conn = get_crypto_db()
    c = conn.cursor()
    btc_bal = c.execute("SELECT * FROM crypto_balances WHERE account_id = 'acct_123' AND asset = 'BTC'").fetchone()
    usdt_bal = c.execute("SELECT * FROM crypto_balances WHERE account_id = 'acct_123' AND asset = 'USDT'").fetchone()
    
    assert Decimal(btc_bal["available"]) == Decimal("2.01") # 2.0 + 0.01
    # 100000 - 680 - 0.68 = 99319.32
    assert Decimal(usdt_bal["available"]) == Decimal("99319.32")
    conn.close()

def test_orders_replaces_and_cancels():
    client = TestClient(app.app)
    path = "/v1/orders"
    
    # 1. Create order
    payload = {
        "client_order_id": "test-rep-001",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "60000.00",
        "quantity": "0.01"
    }
    res = post_json(client, path, payload)
    order_id = res.json()["id"]

    # 2. Replace order
    replace_path = f"/v1/orders/{order_id}/replace"
    replace_payload = {
        "new_client_order_id": "test-rep-002",
        "price": "61000.00",
        "quantity": "0.015"
    }
    res_rep = post_json(client, replace_path, replace_payload)
    assert res_rep.status_code == 200
    
    new_order_id = res_rep.json()["new_order_id"]
    
    # Verify old order cancelled, new order created
    conn = get_crypto_db()
    c = conn.cursor()
    old_o = c.execute("SELECT status FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    new_o = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (new_order_id,)).fetchone()
    assert old_o["status"] == "cancelled"
    assert new_o["status"] == "open" or new_o["status"] == "pending"
    assert Decimal(new_o["price"]) == Decimal("61000.00")
    assert Decimal(new_o["quantity"]) == Decimal("0.015")
    conn.close()

def test_kill_switch_blocking():
    client = TestClient(app.app)
    
    # 1. Activate Kill Switch
    act_path = "/v1/risk/kill-switch/activate"
    payload = {"reason": "test emergency", "cancel_all_orders": True}
    res_act = post_json(client, act_path, payload)
    assert res_act.status_code == 200
    assert res_act.json()["active"] is True

    # Check status
    res_status = client.get("/v1/system/status")
    assert res_status.json()["trading_status"] == "halted"

    # 2. Attempt placing order (should be rejected)
    order_path = "/v1/orders"
    order_payload = {
        "client_order_id": "test-kill-001",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "60000.00",
        "quantity": "0.01"
    }
    res_order = post_json(client, order_path, order_payload)
    assert res_order.status_code == 423
    assert res_order.json()["detail"]["error"]["code"] == "KILL_SWITCH_ACTIVE"

    # 3. Deactivate Kill Switch
    deact_path = "/v1/risk/kill-switch/deactivate"
    res_deact = post_json(client, deact_path, {})
    assert res_deact.status_code == 200
    assert res_deact.json()["active"] is False

    # Check status restored
    res_status2 = client.get("/v1/system/status")
    assert res_status2.json()["trading_status"] == "trading"

def test_webhook_endpoints_crud():
    client = TestClient(app.app)
    path = "/v1/webhooks/endpoints"
    
    # 1. Create Webhook
    payload = {
        "url": "https://example.com/callback",
        "events": ["order.updated", "fill.created"],
        "secret": "mysecret",
        "status": "active"
    }
    res = post_json(client, path, payload)
    assert res.status_code == 200
    wh_id = res.json()["webhook_id"]

    # 2. Query list
    headers = get_auth_headers("GET", path)
    res_list = client.get(path, headers=headers)
    assert res_list.status_code == 200
    assert len(res_list.json()["data"]) > 0

    # 3. Update Webhook
    patch_path = f"/v1/webhooks/endpoints/{wh_id}"
    patch_payload = {"events": ["order.created"], "status": "disabled"}
    res_patch = patch_json(client, patch_path, patch_payload)
    assert res_patch.status_code == 200
    assert res_patch.json()["updated"] is True

    # 4. Delete Webhook
    del_path = f"/v1/webhooks/endpoints/{wh_id}"
    headers_del = get_auth_headers("DELETE", del_path)
    res_del = client.delete(del_path, headers=headers_del)
    assert res_del.status_code == 200
    assert res_del.json()["deleted"] is True

def test_api_keys_management():
    client = TestClient(app.app)
    path = "/v1/api-keys"
    
    # 1. Create API key
    payload = {
        "name": "new-key",
        "scopes": ["read:market", "trade:spot", "read:account"],
        "ip_whitelist": ["127.0.0.1"]
    }
    res = post_json(client, path, payload)
    assert res.status_code == 200
    key_id = res.json()["api_key_id"]
    new_api_key = res.json()["api_key"]
    new_api_secret = res.json()["api_secret"]

    # Test key works!
    test_path = "/v1/account"
    headers_new_key = get_auth_headers("GET", test_path, api_key=new_api_key, secret=new_api_secret)
    res_test = client.get(test_path, headers=headers_new_key)
    assert res_test.status_code == 200
    assert res_test.json()["account_id"] == "acct_123"

    # 2. Disable key
    disable_path = f"/v1/api-keys/{key_id}/disable"
    res_disable = post_json(client, disable_path, {})
    assert res_disable.status_code == 200
    assert res_disable.json()["status"] == "disabled"

    # Test key no longer works (status disabled -> 403 Forbidden)
    res_test2 = client.get(test_path, headers=headers_new_key)
    assert res_test2.status_code == 403

    # 3. Delete key
    del_path = f"/v1/api-keys/{key_id}"
    headers_del = get_auth_headers("DELETE", del_path)
    res_del = client.delete(del_path, headers=headers_del)
    assert res_del.status_code == 200
    assert res_del.json()["deleted"] is True

def test_websocket_connectivity():
    client = TestClient(app.app)
    
    with client.websocket_connect("/ws/v1") as ws:
        # Send auth message
        timestamp = str(int(time.time() * 1000))
        nonce = "ws_test_nonce"
        payload = f"{timestamp}{nonce}"
        sig = hmac.new(
            b"secret_xxx",
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        ws.send_json({
            "op": "auth",
            "api_key": "api_key_xxx",
            "timestamp": timestamp,
            "nonce": nonce,
            "signature": sig
        })
        
        auth_res = ws.receive_json()
        assert auth_res["op"] == "auth"
        assert auth_res["success"] is True
        
        # Subscribe to public channel
        ws.send_json({
            "op": "subscribe",
            "channels": [{"name": "ticker", "symbols": ["BTC-USDT"]}]
        })
        
        sub_res = ws.receive_json()
        assert sub_res["op"] == "subscribed"
        assert "ticker" in sub_res["subscribed"]


def test_binance_compatibility_api():
    client = TestClient(app.app)
    
    # 1. Ping
    res = client.get("/api/v3/ping")
    assert res.status_code == 200
    assert res.json() == {}
    
    # 2. Time
    res = client.get("/api/v3/time")
    assert res.status_code == 200
    assert "serverTime" in res.json()
    
    # 3. ExchangeInfo
    res = client.get("/api/v3/exchangeInfo?symbol=BTCUSDT")
    assert res.status_code == 200
    data = res.json()
    assert "symbols" in data
    assert len(data["symbols"]) == 1
    assert data["symbols"][0]["symbol"] == "BTCUSDT"
    assert data["symbols"][0]["baseAsset"] == "BTC"
    assert data["symbols"][0]["quoteAsset"] == "USDT"

    # 4. Depth
    res = client.get("/api/v3/depth?symbol=BTCUSDT&limit=5")
    assert res.status_code == 200
    assert "bids" in res.json()
    assert "asks" in res.json()

    # 5. Trades
    res = client.get("/api/v3/trades?symbol=BTCUSDT&limit=5")
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    # 6. Klines
    res = client.get("/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=5")
    assert res.status_code == 200
    assert isinstance(res.json(), list)

    # 7. Ticker price
    res = client.get("/api/v3/ticker/price?symbol=BTCUSDT")
    assert res.status_code == 200
    assert res.json()["symbol"] == "BTCUSDT"
    assert "price" in res.json()

    # 8. Book Ticker
    res = client.get("/api/v3/ticker/bookTicker?symbol=BTCUSDT")
    assert res.status_code == 200
    assert res.json()["symbol"] == "BTCUSDT"
    assert "bidPrice" in res.json()

    # 9. Private Account Information (signed)
    def get_binance_signed_headers(query_params: dict, body_params: dict = {}, api_key: str = "api_key_xxx", secret: str = "secret_xxx"):
        timestamp = str(int(time.time() * 1000))
        query_params["timestamp"] = timestamp
        
        q_parts = []
        for k, v in query_params.items():
            q_parts.append(f"{k}={v}")
        query_str = "&".join(q_parts)
        
        b_parts = []
        for k, v in body_params.items():
            b_parts.append(f"{k}={v}")
        body_str = "&".join(b_parts)
        
        total_params = query_str
        if body_str:
            total_params = f"{query_str}&{body_str}"
            
        sig = hmac.new(
            secret.encode('utf-8'),
            total_params.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        query_str_with_sig = f"{query_str}&signature={sig}"
        
        return query_str_with_sig, {
            "X-MBX-APIKEY": api_key
        }

    # Test GET account details
    q_str, headers = get_binance_signed_headers({})
    res = client.get(f"/api/v3/account?{q_str}", headers=headers)
    assert res.status_code == 200
    assert "balances" in res.json()
    assert any(b["asset"] == "BTC" for b in res.json()["balances"])

    # Test POST order (test)
    q_str, headers = get_binance_signed_headers({
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "quantity": "0.01",
        "price": "60000.00",
        "timeInForce": "GTC"
    })
    res = client.post(f"/api/v3/order/test?{q_str}", headers=headers)
    assert res.status_code == 200

    # Test POST order (real placement)
    q_str, headers = get_binance_signed_headers({
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "quantity": "0.01",
        "price": "60000.00",
        "timeInForce": "GTC",
        "newClientOrderId": "bin_test_001"
    })
    res = client.post(f"/api/v3/order?{q_str}", headers=headers)
    assert res.status_code == 200
    order_id = res.json()["orderId"]
    assert res.json()["clientOrderId"] == "bin_test_001"
    assert res.json()["status"] == "NEW"

    # Query order
    q_str, headers = get_binance_signed_headers({
        "symbol": "BTCUSDT",
        "orderId": order_id
    })
    res = client.get(f"/api/v3/order?{q_str}", headers=headers)
    assert res.status_code == 200
    assert res.json()["orderId"] == order_id
    assert res.json()["status"] == "NEW"

    # Query open orders
    q_str, headers = get_binance_signed_headers({"symbol": "BTCUSDT"})
    res = client.get(f"/api/v3/openOrders?{q_str}", headers=headers)
    assert res.status_code == 200
    assert len(res.json()) >= 1
    assert any(o["orderId"] == order_id for o in res.json())

    # Cancel order
    q_str, headers = get_binance_signed_headers({
        "symbol": "BTCUSDT",
        "orderId": order_id
    })
    res = client.delete(f"/api/v3/order?{q_str}", headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELED"

    # Query all orders
    q_str, headers = get_binance_signed_headers({"symbol": "BTCUSDT"})
    res = client.get(f"/api/v3/allOrders?{q_str}", headers=headers)
    assert res.status_code == 200
    assert len(res.json()) >= 1

    # Query my trades
    q_str, headers = get_binance_signed_headers({"symbol": "BTCUSDT"})
    res = client.get(f"/api/v3/myTrades?{q_str}", headers=headers)
    assert res.status_code == 200


def test_exchange_adapter_management_api():
    client = TestClient(app.app)
    
    # 1. Query Exchange List
    headers = get_auth_headers("GET", "/v1/exchanges")
    res = client.get("/v1/exchanges", headers=headers)
    assert res.status_code == 200
    assert res.json()["success"] is True
    data = res.json()["data"]
    assert any(x["exchange"] == "binance" for x in data)
    assert any(x["exchange"] == "simulated" for x in data)

    # 2. Query Single Exchange Status (Binance)
    headers = get_auth_headers("GET", "/v1/exchanges/binance/status")
    res = client.get("/v1/exchanges/binance/status", headers=headers)
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["data"]["exchange"] == "binance"

    # 3. Query Single Exchange Status (Simulated)
    headers = get_auth_headers("GET", "/v1/exchanges/simulated/status")
    res = client.get("/v1/exchanges/simulated/status", headers=headers)
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["data"]["exchange"] == "simulated"

    # 4. Query Exchange Symbol Mapping
    headers = get_auth_headers("GET", "/v1/exchanges/binance/symbols")
    res = client.get("/v1/exchanges/binance/symbols", headers=headers)
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["exchange"] == "binance"
    assert len(res.json()["data"]) >= 3
    assert any(sym["internal_symbol"] == "BTC-USDT" and sym["exchange_symbol"] == "BTCUSDT" for sym in res.json()["data"])

    # 5. Invalid Exchange query -> 404
    headers = get_auth_headers("GET", "/v1/exchanges/invalid_exchange/status")
    res = client.get("/v1/exchanges/invalid_exchange/status", headers=headers)
    assert res.status_code == 404


def test_reconciliation_api():
    client = TestClient(app.app)
    
    # 1. Create a limit order
    payload = {
        "client_order_id": "recon_test_001",
        "symbol": "BTC-USDT",
        "side": "buy",
        "type": "limit",
        "price": "60000.00",
        "quantity": "0.01"
    }
    res_order = post_json(client, "/v1/orders", payload)
    assert res_order.status_code == 200
    order_id = res_order.json()["id"]

    # Check initially no issues
    headers = get_auth_headers("GET", "/v1/internal/reconciliation/issues")
    res_issues = client.get("/v1/internal/reconciliation/issues", headers=headers)
    assert res_issues.status_code == 200
    assert res_issues.json()["total_issues"] == 0

    # 2. Inject a fake fill into DB using sqlite directly
    conn = get_crypto_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO crypto_fills (id, order_id, account_id, client_order_id, symbol, side, price, quantity, notional, fee, fee_asset, liquidity, executed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("fill_recon_001", order_id, "acct_123", "ex_fill_recon_1", "BTC-USDT", "buy", "60000.00", "0.005", "300.00", "0.00001", "BTC", "maker", "2026-06-04T12:00:00Z"))
    conn.commit()
    conn.close()

    # 3. Check issues again, should have 1 issue (FILLED_QUANTITY_MISMATCH)
    headers_issues2 = get_auth_headers("GET", "/v1/internal/reconciliation/issues")
    res_issues2 = client.get("/v1/internal/reconciliation/issues", headers=headers_issues2)
    assert res_issues2.status_code == 200
    assert res_issues2.json()["total_issues"] == 1
    assert res_issues2.json()["issues"][0]["type"] == "FILLED_QUANTITY_MISMATCH"
    assert res_issues2.json()["issues"][0]["order_id"] == order_id

    # 4. Trigger reconciliation for this order
    headers_post = get_auth_headers("POST", f"/v1/internal/reconciliation/orders/{order_id}")
    res_recon = client.post(f"/v1/internal/reconciliation/orders/{order_id}", headers=headers_post)
    assert res_recon.status_code == 200
    assert res_recon.json()["corrected"] is True
    assert res_recon.json()["new_status"] == "partially_filled"
    assert res_recon.json()["new_filled"] == "0.005"

    # 5. Check issues again, should be 0 issues now
    headers = get_auth_headers("GET", "/v1/internal/reconciliation/issues")
    res_issues3 = client.get("/v1/internal/reconciliation/issues", headers=headers)
    assert res_issues3.status_code == 200
    assert res_issues3.json()["total_issues"] == 0

    # 6. Run batch reconciliation
    headers_batch = get_auth_headers("POST", "/v1/internal/reconciliation/fills")
    res_batch = client.post("/v1/internal/reconciliation/fills", headers=headers_batch)
    assert res_batch.status_code == 200
    assert res_batch.json()["success"] is True


def test_rate_limiting_api():
    from crypto_api.auth import global_rate_limiter
    global_rate_limiter.history.clear()
    client = TestClient(app.app)
    
    # Call query_orders (limit 60) up to 65 times to trigger rate limit
    for i in range(65):
        headers = get_auth_headers("GET", "/v1/orders")
        res = client.get("/v1/orders", headers=headers)
        if i >= 60:
            assert res.status_code == 429
            assert "X-RateLimit-Limit" in res.headers
            assert res.headers["X-RateLimit-Remaining"] == "0"
            assert "X-RateLimit-Reset" in res.headers
            assert res.json()["detail"]["success"] is False
            assert res.json()["detail"]["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            break
        else:
            assert res.status_code == 200




