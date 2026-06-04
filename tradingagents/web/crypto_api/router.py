import sqlite3
import json
import time
import hashlib
import asyncio
from datetime import datetime, UTC
from typing import List, Optional, Dict, Any
from decimal import Decimal
from uuid import uuid4
import urllib.parse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

# Imports from auth, models and executor
from crypto_api.auth import (
    authenticate_request,
    require_scopes,
    get_crypto_db,
    normalize_symbol,
    authenticate_binance_request,
    require_binance_scopes
)
from crypto_api.executor import price_engine, validate_pre_trade_risk, fill_order, cancel_single_order_sync
from crypto_api.ws import ws_manager
from crypto_api.webhooks import dispatch_webhook

router = APIRouter(prefix="/v1", tags=["Cryptocurrency Trading API"])
binance_router = APIRouter(prefix="/api/v3", tags=["Binance Spot API Compatibility Layer"])

# ─────────────── Pydantic Request Models ───────────────

class CreateOrderRequest(BaseModel):
    client_order_id: str = Field(..., description="Unique client side order ID")
    symbol: str = Field(..., description="Trading pair, e.g. BTC-USDT")
    side: str = Field(..., description="buy or sell")
    type: str = Field(..., description="limit, market, stop_limit, stop_market, etc.")
    price: Optional[str] = Field(None, description="Price for limit/conditional orders")
    quantity: str = Field(..., description="Order quantity")
    quote_quantity: Optional[str] = Field(None, description="Quote asset quantity for market orders")
    stop_price: Optional[str] = Field(None, description="Stop trigger price")
    time_in_force: Optional[str] = "GTC"
    post_only: Optional[bool] = False
    self_trade_prevention: Optional[str] = "cancel_newest"

class BatchOrderRequest(BaseModel):
    orders: List[CreateOrderRequest]

class CancelBatchRequest(BaseModel):
    order_ids: List[str]

class CancelAllRequest(BaseModel):
    symbol: Optional[str] = None
    reason: Optional[str] = "manual_cancel_all"

class ReplaceOrderRequest(BaseModel):
    new_client_order_id: str
    price: str
    quantity: str

class CreateApiKeyRequest(BaseModel):
    name: str
    scopes: List[str]
    ip_whitelist: List[str]
    expires_at: Optional[str] = None

class UpdateApiKeyRequest(BaseModel):
    name: Optional[str] = None
    scopes: Optional[List[str]] = None
    ip_whitelist: Optional[List[str]] = None
    status: Optional[str] = None

class UpdateRiskLimitsRequest(BaseModel):
    max_single_order_notional: Optional[str] = None
    max_daily_notional: Optional[str] = None
    max_open_orders: Optional[int] = None
    max_price_deviation_percent: Optional[str] = None
    allowed_symbols: Optional[List[str]] = None

class KillSwitchRequest(BaseModel):
    reason: str
    cancel_all_orders: Optional[bool] = True

class CreateWebhookRequest(BaseModel):
    url: str
    events: List[str]
    secret: str
    status: Optional[str] = "active"

class UpdateWebhookRequest(BaseModel):
    events: Optional[List[str]] = None
    status: Optional[str] = None

# Helper to log trading audits
def write_audit_log(conn: sqlite3.Connection, account_id: str, actor_id: str, action: str, resource_type: str, resource_id: Optional[str], ip: str, ua: str, result: str, metadata: Dict[str, Any] = {}):
    c = conn.cursor()
    audit_id = f"audit_{uuid4().hex[:12]}"
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    c.execute("""
        INSERT INTO crypto_audit_logs (id, account_id, actor_id, action, resource_type, resource_id, ip_address, user_agent, result, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (audit_id, account_id, actor_id, action, resource_type, resource_id, ip, ua, result, json.dumps(metadata), now_iso))
    conn.commit()


# ─────────────── 13.1 Public System & Market Data API ───────────────

@router.get("/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
    }

@router.get("/server-time")
def server_time():
    now_ms = int(time.time() * 1000)
    return {
        "server_time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "server_time_ms": now_ms
    }

@router.get("/system/status")
def system_status(request: Request):
    # Check kill switch state
    conn = get_crypto_db()
    c = conn.cursor()
    ks = c.execute("SELECT active, reason, activated_at FROM crypto_kill_switch WHERE id = 1").fetchone()
    conn.close()

    if ks and ks["active"] == 1:
        return {
            "status": "degraded",
            "trading_status": "halted",
            "message": f"Trading halted due to kill switch: {ks['reason']}",
            "updated_at": ks["activated_at"]
        }
        
    return {
        "status": "online",
        "trading_status": "trading",
        "message": None,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
    }

@router.get("/markets")
def query_markets(status: Optional[str] = None, base_asset: Optional[str] = None, quote_asset: Optional[str] = None):
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_markets WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if base_asset:
        query += " AND base_asset = ?"
        params.append(base_asset)
    if quote_asset:
        query += " AND quote_asset = ?"
        params.append(quote_asset)
        
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    return {
        "success": True,
        "data": [dict(r) for r in rows],
        "request_id": f"req_{uuid4().hex[:12]}"
    }

@router.get("/markets/{symbol}")
def query_single_market(symbol: str):
    symbol = normalize_symbol(symbol)
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_markets WHERE symbol = ?", (symbol,)).fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_SYMBOL",
                    "message": f"Market symbol '{symbol}' not found."
                }
            }
        )
        
    market = dict(row)
    # Add allowed types & time_in_force constraints dynamically
    market["allowed_order_types"] = ["market", "limit", "stop_market", "stop_limit", "take_profit_market", "take_profit_limit"]
    market["allowed_time_in_force"] = ["GTC", "IOC", "FOK"]
    
    return market

@router.get("/ticker")
async def query_ticker(symbol: str):
    symbol = normalize_symbol(symbol)
    ticker = await price_engine.get_ticker_24h(symbol)
    return ticker

@router.get("/tickers")
async def query_tickers(symbols: Optional[str] = None):
    # symbols can be comma-separated list
    sym_list = [normalize_symbol(s) for s in symbols.split(",")] if symbols else ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    
    tickers = []
    for sym in sym_list:
        try:
            ticker = await price_engine.get_ticker_24h(sym)
            tickers.append({
                "symbol": sym,
                "last_price": ticker["last_price"],
                "volume_24h": ticker["volume_24h"],
                "price_change_percent_24h": ticker["price_change_percent_24h"],
                "timestamp": ticker["timestamp"]
            })
        except Exception:
            pass
            
    return {
        "success": True,
        "data": tickers,
        "request_id": f"req_{uuid4().hex[:12]}"
    }

@router.get("/orderbook")
async def query_orderbook(symbol: str, depth: int = 50):
    symbol = normalize_symbol(symbol)
    return await price_engine.get_orderbook(symbol, depth)

@router.get("/trades")
async def query_recent_trades(symbol: str, limit: int = 100):
    symbol = normalize_symbol(symbol)
    trades = await price_engine.get_recent_trades(symbol, limit)
    return {
        "success": True,
        "data": trades,
        "request_id": f"req_{uuid4().hex[:12]}"
    }

@router.get("/klines")
async def query_klines(symbol: str, interval: str = "1m", limit: int = 500):
    symbol = normalize_symbol(symbol)
    klines = await price_engine.get_klines(symbol, interval, limit)
    return {
        "symbol": symbol,
        "interval": interval,
        "data": klines
    }


# ─────────────── 13.2 Private Account API ───────────────

@router.get("/account")
def query_account(auth_info: Dict[str, Any] = Depends(require_scopes(["read:account"]))):
    return {
        "account_id": auth_info["account_id"],
        "account_type": "spot",
        "status": "active",
        "trading_enabled": True,
        "created_at": "2026-06-04T10:00:00Z"
    }

@router.get("/balances")
def query_balances(asset: Optional[str] = None, hide_zero: bool = True, auth_info: Dict[str, Any] = Depends(require_scopes(["read:account"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_balances WHERE account_id = ?"
    params = [auth_info["account_id"]]
    if asset:
        query += " AND asset = ?"
        params.append(asset)
        
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    data = []
    for r in rows:
        bal = dict(r)
        if hide_zero and Decimal(bal["total"]) == Decimal("0"):
            continue
        data.append({
            "asset": bal["asset"],
            "available": bal["available"],
            "locked": bal["locked"],
            "total": bal["total"],
            "updated_at": bal["updated_at"]
        })
        
    return {
        "success": True,
        "data": data,
        "request_id": f"req_{uuid4().hex[:12]}"
    }

@router.get("/balances/{asset}")
def query_single_balance(asset: str, auth_info: Dict[str, Any] = Depends(require_scopes(["read:account"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute(
        "SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?",
        (auth_info["account_id"], asset)
    ).fetchone()
    conn.close()
    
    if not row:
        return {
            "asset": asset,
            "available": "0.00",
            "locked": "0.00",
            "total": "0.00",
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
        }
        
    return dict(row)

@router.get("/ledger")
def query_ledger(asset: Optional[str] = None, type: Optional[str] = None, limit: int = 100, auth_info: Dict[str, Any] = Depends(require_scopes(["read:account"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_ledger WHERE account_id = ?"
    params = [auth_info["account_id"]]
    
    if asset:
        query += " AND asset = ?"
        params.append(asset)
    if type:
        query += " AND type = ?"
        params.append(type)
        
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    return {
        "success": True,
        "data": [dict(r) for r in rows],
        "pagination": {
            "limit": limit,
            "next_cursor": None,
            "has_more": False
        }
    }

@router.get("/fees")
def query_fees(symbol: Optional[str] = None, auth_info: Dict[str, Any] = Depends(require_scopes(["read:account"]))):
    if symbol:
        symbol = normalize_symbol(symbol)
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT symbol, maker_fee_rate, taker_fee_rate FROM crypto_markets"
    params = []
    if symbol:
        query += " WHERE symbol = ?"
        params.append(symbol)
        
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    data = []
    for r in rows:
        data.append({
            "symbol": r["symbol"],
            "maker_fee_rate": r["maker_fee_rate"],
            "taker_fee_rate": r["taker_fee_rate"],
            "fee_tier": "standard",
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
        })
        
    return {
        "success": True,
        "data": data
    }


# ─────────────── 13.3 Order API ───────────────

@router.post("/orders")
async def create_order(
    request: Request,
    req_body: CreateOrderRequest,
    auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))
):
    req_body.symbol = normalize_symbol(req_body.symbol)
    account_id = auth_info["account_id"]
    client_ip = auth_info["client_ip"]
    ua = request.headers.get("user-agent", "")

    conn = get_crypto_db()
    c = conn.cursor()
    
    # 1. Idempotency Check / client_order_id replay check
    existing = c.execute(
        "SELECT * FROM crypto_orders WHERE account_id = ? AND client_order_id = ?",
        (account_id, req_body.client_order_id)
    ).fetchone()
    
    if existing:
        conn.close()
        # Return existing order instead of double execution
        return dict(existing)

    # 2. Risk check and balance locks
    order_dict = req_body.model_dump()
    passed, err_code, meta = await validate_pre_trade_risk(conn, account_id, order_dict)
    if not passed:
        write_audit_log(conn, account_id, auth_info["api_key_id"], "order.create", "order", None, client_ip, ua, "failed", {"error": err_code, "request": order_dict})
        conn.close()
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": err_code or "RISK_LIMIT_EXCEEDED",
                    "message": "Order failed pre-trade risk controls.",
                    "details": meta
                }
            }
        )

    # 3. Lock balance
    side = req_body.side
    order_type = req_body.type
    quantity = Decimal(req_body.quantity)
    price = Decimal(req_body.price) if req_body.price else await price_engine.get_price(req_body.symbol)
    notional = price * quantity

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if side == "buy":
        # Lock quote currency
        quote_asset = meta["quote_asset"]
        row = c.execute("SELECT available, locked FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, quote_asset)).fetchone()
        bal = dict(row)
        
        # lock notional (or price * quantity)
        new_avail = Decimal(bal["available"]) - notional
        new_locked = Decimal(bal["locked"]) + notional
        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, quote_asset, str(new_avail), str(new_locked), str(new_avail + new_locked), now_iso))
    else:
        # Lock base currency
        base_asset = meta["base_asset"]
        row = c.execute("SELECT available, locked FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, base_asset)).fetchone()
        bal = dict(row)
        
        new_avail = Decimal(bal["available"]) - quantity
        new_locked = Decimal(bal["locked"]) + quantity
        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, base_asset, str(new_avail), str(new_locked), str(new_avail + new_locked), now_iso))

    # 4. Insert Order
    order_id = f"ord_{uuid4().hex[:12]}"
    
    # Check if market order, then it executes immediately, else open on book
    is_market = order_type == "market"
    initial_status = "open" if not is_market else "filled"
    
    c.execute("""
        INSERT INTO crypto_orders
        (id, account_id, exchange, client_order_id, symbol, side, type, status, price, quantity, quote_quantity, stop_price, filled_quantity, remaining_quantity, time_in_force, post_only, self_trade_prevention, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id, account_id, "simulated", req_body.client_order_id, req_body.symbol, side, order_type,
        "pending" if not is_market else "filled", req_body.price, req_body.quantity, req_body.quote_quantity, req_body.stop_price,
        "0" if not is_market else req_body.quantity, req_body.quantity if not is_market else "0",
        req_body.time_in_force, 1 if req_body.post_only else 0, req_body.self_trade_prevention, now_iso, now_iso
    ))
    conn.commit()

    # Write audit log
    write_audit_log(conn, account_id, auth_info["api_key_id"], "order.create", "order", order_id, client_ip, ua, "success", order_dict)

    # Trigger Websocket client push
    saved_order_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    saved_order = dict(saved_order_row)
    asyncio.create_task(ws_manager.push_private(account_id, "orders", "order.created", saved_order))
    asyncio.create_task(dispatch_webhook(account_id, "order.created", saved_order))

    # Push balance updates
    for asset in (meta["base_asset"], meta["quote_asset"]):
        b_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, asset)).fetchone()
        if b_row:
            asyncio.create_task(ws_manager.push_private(account_id, "balances", "balance.updated", dict(b_row)))

    # If it is a market order, execute immediate fill
    if is_market:
        fill_order(conn, order_id, price, quantity, is_maker=False)
        
    conn.close()
    
    # Return latest state
    conn = get_crypto_db()
    c = conn.cursor()
    final_order = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    
    return dict(final_order)

@router.post("/orders/test")
async def create_test_order(req_body: CreateOrderRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))):
    req_body.symbol = normalize_symbol(req_body.symbol)
    conn = get_crypto_db()
    passed, err_code, meta = await validate_pre_trade_risk(conn, auth_info["account_id"], req_body.model_dump())
    conn.close()
    
    if not passed:
        return {
            "valid": False,
            "error_code": err_code,
            "checks": [{"name": "pre_trade_risk", "passed": False, "message": err_code}]
        }
        
    return {
        "valid": True,
        "estimated_notional": meta["estimated_notional"],
        "estimated_fee": meta["estimated_fee"],
        "checks": [
            {"name": "balance_check", "passed": True},
            {"name": "min_notional_check", "passed": True},
            {"name": "price_precision_check", "passed": True}
        ]
    }

@router.get("/orders")
def query_orders(
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    auth_info: Dict[str, Any] = Depends(require_scopes(["read:orders"]))
):
    if symbol:
        symbol = normalize_symbol(symbol)
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_orders WHERE account_id = ?"
    params = [auth_info["account_id"]]
    
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if side:
        query += " AND side = ?"
        params.append(side)
    if status:
        query += " AND status = ?"
        params.append(status)
        
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    return {
        "success": True,
        "data": [dict(r) for r in rows],
        "pagination": {
            "limit": limit,
            "next_cursor": None,
            "has_more": False
        }
    }

@router.get("/orders/{order_id}")
def query_single_order(order_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["read:orders"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_orders WHERE account_id = ? AND id = ?", (auth_info["account_id"], order_id)).fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "ORDER_NOT_FOUND",
                    "message": f"Order {order_id} not found."
                }
            }
        )
        
    return dict(row)

@router.get("/orders/by-client-id/{client_order_id}")
def query_order_by_client_id(client_order_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["read:orders"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_orders WHERE account_id = ? AND client_order_id = ?", (auth_info["account_id"], client_order_id)).fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "ORDER_NOT_FOUND",
                    "message": f"Order with client_order_id '{client_order_id}' not found."
                }
            }
        )
        
    return dict(row)

@router.get("/open-orders")
def query_open_orders(symbol: Optional[str] = None, auth_info: Dict[str, Any] = Depends(require_scopes(["read:orders"]))):
    if symbol:
        symbol = normalize_symbol(symbol)
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_orders WHERE account_id = ? AND status IN ('pending', 'accepted', 'open', 'partially_filled')"
    params = [auth_info["account_id"]]
    
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
        
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    return {
        "success": True,
        "data": [dict(r) for r in rows]
    }

@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))):
    conn = get_crypto_db()
    try:
        res = cancel_single_order_sync(conn, order_id)
        conn.close()
        return res
    except ValueError as e:
        conn.close()
        err_msg = str(e)
        raise HTTPException(
            status_code=400 if err_msg != "ORDER_NOT_FOUND" else 404,
            detail={
                "success": False,
                "error": {
                    "code": err_msg,
                    "message": "Order cancellation failed."
                }
            }
        )

@router.post("/orders/by-client-id/{client_order_id}/cancel")
async def cancel_order_by_client_id(client_order_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT id FROM crypto_orders WHERE account_id = ? AND client_order_id = ?", (auth_info["account_id"], client_order_id)).fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "ORDER_NOT_FOUND",
                    "message": f"Order with client_order_id '{client_order_id}' not found."
                }
            }
        )
        
    order_id = row["id"]
    try:
        res = cancel_single_order_sync(conn, order_id)
        conn.close()
        return res
    except ValueError as e:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": str(e),
                    "message": "Order cancellation failed."
                }
            }
        )

@router.post("/orders/batch")
async def create_batch_orders(
    request: Request,
    req_body: BatchOrderRequest,
    auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))
):
    results = []
    # Maximum 50 orders per request
    if len(req_body.orders) > 50:
        raise HTTPException(status_code=400, detail="Batch size limit of 50 orders exceeded")

    for order_req in req_body.orders:
        try:
            res = await create_order(request, order_req, auth_info)
            results.append({
                "client_order_id": order_req.client_order_id,
                "order_id": res.get("id"),
                "status": res.get("status"),
                "success": True
            })
        except HTTPException as e:
            results.append({
                "client_order_id": order_req.client_order_id,
                "success": False,
                "error": e.detail
            })
            
    return {
        "success": True,
        "data": results
    }

@router.post("/orders/cancel-batch")
async def cancel_batch_orders(req_body: CancelBatchRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))):
    results = []
    conn = get_crypto_db()
    for order_id in req_body.order_ids:
        try:
            res = cancel_single_order_sync(conn, order_id)
            results.append({
                "order_id": order_id,
                "status": res["status"],
                "success": True
            })
        except Exception as e:
            results.append({
                "order_id": order_id,
                "success": False,
                "error": str(e)
            })
    conn.close()
    return {
        "success": True,
        "data": results
    }

@router.post("/orders/cancel-all")
async def cancel_all_orders(req_body: CancelAllRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))):
    if req_body.symbol:
        req_body.symbol = normalize_symbol(req_body.symbol)
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT id FROM crypto_orders WHERE account_id = ? AND status IN ('pending', 'accepted', 'open', 'partially_filled')"
    params = [account_id]
    if req_body.symbol:
        query += " AND symbol = ?"
        params.append(req_body.symbol)
        
    rows = c.execute(query, params).fetchall()
    
    count = 0
    for r in rows:
        try:
            cancel_single_order_sync(conn, r["id"], reason=req_body.reason)
            count += 1
        except Exception:
            pass
            
    conn.close()
    return {
        "symbol": req_body.symbol,
        "cancel_requested_count": count,
        "requested_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
    }

@router.post("/orders/{order_id}/replace")
async def replace_order(
    request: Request,
    order_id: str,
    req_body: ReplaceOrderRequest,
    auth_info: Dict[str, Any] = Depends(require_scopes(["trade:spot"]))
):
    conn = get_crypto_db()
    c = conn.cursor()
    
    # 1. Fetch old order
    old_row = c.execute("SELECT * FROM crypto_orders WHERE account_id = ? AND id = ?", (auth_info["account_id"], order_id)).fetchone()
    if not old_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
    
    old_order = dict(old_row)
    
    # 2. Cancel old order
    try:
        cancel_single_order_sync(conn, order_id, reason="replaced")
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Failed to cancel order to replace: {e}")
        
    # 3. Create new order
    new_req = CreateOrderRequest(
        client_order_id=req_body.new_client_order_id,
        symbol=old_order["symbol"],
        side=old_order["side"],
        type=old_order["type"],
        price=req_body.price,
        quantity=req_body.quantity,
        time_in_force=old_order["time_in_force"],
        post_only=bool(old_order["post_only"]),
        self_trade_prevention=old_order["self_trade_prevention"]
    )
    
    conn.close()
    
    res = await create_order(request, new_req, auth_info)
    
    return {
        "old_order_id": order_id,
        "new_order_id": res["id"],
        "new_client_order_id": req_body.new_client_order_id,
        "status": res["status"],
        "price": res["price"],
        "quantity": res["quantity"],
        "created_at": res["created_at"]
    }


# ─────────────── 13.4 Fill / Trade API ───────────────

@router.get("/fills")
def query_fills(
    symbol: Optional[str] = None,
    order_id: Optional[str] = None,
    side: Optional[str] = None,
    limit: int = 100,
    auth_info: Dict[str, Any] = Depends(require_scopes(["read:fills"]))
):
    if symbol:
        symbol = normalize_symbol(symbol)
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_fills WHERE account_id = ?"
    params = [auth_info["account_id"]]
    
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    if order_id:
        query += " AND order_id = ?"
        params.append(order_id)
    if side:
        query += " AND side = ?"
        params.append(side)
        
    query += " ORDER BY executed_at DESC LIMIT ?"
    params.append(limit)
    
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    return {
        "success": True,
        "data": [dict(r) for r in rows],
        "pagination": {
            "limit": limit,
            "next_cursor": None,
            "has_more": False
        }
    }

@router.get("/fills/summary")
def query_fills_summary(
    symbol: str,
    start_time: str,
    end_time: str,
    auth_info: Dict[str, Any] = Depends(require_scopes(["read:fills"]))
):
    symbol = normalize_symbol(symbol)
    conn = get_crypto_db()
    c = conn.cursor()
    
    rows = c.execute(
        "SELECT * FROM crypto_fills WHERE account_id = ? AND symbol = ? AND executed_at >= ? AND executed_at <= ?",
        (auth_info["account_id"], symbol, start_time, end_time)
    ).fetchall()
    conn.close()
    
    buy_vol = Decimal("0")
    sell_vol = Decimal("0")
    buy_notional = Decimal("0")
    sell_notional = Decimal("0")
    total_fee = Decimal("0")
    fee_asset = "USDT"
    
    for r in rows:
        qty = Decimal(r["quantity"])
        notional = Decimal(r["notional"])
        fee = Decimal(r["fee"])
        fee_asset = r["fee_asset"]
        
        total_fee += fee
        if r["side"] == "buy":
            buy_vol += qty
            buy_notional += notional
        else:
            sell_vol += qty
            sell_notional += notional
            
    return {
        "symbol": symbol,
        "start_time": start_time,
        "end_time": end_time,
        "buy_volume": str(buy_vol),
        "sell_volume": str(sell_vol),
        "buy_notional": str(buy_notional),
        "sell_notional": str(sell_notional),
        "total_fee": str(total_fee),
        "fee_asset": fee_asset,
        "trade_count": len(rows)
    }

@router.get("/fills/{fill_id}")
def query_single_fill(fill_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["read:fills"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_fills WHERE account_id = ? AND id = ?", (auth_info["account_id"], fill_id)).fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Fill not found")
    return dict(row)


# ─────────────── 13.5 Risk API ───────────────

@router.get("/risk/limits")
def query_risk_limits(auth_info: Dict[str, Any] = Depends(require_scopes(["risk:read"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_risk_limits WHERE account_id = ?", (auth_info["account_id"],)).fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Risk limits not configured")
    return dict(row)

@router.patch("/risk/limits")
def update_risk_limits(req_body: UpdateRiskLimitsRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["risk:write"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    # Get current
    row = c.execute("SELECT * FROM crypto_risk_limits WHERE account_id = ?", (account_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Risk limits not found")
        
    current = dict(row)
    
    new_max_single = req_body.max_single_order_notional or current["max_single_order_notional"]
    new_max_daily = req_body.max_daily_notional or current["max_daily_notional"]
    new_max_open = req_body.max_open_orders if req_body.max_open_orders is not None else current["max_open_orders"]
    new_deviation = req_body.max_price_deviation_percent or current["max_price_deviation_percent"]
    new_allowed = json.dumps(req_body.allowed_symbols) if req_body.allowed_symbols is not None else current["allowed_symbols"]
    
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    c.execute("""
        UPDATE crypto_risk_limits
        SET max_single_order_notional = ?, max_daily_notional = ?, max_open_orders = ?, max_price_deviation_percent = ?, allowed_symbols = ?, updated_at = ?
        WHERE account_id = ?
    """, (new_max_single, new_max_daily, new_max_open, new_deviation, new_allowed, now_iso, account_id))
    conn.commit()
    conn.close()
    
    return {
        "updated": True,
        "updated_at": now_iso
    }

@router.post("/risk/validate-order")
async def risk_validate_order(req_body: CreateOrderRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["risk:read"]))):
    req_body.symbol = normalize_symbol(req_body.symbol)
    conn = get_crypto_db()
    passed, err_code, meta = await validate_pre_trade_risk(conn, auth_info["account_id"], req_body.model_dump())
    conn.close()
    
    return {
        "valid": passed,
        "error_code": err_code,
        "estimated_notional": meta.get("estimated_notional") if passed else "0",
        "estimated_fee": meta.get("estimated_fee") if passed else "0",
        "checks": [
            {"name": "symbol_status", "passed": passed or err_code != "SYMBOL_NOT_TRADING"},
            {"name": "balance", "passed": passed or err_code != "INSUFFICIENT_BALANCE"},
            {"name": "min_notional", "passed": passed or err_code != "MIN_NOTIONAL_NOT_MET"}
        ]
    }

@router.get("/risk/exposure")
def query_risk_exposure(auth_info: Dict[str, Any] = Depends(require_scopes(["risk:read"]))):
    # Fetch all asset balances
    conn = get_crypto_db()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM crypto_balances WHERE account_id = ?", (auth_info["account_id"],)).fetchall()
    conn.close()
    
    assets = []
    total_equity = Decimal("0")
    
    # We fetch prices in background to calculate mark values
    # BTC-USDT price, etc.
    for r in rows:
        asset = r["asset"]
        total_qty = Decimal(r["total"])
        if total_qty == Decimal("0"):
            continue
            
        if asset == "USDT":
            price = Decimal("1.00")
        else:
            # We call blocking price engine with synchronous wrapper (simulated engine is fast)
            price = Decimal(price_engine.price_cache.get(f"{asset}-USDT", "0"))
            
        val_usd = total_qty * price
        total_equity += val_usd
        
        assets.append({
            "asset": asset,
            "quantity": r["total"],
            "mark_price": str(price),
            "value_usd": str(val_usd),
            "exposure_percent": "0.00"  # calculated below
        })
        
    for a in assets:
        if total_equity > Decimal("0"):
            pct = (Decimal(a["value_usd"]) / total_equity) * 100
            a["exposure_percent"] = f"{pct:.2f}"
            
    return {
        "total_equity_usd": str(total_equity),
        "assets": assets,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
    }

@router.get("/risk/kill-switch")
def query_kill_switch(auth_info: Dict[str, Any] = Depends(require_scopes(["risk:read"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute("SELECT * FROM crypto_kill_switch WHERE id = 1").fetchone()
    conn.close()
    
    if not row:
        return {"active": False, "reason": None}
    return dict(row)

@router.post("/risk/kill-switch/activate")
async def activate_kill_switch(req_body: KillSwitchRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["risk:write"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    c.execute("""
        UPDATE crypto_kill_switch
        SET active = 1, reason = ?, activated_at = ?, activated_by = ?
        WHERE id = 1
    """, (req_body.reason, now_iso, auth_info["api_key_id"]))
    conn.commit()
    
    count = 0
    if req_body.cancel_all_orders:
        # Cancel all open orders for all users (simulated system wide emergency stop)
        rows = c.execute("SELECT id FROM crypto_orders WHERE status IN ('pending', 'accepted', 'open', 'partially_filled')").fetchall()
        for r in rows:
            try:
                cancel_single_order_sync(conn, r["id"], reason="kill_switch_activated")
                count += 1
            except Exception:
                pass
                
    conn.close()
    
    # Broadcast event to WebSocket
    asyncio.create_task(ws_manager.push_private(auth_info["account_id"], "risk", "kill_switch.active", {"active": True, "reason": req_body.reason}))
    asyncio.create_task(dispatch_webhook(auth_info["account_id"], "risk.triggered", {"active": True, "reason": req_body.reason}))

    return {
        "active": True,
        "cancel_all_orders": req_body.cancel_all_orders,
        "activated_at": now_iso
    }

@router.post("/risk/kill-switch/deactivate")
async def deactivate_kill_switch(auth_info: Dict[str, Any] = Depends(require_scopes(["risk:write"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    c.execute("""
        UPDATE crypto_kill_switch
        SET active = 0, reason = NULL, activated_at = NULL, activated_by = NULL
        WHERE id = 1
    """, ())
    conn.commit()
    conn.close()
    
    asyncio.create_task(ws_manager.push_private(auth_info["account_id"], "risk", "kill_switch.inactive", {"active": False}))

    return {
        "active": False,
        "deactivated_at": now_iso
    }


# ─────────────── 13.6 Webhook API ───────────────

@router.post("/webhooks/endpoints")
def create_webhook(req_body: CreateWebhookRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["webhook:write"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    wh_id = f"wh_{uuid4().hex[:12]}"
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    
    c.execute("""
        INSERT INTO crypto_webhook_endpoints (id, account_id, url, events, secret, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (wh_id, account_id, req_body.url, json.dumps(req_body.events), req_body.secret, req_body.status, now_iso))
    conn.commit()
    conn.close()
    
    return {
        "webhook_id": wh_id,
        "url": req_body.url,
        "events": req_body.events,
        "status": req_body.status,
        "created_at": now_iso
    }

@router.get("/webhooks/endpoints")
def query_webhooks(auth_info: Dict[str, Any] = Depends(require_scopes(["webhook:read"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM crypto_webhook_endpoints WHERE account_id = ?", (auth_info["account_id"],)).fetchall()
    conn.close()
    
    data = []
    for r in rows:
        wh = dict(r)
        data.append({
            "webhook_id": wh["id"],
            "url": wh["url"],
            "events": json.loads(wh["events"]),
            "status": wh["status"],
            "created_at": wh["created_at"]
        })
        
    return {"data": data}

@router.patch("/webhooks/endpoints/{webhook_id}")
def update_webhook(webhook_id: str, req_body: UpdateWebhookRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["webhook:write"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = c.execute("SELECT * FROM crypto_webhook_endpoints WHERE account_id = ? AND id = ?", (account_id, webhook_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        
    current = dict(row)
    new_events = json.dumps(req_body.events) if req_body.events is not None else current["events"]
    new_status = req_body.status or current["status"]
    
    c.execute("""
        UPDATE crypto_webhook_endpoints
        SET events = ?, status = ?
        WHERE id = ? AND account_id = ?
    """, (new_events, new_status, webhook_id, account_id))
    conn.commit()
    conn.close()
    
    return {
        "webhook_id": webhook_id,
        "updated": True
    }

@router.delete("/webhooks/endpoints/{webhook_id}")
def delete_webhook(webhook_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["webhook:write"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = c.execute("SELECT id FROM crypto_webhook_endpoints WHERE account_id = ? AND id = ?", (account_id, webhook_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        
    c.execute("DELETE FROM crypto_webhook_endpoints WHERE id = ? AND account_id = ?", (webhook_id, account_id))
    conn.commit()
    conn.close()
    
    return {
        "webhook_id": webhook_id,
        "deleted": True
    }


# ─────────────── 13.7 Audit API ───────────────

@router.get("/audit-logs")
def query_audit_logs(
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    limit: int = 100,
    auth_info: Dict[str, Any] = Depends(require_scopes(["audit:read"]))
):
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_audit_logs WHERE account_id = ?"
    params = [auth_info["account_id"]]
    
    if action:
        query += " AND action = ?"
        params.append(action)
    if resource_type:
        query += " AND resource_type = ?"
        params.append(resource_type)
        
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    data = []
    for r in rows:
        log = dict(r)
        data.append({
            "audit_id": log["id"],
            "actor_id": log["actor_id"],
            "api_key_id": log["api_key_id"],
            "action": log["action"],
            "resource_type": log["resource_type"],
            "resource_id": log["resource_id"],
            "ip_address": log["ip_address"],
            "user_agent": log["user_agent"],
            "result": log["result"],
            "metadata": json.loads(log["metadata"]) if log["metadata"] else {},
            "created_at": log["created_at"]
        })
        
    return {
        "success": True,
        "data": data,
        "pagination": {
            "limit": limit,
            "next_cursor": None,
            "has_more": False
        }
    }


# ─────────────── 13.8 API Key API ───────────────

@router.post("/api-keys")
def create_api_key(req_body: CreateApiKeyRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:keys"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    api_key_id = f"key_{uuid4().hex[:12]}"
    api_key = f"api_key_{uuid4().hex[:20]}"
    api_secret = f"secret_{uuid4().hex[:20]}"
    
    api_key_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    
    c.execute("""
        INSERT INTO crypto_api_keys (id, account_id, name, api_key_hash, api_secret, scopes, ip_whitelist, status, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (api_key_id, account_id, req_body.name, api_key_hash, api_secret, json.dumps(req_body.scopes), json.dumps(req_body.ip_whitelist), "active", req_body.expires_at, now_iso))
    conn.commit()
    conn.close()
    
    return {
        "api_key_id": api_key_id,
        "api_key": api_key,
        "api_secret": api_secret,
        "name": req_body.name,
        "scopes": req_body.scopes,
        "status": "active",
        "created_at": now_iso
    }

@router.get("/api-keys")
def query_api_keys(auth_info: Dict[str, Any] = Depends(require_scopes(["admin:keys"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    rows = c.execute("SELECT * FROM crypto_api_keys WHERE account_id = ?", (account_id,)).fetchall()
    conn.close()
    
    data = []
    for r in rows:
        key = dict(r)
        data.append({
            "api_key_id": key["id"],
            "name": key["name"],
            "scopes": json.loads(key["scopes"]) if key["scopes"] else [],
            "ip_whitelist": json.loads(key["ip_whitelist"]) if key["ip_whitelist"] else [],
            "status": key["status"],
            "created_at": key["created_at"],
            "expires_at": key["expires_at"],
            "last_used_at": key["last_used_at"]
        })
        
    return {"data": data}

@router.patch("/api-keys/{api_key_id}")
def update_api_key(api_key_id: str, req_body: UpdateApiKeyRequest, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:keys"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = c.execute("SELECT * FROM crypto_api_keys WHERE account_id = ? AND id = ?", (account_id, api_key_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="API Key not found")
        
    current = dict(row)
    new_name = req_body.name or current["name"]
    new_scopes = json.dumps(req_body.scopes) if req_body.scopes is not None else current["scopes"]
    new_ip = json.dumps(req_body.ip_whitelist) if req_body.ip_whitelist is not None else current["ip_whitelist"]
    new_status = req_body.status or current["status"]
    
    c.execute("""
        UPDATE crypto_api_keys
        SET name = ?, scopes = ?, ip_whitelist = ?, status = ?
        WHERE id = ? AND account_id = ?
    """, (new_name, new_scopes, new_ip, new_status, api_key_id, account_id))
    conn.commit()
    conn.close()
    
    return {
        "api_key_id": api_key_id,
        "updated": True
    }

@router.post("/api-keys/{api_key_id}/disable")
def disable_api_key(api_key_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:keys"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = c.execute("SELECT id FROM crypto_api_keys WHERE account_id = ? AND id = ?", (account_id, api_key_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="API Key not found")
        
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    c.execute("UPDATE crypto_api_keys SET status = 'disabled' WHERE id = ? AND account_id = ?", (api_key_id, account_id))
    conn.commit()
    conn.close()
    
    return {
        "api_key_id": api_key_id,
        "status": "disabled",
        "updated_at": now_iso
    }

@router.delete("/api-keys/{api_key_id}")
def delete_api_key(api_key_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:keys"]))):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = c.execute("SELECT id FROM crypto_api_keys WHERE account_id = ? AND id = ?", (account_id, api_key_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="API Key not found")
        
    c.execute("DELETE FROM crypto_api_keys WHERE id = ? AND account_id = ?", (api_key_id, account_id))
    conn.commit()
    conn.close()
    
    return {
        "api_key_id": api_key_id,
        "deleted": True
    }


# ─────────────── 13.10 Exchange Adapter Management API ───────────────

@router.get("/exchanges")
async def get_exchanges(auth_info: Dict[str, Any] = Depends(require_scopes(["admin:system"]))):
    return {
        "success": True,
        "data": [
            {
                "exchange": "binance",
                "status": "online",
                "trading_status": "trading",
                "supported_market_types": ["spot"],
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
            },
            {
                "exchange": "simulated",
                "status": "online",
                "trading_status": "trading",
                "supported_market_types": ["spot"],
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
            }
        ]
    }

@router.get("/exchanges/{exchange}/status")
async def get_exchange_status(exchange: str, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:system"]))):
    exch = exchange.lower().strip()
    if exch not in ("binance", "simulated"):
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "EXCHANGE_NOT_FOUND",
                    "message": f"Exchange '{exchange}' is not configured."
                }
            }
        )
    
    if exch == "binance":
        import urllib.request
        start = time.time()
        latency = 50
        status_val = "online"
        try:
            url = f"{price_engine.base_url}/api/v3/ping"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2) as res:
                res.read()
            latency = int((time.time() - start) * 1000)
        except Exception:
            status_val = "offline"
            latency = 999
            
        return {
            "success": True,
            "data": {
                "exchange": "binance",
                "status": status_val,
                "trading_status": "trading" if status_val == "online" else "halted",
                "latency_ms": latency,
                "last_heartbeat_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "rate_limit": {
                    "remaining": 999,
                    "reset_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
                }
            }
        }
    else:
        return {
            "success": True,
            "data": {
                "exchange": "simulated",
                "status": "online",
                "trading_status": "trading",
                "latency_ms": 0,
                "last_heartbeat_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "rate_limit": {
                    "remaining": 9999,
                    "reset_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")
                }
            }
        }

@router.get("/exchanges/{exchange}/symbols")
async def get_exchange_symbols(exchange: str, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:system"]))):
    exch = exchange.lower().strip()
    if exch not in ("binance", "simulated"):
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "EXCHANGE_NOT_FOUND",
                    "message": f"Exchange '{exchange}' is not configured."
                }
            }
        )
    
    conn = get_crypto_db()
    c = conn.cursor()
    rows = c.execute("SELECT symbol FROM crypto_markets").fetchall()
    conn.close()
    
    data = []
    for r in rows:
        symbol = r["symbol"]
        exchange_symbol = symbol.replace("-", "") if exch == "binance" else symbol
        data.append({
            "internal_symbol": symbol,
            "exchange_symbol": exchange_symbol,
            "status": "trading"
        })
        
    return {
        "success": True,
        "exchange": exchange,
        "data": data
    }


# ─────────────── 13.11 Internal Reconciliation API ───────────────

@router.post("/internal/reconciliation/orders/{order_id}")
async def reconcile_order(order_id: str, auth_info: Dict[str, Any] = Depends(require_scopes(["admin:system"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    # Query order
    order = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": {
                    "code": "ORDER_NOT_FOUND",
                    "message": f"Order '{order_id}' was not found."
                }
            }
        )
        
    order_dict = dict(order)
    
    # Query fills
    fills = c.execute("SELECT quantity, fee, fee_asset FROM crypto_fills WHERE order_id = ?", (order_id,)).fetchall()
    
    sum_filled_qty = Decimal("0")
    sum_fee = Decimal("0")
    fee_asset = None
    for f in fills:
        sum_filled_qty += Decimal(f["quantity"])
        sum_fee += Decimal(f["fee"])
        fee_asset = f["fee_asset"]
        
    orig_qty = Decimal(order_dict["quantity"])
    cur_filled = Decimal(order_dict["filled_quantity"])
    
    corrected = False
    old_filled = str(cur_filled)
    old_status = order_dict["status"]
    
    if sum_filled_qty != cur_filled:
        corrected = True
        new_remaining = orig_qty - sum_filled_qty
        new_status = order_dict["status"]
        if sum_filled_qty >= orig_qty:
            new_status = "filled"
            new_remaining = Decimal("0")
        elif sum_filled_qty > 0:
            new_status = "partially_filled"
        else:
            if order_dict["status"] in ("filled", "partially_filled"):
                new_status = "open"
                
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        c.execute("""
            UPDATE crypto_orders
            SET filled_quantity = ?, remaining_quantity = ?, status = ?, fee = ?, fee_asset = ?, updated_at = ?
            WHERE id = ?
        """, (str(sum_filled_qty), str(new_remaining), new_status, str(sum_fee), fee_asset, now_iso, order_id))
        conn.commit()
        
        # Log audit log
        write_audit_log(conn, order_dict["account_id"], auth_info["api_key_id"], "order.reconcile", "order", order_id, "internal", "system", "success", {
            "order_id": order_id,
            "corrected": True,
            "old_status": old_status,
            "new_status": new_status,
            "old_filled": old_filled,
            "new_filled": str(sum_filled_qty)
        })
        
        order_dict["status"] = new_status
        order_dict["filled_quantity"] = str(sum_filled_qty)
        order_dict["remaining_quantity"] = str(new_remaining)
        
    conn.close()
    
    return {
        "success": True,
        "order_id": order_id,
        "reconciled": True,
        "corrected": corrected,
        "old_status": old_status,
        "new_status": order_dict["status"],
        "old_filled": old_filled,
        "new_filled": order_dict["filled_quantity"]
    }

@router.post("/internal/reconciliation/fills")
async def reconcile_fills(auth_info: Dict[str, Any] = Depends(require_scopes(["admin:system"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    # Get all active or filled/partially filled orders
    orders = c.execute("SELECT id FROM crypto_orders").fetchall()
    
    reconciled_count = 0
    corrected_count = 0
    corrections = []
    
    for r in orders:
        order_id = r["id"]
        res = await reconcile_order_internal(conn, order_id, auth_info["api_key_id"])
        reconciled_count += 1
        if res["corrected"]:
            corrected_count += 1
            corrections.append(res)
            
    conn.close()
    return {
        "success": True,
        "total_reconciled": reconciled_count,
        "total_corrected": corrected_count,
        "corrections": corrections
    }

# Internal helper function used by endpoint and batch function
async def reconcile_order_internal(conn: sqlite3.Connection, order_id: str, actor_id: str) -> Dict[str, Any]:
    c = conn.cursor()
    order = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        return {"order_id": order_id, "corrected": False, "error": "NOT_FOUND"}
        
    order_dict = dict(order)
    fills = c.execute("SELECT quantity, fee, fee_asset FROM crypto_fills WHERE order_id = ?", (order_id,)).fetchall()
    
    sum_filled_qty = Decimal("0")
    sum_fee = Decimal("0")
    fee_asset = None
    for f in fills:
        sum_filled_qty += Decimal(f["quantity"])
        sum_fee += Decimal(f["fee"])
        fee_asset = f["fee_asset"]
        
    orig_qty = Decimal(order_dict["quantity"])
    cur_filled = Decimal(order_dict["filled_quantity"])
    
    corrected = False
    old_filled = str(cur_filled)
    old_status = order_dict["status"]
    new_status = old_status
    
    if sum_filled_qty != cur_filled:
        corrected = True
        new_remaining = orig_qty - sum_filled_qty
        if sum_filled_qty >= orig_qty:
            new_status = "filled"
            new_remaining = Decimal("0")
        elif sum_filled_qty > 0:
            new_status = "partially_filled"
        else:
            if old_status in ("filled", "partially_filled"):
                new_status = "open"
                
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        c.execute("""
            UPDATE crypto_orders
            SET filled_quantity = ?, remaining_quantity = ?, status = ?, fee = ?, fee_asset = ?, updated_at = ?
            WHERE id = ?
        """, (str(sum_filled_qty), str(new_remaining), new_status, str(sum_fee), fee_asset, now_iso, order_id))
        conn.commit()
        
        # Log audit log
        write_audit_log(conn, order_dict["account_id"], actor_id, "order.reconcile", "order", order_id, "internal", "system", "success", {
            "order_id": order_id,
            "corrected": True,
            "old_status": old_status,
            "new_status": new_status,
            "old_filled": old_filled,
            "new_filled": str(sum_filled_qty)
        })
        
    return {
        "order_id": order_id,
        "corrected": corrected,
        "old_status": old_status,
        "new_status": new_status,
        "old_filled": old_filled,
        "new_filled": str(sum_filled_qty)
    }

@router.get("/internal/reconciliation/issues")
async def get_reconciliation_issues(auth_info: Dict[str, Any] = Depends(require_scopes(["admin:system"]))):
    conn = get_crypto_db()
    c = conn.cursor()
    
    # Find orders with mismatching filled quantity
    orders = c.execute("SELECT * FROM crypto_orders").fetchall()
    
    issues = []
    for r in orders:
        order_id = r["id"]
        fills = c.execute("SELECT quantity FROM crypto_fills WHERE order_id = ?", (order_id,)).fetchall()
        sum_filled_qty = sum(Decimal(f["quantity"]) for f in fills)
        cur_filled = Decimal(r["filled_quantity"])
        
        if sum_filled_qty != cur_filled:
            issues.append({
                "type": "FILLED_QUANTITY_MISMATCH",
                "order_id": order_id,
                "symbol": r["symbol"],
                "account_id": r["account_id"],
                "order_filled": str(cur_filled),
                "actual_fills_sum": str(sum_filled_qty),
                "description": f"Order filled quantity ({cur_filled}) does not match sum of fills ({sum_filled_qty})"
            })
            
        # Check if status is unknown
        if r["status"] == "unknown":
            issues.append({
                "type": "UNKNOWN_STATUS",
                "order_id": order_id,
                "symbol": r["symbol"],
                "account_id": r["account_id"],
                "description": "Order status is unknown"
            })
            
    conn.close()
    return {
        "success": True,
        "total_issues": len(issues),
        "issues": issues
    }


def order_to_binance(order: Dict[str, Any], fills_list: List[Dict[str, Any]] = []) -> Dict[str, Any]:
    status_map = {
        "pending": "NEW",
        "accepted": "NEW",
        "open": "NEW",
        "partially_filled": "PARTIALLY_FILLED",
        "filled": "FILLED",
        "cancelled": "CANCELED",
        "rejected": "REJECTED",
        "expired": "EXPIRED"
    }
    
    # Convert times to millisecond timestamps
    created_at_dt = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
    created_ms = int(created_at_dt.timestamp() * 1000)
    
    updated_at_dt = datetime.fromisoformat(order["updated_at"].replace("Z", "+00:00"))
    updated_ms = int(updated_at_dt.timestamp() * 1000)

    numeric_order_id = int(hashlib.md5(order["id"].encode('utf-8')).hexdigest()[:8], 16)

    binance_fills = []
    cummulative_quote_qty = Decimal("0")
    for f in fills_list:
        cummulative_quote_qty += Decimal(f["notional"])
        binance_fills.append({
            "price": f["price"],
            "qty": f["quantity"],
            "commission": f["fee"],
            "commissionAsset": f["fee_asset"],
            "tradeId": int(hashlib.md5(f["id"].encode('utf-8')).hexdigest()[:8], 16)
        })

    orig_qty = Decimal(order["quantity"])
    filled_qty = Decimal(order["filled_quantity"])
    
    return {
        "symbol": order["symbol"].replace("-", ""),
        "orderId": numeric_order_id,
        "orderListId": -1,
        "clientOrderId": order["client_order_id"],
        "transactTime": updated_ms,
        "price": order["price"] if order["price"] else "0.00",
        "origQty": str(orig_qty),
        "executedQty": str(filled_qty),
        "cummulativeQuoteQty": str(cummulative_quote_qty),
        "status": status_map.get(order["status"], "NEW"),
        "timeInForce": order["time_in_force"].upper() if order["time_in_force"] else "GTC",
        "type": order["type"].upper(),
        "side": order["side"].upper(),
        "workingTime": created_ms,
        "fills": binance_fills,
        "selfTradePreventionMode": "NONE"
    }

# ─────────────── Binance Spot API Compatibility Layer ───────────────

@binance_router.get("/ping")
def binance_ping():
    return {}

@binance_router.get("/time")
def binance_time():
    return {"serverTime": int(time.time() * 1000)}

@binance_router.get("/exchangeInfo")
def binance_exchange_info(
    symbol: Optional[str] = None,
    symbols: Optional[str] = None
):
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_markets"
    params = []
    
    filter_syms = []
    if symbol:
        filter_syms.append(normalize_symbol(symbol))
    elif symbols:
        try:
            decoded = json.loads(symbols)
            if isinstance(decoded, list):
                filter_syms.extend([normalize_symbol(s) for s in decoded])
        except Exception:
            pass
            
    if filter_syms:
        placeholders = ",".join("?" for _ in filter_syms)
        query += f" WHERE symbol IN ({placeholders})"
        params = filter_syms
        
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    symbols_list = []
    for r in rows:
        m = dict(r)
        sym_binance = m["symbol"].replace("-", "")
        symbols_list.append({
            "symbol": sym_binance,
            "status": "TRADING" if m["status"] == "trading" else "HALTED",
            "baseAsset": m["base_asset"],
            "baseAssetPrecision": m["quantity_precision"],
            "quoteAsset": m["quote_asset"],
            "quotePrecision": m["price_precision"],
            "quoteAssetPrecision": m["price_precision"],
            "baseCommissionPrecision": 8,
            "quoteCommissionPrecision": 8,
            "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "LIMIT_MAKER"],
            "timeInForce": ["GTC", "IOC", "FOK"],
            "icebergAllowed": False,
            "ocoAllowed": False,
            "quoteOrderQtyMarketAllowed": True,
            "allowTrailingStop": False,
            "cancelReplaceAllowed": True,
            "isSpotTradingAllowed": True,
            "isMarginTradingAllowed": False,
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": m["tick_size"],
                    "maxPrice": "1000000.00",
                    "tickSize": m["tick_size"]
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": m["min_quantity"],
                    "maxQty": m["max_quantity"],
                    "stepSize": m["lot_size"]
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": m["min_notional"],
                    "applyMinToMarket": True,
                    "maxNotional": "9000000.00",
                    "applyMaxToMarket": False,
                    "avgPriceMins": 5
                }
            ],
            "permissions": ["SPOT"],
            "defaultSelfTradePreventionMode": "NONE",
            "allowedSelfTradePreventionModes": ["NONE"]
        })
        
    return {
        "timezone": "UTC",
        "serverTime": int(time.time() * 1000),
        "rateLimits": [],
        "exchangeFilters": [],
        "symbols": symbols_list
    }

@binance_router.get("/depth")
async def binance_depth(symbol: str, limit: int = 100):
    norm_symbol = normalize_symbol(symbol)
    ob = await price_engine.get_orderbook(norm_symbol, limit)
    return {
        "lastUpdateId": ob["last_update_id"],
        "bids": ob["bids"],
        "asks": ob["asks"]
    }

@binance_router.get("/trades")
async def binance_trades(symbol: str, limit: int = 100):
    norm_symbol = normalize_symbol(symbol)
    trades = await price_engine.get_recent_trades(norm_symbol, limit)
    
    binance_trades = []
    for t in trades:
        t_id = int(hashlib.md5(t["trade_id"].encode('utf-8')).hexdigest()[:8], 16)
        dt = datetime.fromisoformat(t["executed_at"].replace("Z", "+00:00"))
        ts_ms = int(dt.timestamp() * 1000)
        binance_trades.append({
            "id": t_id,
            "price": t["price"],
            "qty": t["quantity"],
            "quoteQty": str(Decimal(t["price"]) * Decimal(t["quantity"])),
            "time": ts_ms,
            "isBuyerMaker": t["side"] == "sell",
            "isBestMatch": True
        })
    return binance_trades

@binance_router.get("/klines")
async def binance_klines(
    symbol: str,
    interval: str,
    limit: int = 500,
    startTime: Optional[int] = None,
    endTime: Optional[int] = None
):
    norm_symbol = normalize_symbol(symbol)
    klines = await price_engine.get_klines(norm_symbol, interval, limit)
    
    result = []
    for k in klines:
        open_time = int(datetime.fromisoformat(k["open_time"].replace("Z", "+00:00")).timestamp() * 1000)
        close_time = int(datetime.fromisoformat(k["close_time"].replace("Z", "+00:00")).timestamp() * 1000)
        result.append([
            open_time,
            k["open"],
            k["high"],
            k["low"],
            k["close"],
            k["volume"],
            close_time,
            k["quote_volume"],
            k["trade_count"],
            str(Decimal(k["volume"]) * Decimal("0.5")),
            str(Decimal(k["quote_volume"]) * Decimal("0.5")),
            "0"
        ])
    return result

@binance_router.get("/ticker/price")
async def binance_ticker_price(symbol: Optional[str] = None):
    sym_list = [symbol] if symbol else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    tickers = []
    for sym in sym_list:
        norm_sym = normalize_symbol(sym)
        try:
            ticker = await price_engine.get_ticker_24h(norm_sym)
            tickers.append({
                "symbol": sym.upper(),
                "price": ticker["last_price"]
            })
        except Exception:
            pass
            
    if symbol:
        if not tickers:
            raise HTTPException(status_code=400, detail={"code": -1121, "msg": "Invalid symbol."})
        return tickers[0]
    return tickers

@binance_router.get("/ticker/bookTicker")
async def binance_ticker_book_ticker(symbol: Optional[str] = None):
    sym_list = [symbol] if symbol else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    tickers = []
    for sym in sym_list:
        norm_sym = normalize_symbol(sym)
        try:
            ticker = await price_engine.get_ticker_24h(norm_sym)
            tickers.append({
                "symbol": sym.upper(),
                "bidPrice": ticker["best_bid"],
                "bidQty": "1.000000",
                "askPrice": ticker["best_ask"],
                "askQty": "1.000000"
            })
        except Exception:
            pass
            
    if symbol:
        if not tickers:
            raise HTTPException(status_code=400, detail={"code": -1121, "msg": "Invalid symbol."})
        return tickers[0]
    return tickers

# ─────────────── Signed Private Endpoints ───────────────

@binance_router.post("/order")
async def binance_create_order(
    request: Request,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["trade:spot"]))
):
    account_id = auth_info["account_id"]
    client_ip = auth_info["client_ip"]
    ua = request.headers.get("user-agent", "")

    # Gather params from query and form body
    query_params = dict(request.query_params)
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8') if body_bytes else ""
    form_params = dict(urllib.parse.parse_qsl(body_str))
    params = {**query_params, **form_params}

    symbol = params.get("symbol")
    side = params.get("side")
    order_type = params.get("type")
    quantity = params.get("quantity")
    price = params.get("price")
    stop_price = params.get("stopPrice")
    time_in_force = params.get("timeInForce", "GTC")
    client_order_id = params.get("newClientOrderId") or f"binance_{uuid4().hex[:12]}"

    if not symbol or not side or not order_type or not quantity:
        raise HTTPException(
            status_code=400,
            detail={"code": -1102, "msg": "Mandatory parameter 'symbol', 'side', 'type' or 'quantity' was not sent, empty, or malformed."}
        )

    norm_symbol = normalize_symbol(symbol)
    side = side.lower()
    order_type_mapped = {
        "LIMIT": "limit",
        "MARKET": "market",
        "STOP_LOSS": "stop_market",
        "STOP_LOSS_LIMIT": "stop_limit",
        "TAKE_PROFIT": "take_profit_market",
        "TAKE_PROFIT_LIMIT": "take_profit_limit",
        "LIMIT_MAKER": "limit"
    }.get(order_type.upper())

    if not order_type_mapped:
        raise HTTPException(status_code=400, detail={"code": -1102, "msg": f"Order type '{order_type}' is not supported."})

    post_only = (order_type.upper() == "LIMIT_MAKER")

    order_dict = {
        "client_order_id": client_order_id,
        "symbol": norm_symbol,
        "side": side,
        "type": order_type_mapped,
        "price": price,
        "quantity": quantity,
        "stop_price": stop_price,
        "time_in_force": time_in_force,
        "post_only": post_only
    }

    conn = get_crypto_db()
    c = conn.cursor()

    existing = c.execute(
        "SELECT * FROM crypto_orders WHERE account_id = ? AND client_order_id = ?",
        (account_id, client_order_id)
    ).fetchone()
    
    if existing:
        order_data = dict(existing)
        fills_list = [dict(f) for f in c.execute("SELECT * FROM crypto_fills WHERE order_id = ?", (order_data["id"],)).fetchall()]
        conn.close()
        return order_to_binance(order_data, fills_list)

    passed, err_code, meta = await validate_pre_trade_risk(conn, account_id, order_dict)
    if not passed:
        write_audit_log(conn, account_id, auth_info["api_key_id"], "order.create", "order", None, client_ip, ua, "failed", {"error": err_code, "request": order_dict})
        conn.close()
        
        binance_err_code = -2010
        if err_code == "INSUFFICIENT_BALANCE":
            binance_msg = "Account has insufficient balance for requested action."
        elif err_code == "KILL_SWITCH_ACTIVE":
            binance_msg = "Trading is currently disabled due to kill switch."
        elif err_code == "INVALID_SYMBOL" or err_code == "SYMBOL_NOT_TRADING":
            binance_err_code = -1121
            binance_msg = "Invalid symbol."
        elif err_code == "MIN_NOTIONAL_NOT_MET":
            binance_err_code = -1013
            binance_msg = "Filter failure: MIN_NOTIONAL"
        elif err_code == "MIN_QUANTITY_NOT_MET" or err_code == "RISK_LIMIT_EXCEEDED" or err_code == "MAX_ORDER_NOTIONAL_EXCEEDED":
            binance_err_code = -1013
            binance_msg = f"Filter failure: LIMITS ({err_code})"
        else:
            binance_msg = f"Order rejected: {err_code}"

        raise HTTPException(
            status_code=400,
            detail={"code": binance_err_code, "msg": binance_msg}
        )

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    qty_dec = Decimal(quantity)
    price_dec = Decimal(price) if price else await price_engine.get_price(norm_symbol)
    notional = qty_dec * price_dec

    if side == "buy":
        quote_asset = meta["quote_asset"]
        bal = dict(c.execute("SELECT available, locked FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, quote_asset)).fetchone())
        new_avail = Decimal(bal["available"]) - notional
        new_locked = Decimal(bal["locked"]) + notional
        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, quote_asset, str(new_avail), str(new_locked), str(new_avail + new_locked), now_iso))
    else:
        base_asset = meta["base_asset"]
        bal = dict(c.execute("SELECT available, locked FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, base_asset)).fetchone())
        new_avail = Decimal(bal["available"]) - qty_dec
        new_locked = Decimal(bal["locked"]) + qty_dec
        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, base_asset, str(new_avail), str(new_locked), str(new_avail + new_locked), now_iso))

    order_id = f"ord_{uuid4().hex[:12]}"
    is_market = order_type_mapped == "market"
    
    c.execute("""
        INSERT INTO crypto_orders
        (id, account_id, exchange, client_order_id, symbol, side, type, status, price, quantity, stop_price, filled_quantity, remaining_quantity, time_in_force, post_only, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id, account_id, "simulated", client_order_id, norm_symbol, side, order_type_mapped,
        "pending" if not is_market else "filled", price, quantity, stop_price,
        "0" if not is_market else quantity, quantity if not is_market else "0",
        time_in_force, 1 if post_only else 0, now_iso, now_iso
    ))
    conn.commit()

    write_audit_log(conn, account_id, auth_info["api_key_id"], "order.create", "order", order_id, client_ip, ua, "success", order_dict)

    saved_order_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    saved_order = dict(saved_order_row)
    asyncio.create_task(ws_manager.push_private(account_id, "orders", "order.created", saved_order))
    asyncio.create_task(dispatch_webhook(account_id, "order.created", saved_order))

    for asset in (meta["base_asset"], meta["quote_asset"]):
        b_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, asset)).fetchone()
        if b_row:
            asyncio.create_task(ws_manager.push_private(account_id, "balances", "balance.updated", dict(b_row)))

    if is_market:
        fill_order(conn, order_id, price_dec, qty_dec, is_maker=False)

    final_order = dict(c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone())
    fills = [dict(f) for f in c.execute("SELECT * FROM crypto_fills WHERE order_id = ?", (order_id,)).fetchall()]
    conn.close()

    return order_to_binance(final_order, fills)

@binance_router.post("/order/test")
async def binance_test_order(
    request: Request,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["trade:spot"]))
):
    account_id = auth_info["account_id"]
    
    query_params = dict(request.query_params)
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8') if body_bytes else ""
    form_params = dict(urllib.parse.parse_qsl(body_str))
    params = {**query_params, **form_params}

    symbol = params.get("symbol")
    side = params.get("side")
    order_type = params.get("type")
    quantity = params.get("quantity")
    price = params.get("price")
    stop_price = params.get("stopPrice")

    if not symbol or not side or not order_type or not quantity:
        raise HTTPException(
            status_code=400,
            detail={"code": -1102, "msg": "Mandatory parameter 'symbol', 'side', 'type' or 'quantity' was not sent, empty, or malformed."}
        )

    norm_symbol = normalize_symbol(symbol)
    order_type_mapped = {
        "LIMIT": "limit",
        "MARKET": "market",
        "STOP_LOSS": "stop_market",
        "STOP_LOSS_LIMIT": "stop_limit",
        "TAKE_PROFIT": "take_profit_market",
        "TAKE_PROFIT_LIMIT": "take_profit_limit",
        "LIMIT_MAKER": "limit"
    }.get(order_type.upper())

    if not order_type_mapped:
        raise HTTPException(status_code=400, detail={"code": -1102, "msg": f"Order type '{order_type}' is not supported."})

    order_dict = {
        "client_order_id": "test_order",
        "symbol": norm_symbol,
        "side": side.lower(),
        "type": order_type_mapped,
        "price": price,
        "quantity": quantity,
        "stop_price": stop_price
    }

    conn = get_crypto_db()
    passed, err_code, meta = await validate_pre_trade_risk(conn, account_id, order_dict)
    conn.close()

    if not passed:
        raise HTTPException(status_code=400, detail={"code": -2010, "msg": f"Order rejected: {err_code}"})

    return {}

@binance_router.get("/order")
def binance_query_order(
    request: Request,
    symbol: str,
    orderId: Optional[int] = None,
    origClientOrderId: Optional[str] = None,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["read:orders"]))
):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = None
    if orderId:
        norm_sym = normalize_symbol(symbol)
        rows = c.execute("SELECT * FROM crypto_orders WHERE account_id = ? AND symbol = ?", (account_id, norm_sym)).fetchall()
        for r in rows:
            if int(hashlib.md5(r["id"].encode('utf-8')).hexdigest()[:8], 16) == orderId:
                row = r
                break
    elif origClientOrderId:
        row = c.execute(
            "SELECT * FROM crypto_orders WHERE account_id = ? AND client_order_id = ?",
            (account_id, origClientOrderId)
        ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail={"code": -2011, "msg": "Unknown order."})

    order_data = dict(row)
    fills = [dict(f) for f in c.execute("SELECT * FROM crypto_fills WHERE order_id = ?", (order_data["id"],)).fetchall()]
    conn.close()
    
    return order_to_binance(order_data, fills)

@binance_router.delete("/order")
async def binance_cancel_order(
    request: Request,
    symbol: str,
    orderId: Optional[int] = None,
    origClientOrderId: Optional[str] = None,
    newClientOrderId: Optional[str] = None,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["trade:spot"]))
):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    row = None
    if orderId:
        norm_sym = normalize_symbol(symbol)
        rows = c.execute("SELECT * FROM crypto_orders WHERE account_id = ? AND symbol = ?", (account_id, norm_sym)).fetchall()
        for r in rows:
            if int(hashlib.md5(r["id"].encode('utf-8')).hexdigest()[:8], 16) == orderId:
                row = r
                break
    elif origClientOrderId:
        row = c.execute(
            "SELECT * FROM crypto_orders WHERE account_id = ? AND client_order_id = ?",
            (account_id, origClientOrderId)
        ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail={"code": -2011, "msg": "Unknown order."})

    order_data = dict(row)
    order_id = order_data["id"]

    try:
        res = cancel_single_order_sync(conn, order_id, reason="binance_api_requested")
    except ValueError as e:
        conn.close()
        raise HTTPException(status_code=400, detail={"code": -2011, "msg": f"Cancel rejected: {str(e)}"})

    if newClientOrderId:
        c.execute("UPDATE crypto_orders SET client_order_id = ? WHERE id = ?", (newClientOrderId, order_id))
        conn.commit()
        order_data["client_order_id"] = newClientOrderId

    order_data["status"] = "cancelled"
    conn.close()

    numeric_order_id = int(hashlib.md5(order_id.encode('utf-8')).hexdigest()[:8], 16)
    return {
        "symbol": order_data["symbol"].replace("-", ""),
        "origClientOrderId": order_data["client_order_id"],
        "orderId": numeric_order_id,
        "orderListId": -1,
        "clientOrderId": newClientOrderId or f"cancel_{uuid4().hex[:8]}",
        "price": order_data["price"] or "0.00",
        "origQty": order_data["quantity"],
        "executedQty": order_data["filled_quantity"],
        "cummulativeQuoteQty": str(Decimal(order_data["filled_quantity"]) * Decimal(order_data["price"] or "0")),
        "status": "CANCELED",
        "timeInForce": order_data["time_in_force"].upper(),
        "type": order_data["type"].upper(),
        "side": order_data["side"].upper()
    }

@binance_router.get("/openOrders")
def binance_open_orders(
    symbol: Optional[str] = None,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["read:orders"]))
):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    query = "SELECT * FROM crypto_orders WHERE account_id = ? AND status IN ('pending', 'accepted', 'open', 'partially_filled')"
    params = [account_id]
    if symbol:
        query += " AND symbol = ?"
        params.append(normalize_symbol(symbol))
        
    rows = c.execute(query, params).fetchall()
    
    result = []
    for r in rows:
        order_data = dict(r)
        fills = [dict(f) for f in c.execute("SELECT * FROM crypto_fills WHERE order_id = ?", (order_data["id"],)).fetchall()]
        result.append(order_to_binance(order_data, fills))
        
    conn.close()
    return result

@binance_router.get("/allOrders")
def binance_all_orders(
    symbol: str,
    orderId: Optional[int] = None,
    startTime: Optional[int] = None,
    endTime: Optional[int] = None,
    limit: int = 500,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["read:orders"]))
):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    norm_sym = normalize_symbol(symbol)
    query = "SELECT * FROM crypto_orders WHERE account_id = ? AND symbol = ?"
    params = [account_id, norm_sym]
    
    rows = c.execute(query, params).fetchall()
    
    result = []
    for r in rows:
        order_data = dict(r)
        
        numeric_id = int(hashlib.md5(order_data["id"].encode('utf-8')).hexdigest()[:8], 16)
        if orderId and numeric_id < orderId:
            continue
            
        created_dt = datetime.fromisoformat(order_data["created_at"].replace("Z", "+00:00"))
        created_ms = int(created_dt.timestamp() * 1000)
        
        if startTime and created_ms < startTime:
            continue
        if endTime and created_ms > endTime:
            continue
            
        fills = [dict(f) for f in c.execute("SELECT * FROM crypto_fills WHERE order_id = ?", (order_data["id"],)).fetchall()]
        result.append(order_to_binance(order_data, fills))
        
    conn.close()
    result.sort(key=lambda x: x["transactTime"])
    return result[:limit]

@binance_router.get("/account")
def binance_account_info(
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["read:account"]))
):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    rows = c.execute("SELECT * FROM crypto_balances WHERE account_id = ?", (account_id,)).fetchall()
    conn.close()
    
    balances = []
    for r in rows:
        b = dict(r)
        balances.append({
            "asset": b["asset"],
            "free": b["available"],
            "locked": b["locked"]
        })
        
    return {
        "makerCommission": 10,
        "takerCommission": 10,
        "buyerCommission": 0,
        "sellerCommission": 0,
        "canTrade": True,
        "canWithdraw": False,
        "canDeposit": False,
        "updateTime": int(time.time() * 1000),
        "accountType": "SPOT",
        "balances": balances,
        "permissions": ["SPOT"]
    }

@binance_router.get("/myTrades")
def binance_my_trades(
    symbol: str,
    startTime: Optional[int] = None,
    endTime: Optional[int] = None,
    fromId: Optional[int] = None,
    limit: int = 500,
    auth_info: Dict[str, Any] = Depends(require_binance_scopes(["read:fills"]))
):
    account_id = auth_info["account_id"]
    conn = get_crypto_db()
    c = conn.cursor()
    
    norm_sym = normalize_symbol(symbol)
    query = "SELECT * FROM crypto_fills WHERE account_id = ? AND symbol = ?"
    params = [account_id, norm_sym]
    
    rows = c.execute(query, params).fetchall()
    conn.close()
    
    result = []
    for r in rows:
        f = dict(r)
        f_id = int(hashlib.md5(f["id"].encode('utf-8')).hexdigest()[:8], 16)
        
        if fromId and f_id < fromId:
            continue
            
        dt = datetime.fromisoformat(f["executed_at"].replace("Z", "+00:00"))
        ts_ms = int(dt.timestamp() * 1000)
        
        if startTime and ts_ms < startTime:
            continue
        if endTime and ts_ms > endTime:
            continue
            
        result.append({
            "symbol": symbol.upper(),
            "id": f_id,
            "orderId": int(hashlib.md5(f["order_id"].encode('utf-8')).hexdigest()[:8], 16),
            "orderListId": -1,
            "price": f["price"],
            "qty": f["quantity"],
            "quoteQty": f["notional"],
            "commission": f["fee"],
            "commissionAsset": f["fee_asset"],
            "time": ts_ms,
            "isBuyer": f["side"] == "buy",
            "isMaker": f["liquidity"] == "maker",
            "isBestMatch": True
        })
        
    result.sort(key=lambda x: x["time"])
    return result[:limit]
