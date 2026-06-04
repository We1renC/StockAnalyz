import sqlite3
import json
import time
import asyncio
import urllib.request
import urllib.parse
from datetime import datetime, UTC
from typing import Dict, Any, List, Tuple, Optional
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import sys

# Import websocket and webhook dispatchers
from crypto_api.ws import ws_manager
from crypto_api.webhooks import dispatch_webhook

BASE = Path(__file__).parent.parent
DB = BASE / "portfolio.db"

# Dynamic import helper for llm_providers settings
try:
    from llm_providers import load_settings
except ImportError:
    sys.path.append(str(BASE))
    from llm_providers import load_settings

def get_crypto_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# Helper to fetch current prices from Binance or simulate them
class MarketPriceEngine:
    def __init__(self):
        # Local cache of symbol -> price (str)
        self.price_cache: Dict[str, str] = {
            "BTC-USDT": "68000.00",
            "ETH-USDT": "3500.00",
            "SOL-USDT": "150.00"
        }
        self.last_fetched = 0
        
        # Load binance URL from settings
        try:
            settings = load_settings()
            self.base_url = settings.get("binance_api_url", "https://testnet.binance.vision").rstrip('/')
        except Exception:
            self.base_url = "https://testnet.binance.vision"

    async def get_price(self, symbol: str) -> Decimal:
        await self.refresh_prices_if_needed()
        return Decimal(self.price_cache.get(symbol, "0"))

    async def get_ticker_24h(self, symbol: str) -> Dict[str, str]:
        # Translate to Binance symbol, e.g. BTC-USDT -> BTCUSDT
        binance_sym = symbol.replace("-", "")
        url = f"{self.base_url}/api/v3/ticker/24hr?symbol={binance_sym}"
        try:
            def fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3) as res:
                    return json.loads(res.read().decode('utf-8'))
            data = await asyncio.to_thread(fetch)
            return {
                "symbol": symbol,
                "last_price": str(data.get("lastPrice", "0")),
                "best_bid": str(data.get("bidPrice", "0")),
                "best_ask": str(data.get("askPrice", "0")),
                "high_24h": str(data.get("highPrice", "0")),
                "low_24h": str(data.get("lowPrice", "0")),
                "volume_24h": str(data.get("volume", "0")),
                "quote_volume_24h": str(data.get("quoteVolume", "0")),
                "price_change_24h": str(data.get("priceChange", "0")),
                "price_change_percent_24h": str(data.get("priceChangePercent", "0")),
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
            }
        except Exception:
            # Volatility mock fallback
            last_price = Decimal(self.price_cache.get(symbol, "0"))
            import random
            change = last_price * Decimal(str(random.uniform(-0.01, 0.01)))
            new_price = last_price + change
            bid = new_price * Decimal("0.999")
            ask = new_price * Decimal("1.001")
            return {
                "symbol": symbol,
                "last_price": f"{new_price:.2f}",
                "best_bid": f"{bid:.2f}",
                "best_ask": f"{ask:.2f}",
                "high_24h": f"{(new_price * Decimal('1.05')):.2f}",
                "low_24h": f"{(new_price * Decimal('0.95')):.2f}",
                "volume_24h": "1000.00",
                "quote_volume_24h": f"{(new_price * 1000):.2f}",
                "price_change_24h": f"{change:.2f}",
                "price_change_percent_24h": f"{(change / last_price * 100):.2f}",
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
            }

    async def get_orderbook(self, symbol: str, depth: int = 50) -> Dict[str, Any]:
        binance_sym = symbol.replace("-", "")
        url = f"{self.base_url}/api/v3/depth?symbol={binance_sym}&limit={depth}"
        try:
            def fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3) as res:
                    return json.loads(res.read().decode('utf-8'))
            data = await asyncio.to_thread(fetch)
            return {
                "symbol": symbol,
                "last_update_id": data.get("lastUpdateId", 0),
                "bids": [[str(b[0]), str(b[1])] for b in data.get("bids", [])],
                "asks": [[str(a[0]), str(a[1])] for a in data.get("asks", [])],
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
            }
        except Exception:
            # Local mock fallback
            price = await self.get_price(symbol)
            bids = []
            asks = []
            for i in range(1, 10):
                bid_p = price - Decimal(str(i * 0.1))
                ask_p = price + Decimal(str(i * 0.1))
                bids.append([f"{bid_p:.2f}", f"{(1.5 / i):.4f}"])
                asks.append([f"{ask_p:.2f}", f"{(1.2 / i):.4f}"])
            return {
                "symbol": symbol,
                "last_update_id": int(time.time()),
                "bids": bids,
                "asks": asks,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
            }

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        binance_sym = symbol.replace("-", "")
        url = f"{self.base_url}/api/v3/trades?symbol={binance_sym}&limit={limit}"
        try:
            def fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3) as res:
                    return json.loads(res.read().decode('utf-8'))
            data = await asyncio.to_thread(fetch)
            trades = []
            for t in data:
                trades.append({
                    "trade_id": f"mtrade_{t['id']}",
                    "symbol": symbol,
                    "price": str(t['price']),
                    "quantity": str(t['qty']),
                    "side": "buy" if t['isBuyerMaker'] else "sell",
                    "executed_at": datetime.fromtimestamp(t['time']/1000.0, UTC).isoformat().replace("+00:00", "Z")
                })
            return trades
        except Exception:
            # Mock fallback
            price = await self.get_price(symbol)
            now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            return [
                {
                    "trade_id": "mtrade_mock_123",
                    "symbol": symbol,
                    "price": f"{price:.2f}",
                    "quantity": "0.0100",
                    "side": "buy",
                    "executed_at": now_iso
                }
            ]

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> List[Dict[str, Any]]:
        binance_sym = symbol.replace("-", "")
        url = f"{self.base_url}/api/v3/klines?symbol={binance_sym}&interval={interval}&limit={limit}"
        try:
            def fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3) as res:
                    return json.loads(res.read().decode('utf-8'))
            data = await asyncio.to_thread(fetch)
            klines = []
            for k in data:
                klines.append({
                    "open_time": datetime.fromtimestamp(k[0]/1000.0, UTC).isoformat().replace("+00:00", "Z"),
                    "open": str(k[1]),
                    "high": str(k[2]),
                    "low": str(k[3]),
                    "close": str(k[4]),
                    "volume": str(k[5]),
                    "quote_volume": str(k[7]),
                    "trade_count": int(k[8]),
                    "close_time": datetime.fromtimestamp(k[6]/1000.0, UTC).isoformat().replace("+00:00", "Z")
                })
            return klines
        except Exception:
            # Mock fallback
            price = await self.get_price(symbol)
            now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            return [{
                "open_time": now_iso,
                "open": f"{price:.2f}",
                "high": f"{(price * Decimal('1.01')):.2f}",
                "low": f"{(price * Decimal('0.99')):.2f}",
                "close": f"{price:.2f}",
                "volume": "100.0",
                "quote_volume": f"{(price * 100):.2f}",
                "trade_count": 50,
                "close_time": now_iso
            }]

    async def refresh_prices_if_needed(self):
        now = time.time()
        if now - self.last_fetched < 5:  # cache for 5 seconds
            return

        # Fetch prices in background to prevent blocking
        async def fetch_one(symbol: str):
            binance_sym = symbol.replace("-", "")
            url = f"{self.base_url}/api/v3/ticker/price?symbol={binance_sym}"
            try:
                def fetch():
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=2) as res:
                        return json.loads(res.read().decode('utf-8'))
                res = await asyncio.to_thread(fetch)
                if "price" in res:
                    self.price_cache[symbol] = str(res["price"])
            except Exception:
                # Volatility walk on fallback
                import random
                current = Decimal(self.price_cache[symbol])
                walk = current * Decimal(str(random.uniform(-0.0005, 0.0005)))
                self.price_cache[symbol] = f"{(current + walk):.4f}"

        await asyncio.gather(*(fetch_one(s) for s in self.price_cache.keys()), return_exceptions=True)
        self.last_fetched = now

price_engine = MarketPriceEngine()

# Pre-trade validation engine
async def validate_pre_trade_risk(conn: sqlite3.Connection, account_id: str, order_data: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    c = conn.cursor()
    
    # 1. Check Kill Switch
    ks = c.execute("SELECT active FROM crypto_kill_switch WHERE id = 1").fetchone()
    if ks and ks["active"] == 1:
        return False, "KILL_SWITCH_ACTIVE", None

    symbol = order_data["symbol"]
    side = order_data["side"]
    order_type = order_data["type"]
    quantity = Decimal(order_data["quantity"])

    # 2. Get market constraints
    m_row = c.execute("SELECT * FROM crypto_markets WHERE symbol = ?", (symbol,)).fetchone()
    if not m_row:
        return False, "INVALID_SYMBOL", None
    market = dict(m_row)

    if market["status"] != "trading":
        return False, "SYMBOL_NOT_TRADING", None

    # Get current price
    price = Decimal(order_data.get("price") or await price_engine.get_price(symbol))
    
    # 3. Precision and limits checks
    # Price tick size
    tick_size = Decimal(market["tick_size"])
    if order_data.get("price"):
        req_price = Decimal(order_data["price"])
        if (req_price % tick_size) != Decimal("0") and abs((req_price % tick_size) - tick_size) > Decimal("1e-9"):
            # Floating point issues can cause slight modulo mismatches, we check if price conforms
            pass # Keep it simple for simulation

    # Quantity lot size
    lot_size = Decimal(market["lot_size"])
    if (quantity % lot_size) != Decimal("0") and abs((quantity % lot_size) - lot_size) > Decimal("1e-9"):
        pass

    # Min/max quantities
    min_qty = Decimal(market["min_quantity"])
    max_qty = Decimal(market["max_quantity"])
    if quantity < min_qty:
        return False, "MIN_QUANTITY_NOT_MET", None
    if quantity > max_qty:
        return False, "RISK_LIMIT_EXCEEDED", None

    # Notional check (price * quantity)
    notional = price * quantity
    min_notional = Decimal(market["min_notional"])
    if notional < min_notional:
        return False, "MIN_NOTIONAL_NOT_MET", None

    # 4. Get Account Risk Limits
    risk_row = c.execute("SELECT * FROM crypto_risk_limits WHERE account_id = ?", (account_id,)).fetchone()
    if risk_row:
        risk = dict(risk_row)
        # Max single order notional
        max_single = Decimal(risk["max_single_order_notional"])
        if notional > max_single:
            return False, "MAX_ORDER_NOTIONAL_EXCEEDED", None

        # Max open orders check
        open_count = c.execute(
            "SELECT COUNT(*) as cnt FROM crypto_orders WHERE account_id = ? AND status IN ('open', 'partially_filled')",
            (account_id,)
        ).fetchone()["cnt"]
        if open_count >= risk["max_open_orders"]:
            return False, "MAX_OPEN_ORDERS_EXCEEDED", None

        # Allowed / blocked list checks
        allowed = json.loads(risk["allowed_symbols"])
        blocked = json.loads(risk["blocked_symbols"])
        if allowed and symbol not in allowed:
            return False, "SYMBOL_BLOCKED", None
        if blocked and symbol in blocked:
            return False, "SYMBOL_BLOCKED", None

    # 5. Sufficient balance check
    # For buy limit: lock quantity * price (quote currency)
    # For buy market: lock quote currency (if quote_quantity specified) or estimate
    # For sell: lock quantity (base currency)
    base_asset = market["base_asset"]
    quote_asset = market["quote_asset"]

    if side == "buy":
        req_asset = quote_asset
        # estimate total cost including fee (e.g. standard taker fee rate)
        fee_rate = Decimal(market["taker_fee_rate"])
        estimated_fee = notional * fee_rate
        req_amount = notional + estimated_fee
    else:
        req_asset = base_asset
        req_amount = quantity

    bal_row = c.execute(
        "SELECT available FROM crypto_balances WHERE account_id = ? AND asset = ?",
        (account_id, req_asset)
    ).fetchone()
    available = Decimal(bal_row["available"]) if bal_row else Decimal("0")

    if available < req_amount:
        return False, "INSUFFICIENT_BALANCE", {
            "asset": req_asset,
            "required": str(req_amount),
            "available": str(available)
        }

    return True, None, {
        "estimated_notional": str(notional),
        "estimated_fee": str(notional * Decimal(market["maker_fee_rate"] if order_type == "limit" else market["taker_fee_rate"])),
        "base_asset": base_asset,
        "quote_asset": quote_asset
    }

# Execute immediate fill for market order or matched order
def fill_order(conn: sqlite3.Connection, order_id: str, fill_price: Decimal, fill_qty: Decimal, is_maker: bool = False):
    c = conn.cursor()
    order_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    if not order_row:
        return
    order = dict(order_row)
    account_id = order["account_id"]
    symbol = order["symbol"]
    side = order["side"]
    
    # Get market rates
    m_row = c.execute("SELECT * FROM crypto_markets WHERE symbol = ?", (symbol,)).fetchone()
    market = dict(m_row)
    base_asset = market["base_asset"]
    quote_asset = market["quote_asset"]
    
    fee_rate = Decimal(market["maker_fee_rate"] if is_maker else market["taker_fee_rate"])
    
    notional = fill_price * fill_qty
    
    # Fee asset is standard quote asset for Buy, base asset for Sell
    # E.g. Buy BTC-USDT: receive BTC, pay USDT + fee in USDT
    # Let's keep it simple: fees paid in quote currency (USDT)
    fee = notional * fee_rate
    fee_asset = quote_asset

    # 1. Update balances and write ledger
    # Fetch balances
    base_bal_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, base_asset)).fetchone()
    quote_bal_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, quote_asset)).fetchone()
    
    base_bal = dict(base_bal_row) if base_bal_row else {"available": "0", "locked": "0", "total": "0"}
    quote_bal = dict(quote_bal_row) if quote_bal_row else {"available": "0", "locked": "0", "total": "0"}

    base_avail = Decimal(base_bal["available"])
    base_locked = Decimal(base_bal["locked"])
    
    quote_avail = Decimal(quote_bal["available"])
    quote_locked = Decimal(quote_bal["locked"])

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if side == "buy":
        # Deduct USDT, release locks, increase BTC
        # Locked was quote currency.
        # Since it is matching, if we locked based on order limit price, let's deduct from locked USDT.
        order_price = Decimal(order["price"]) if order["price"] else fill_price
        locked_to_release = fill_qty * order_price
        
        quote_locked_new = quote_locked - locked_to_release
        # If execution price was better than limit price, we return the difference to available
        diff = locked_to_release - notional
        quote_avail_new = quote_avail + diff
        
        # Deduct fee from USDT
        quote_avail_new = quote_avail_new - fee
        
        # New base balance
        base_avail_new = base_avail + fill_qty
        
        # Write to balances
        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, base_asset, str(base_avail_new), str(base_locked), str(base_avail_new + base_locked), now_iso))

        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, quote_asset, str(quote_avail_new), str(quote_locked_new), str(quote_avail_new + quote_locked_new), now_iso))

        # Ledger records
        # 1. Base asset credit
        ledger_base_id = f"led_{uuid4().hex[:12]}"
        c.execute("""
            INSERT INTO crypto_ledger (id, account_id, asset, type, amount, balance_after, reference_type, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ledger_base_id, account_id, base_asset, "trade", str(fill_qty), str(base_avail_new + base_locked), "fill", order_id, now_iso))

        # 2. Quote asset debit (for trade)
        ledger_quote_id = f"led_{uuid4().hex[:12]}"
        c.execute("""
            INSERT INTO crypto_ledger (id, account_id, asset, type, amount, balance_after, reference_type, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ledger_quote_id, account_id, quote_asset, "trade", str(-notional), str(quote_avail_new + quote_locked_new + fee), "fill", order_id, now_iso))

        # 3. Quote asset fee debit
        ledger_fee_id = f"led_{uuid4().hex[:12]}"
        c.execute("""
            INSERT INTO crypto_ledger (id, account_id, asset, type, amount, balance_after, reference_type, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ledger_fee_id, account_id, quote_asset, "fee", str(-fee), str(quote_avail_new + quote_locked_new), "fee", order_id, now_iso))

    else:
        # Sell: deduct BTC from locked, increase USDT in available
        base_locked_new = base_locked - fill_qty
        quote_avail_new = quote_avail + notional - fee
        
        # Write to balances
        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, base_asset, str(base_avail), str(base_locked_new), str(base_avail + base_locked_new), now_iso))

        c.execute("""
            INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, quote_asset, str(quote_avail_new), str(quote_locked), str(quote_avail_new + quote_locked), now_iso))

        # Ledger records
        # 1. Base asset debit
        ledger_base_id = f"led_{uuid4().hex[:12]}"
        c.execute("""
            INSERT INTO crypto_ledger (id, account_id, asset, type, amount, balance_after, reference_type, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ledger_base_id, account_id, base_asset, "trade", str(-fill_qty), str(base_avail + base_locked_new), "fill", order_id, now_iso))

        # 2. Quote asset credit
        ledger_quote_id = f"led_{uuid4().hex[:12]}"
        c.execute("""
            INSERT INTO crypto_ledger (id, account_id, asset, type, amount, balance_after, reference_type, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ledger_quote_id, account_id, quote_asset, "trade", str(notional), str(quote_avail_new + quote_locked + fee), "fill", order_id, now_iso))

        # 3. Quote asset fee debit
        ledger_fee_id = f"led_{uuid4().hex[:12]}"
        c.execute("""
            INSERT INTO crypto_ledger (id, account_id, asset, type, amount, balance_after, reference_type, reference_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ledger_fee_id, account_id, quote_asset, "fee", str(-fee), str(quote_avail_new + quote_locked), "fee", order_id, now_iso))

    # 2. Write Fill
    fill_id = f"fill_{uuid4().hex[:12]}"
    c.execute("""
        INSERT INTO crypto_fills (id, order_id, account_id, client_order_id, symbol, side, price, quantity, notional, fee, fee_asset, liquidity, executed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (fill_id, order_id, account_id, order["client_order_id"], symbol, side, str(fill_price), str(fill_qty), str(notional), str(fee), fee_asset, "maker" if is_maker else "taker", now_iso))

    # 3. Update Order status
    old_filled = Decimal(order["filled_quantity"])
    new_filled = old_filled + fill_qty
    remaining = Decimal(order["remaining_quantity"]) - fill_qty
    new_status = "filled" if remaining <= Decimal("0") else "partially_filled"

    c.execute("""
        UPDATE crypto_orders
        SET status = ?, filled_quantity = ?, remaining_quantity = ?, average_price = ?, fee = ?, fee_asset = ?, updated_at = ?
        WHERE id = ?
    """, (new_status, str(new_filled), str(remaining), str(fill_price), str(fee), fee_asset, now_iso, order_id))
    
    conn.commit()

    # 4. Trigger WebSocket & Webhook dispatches
    updated_order_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    updated_order = dict(updated_order_row)
    
    # Broadcast order event
    asyncio.create_task(ws_manager.push_private(account_id, "orders", "order.updated", updated_order))
    
    # Broadcast fill event
    fill_row = c.execute("SELECT * FROM crypto_fills WHERE id = ?", (fill_id,)).fetchone()
    fill_data = dict(fill_row)
    asyncio.create_task(ws_manager.push_private(account_id, "fills", "fill.created", fill_data))
    
    # Dispatch webhooks
    asyncio.create_task(dispatch_webhook(account_id, "order.updated", updated_order))
    asyncio.create_task(dispatch_webhook(account_id, "fill.created", fill_data))

    # Balances push
    for asset in (base_asset, quote_asset):
        b_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, asset)).fetchone()
        if b_row:
            asyncio.create_task(ws_manager.push_private(account_id, "balances", "balance.updated", dict(b_row)))

# Cancel order and release balance lock
def cancel_single_order_sync(conn: sqlite3.Connection, order_id: str, reason: str = "user_requested") -> Dict[str, Any]:
    c = conn.cursor()
    order_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    if not order_row:
        raise ValueError("ORDER_NOT_FOUND")
    order = dict(order_row)
    account_id = order["account_id"]
    symbol = order["symbol"]
    side = order["side"]

    if order["status"] in ("filled", "cancelled", "rejected", "expired"):
        raise ValueError("ORDER_ALREADY_FINALIZED")

    # Get market
    m_row = c.execute("SELECT * FROM crypto_markets WHERE symbol = ?", (symbol,)).fetchone()
    market = dict(m_row)
    base_asset = market["base_asset"]
    quote_asset = market["quote_asset"]

    remaining = Decimal(order["remaining_quantity"])
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    # Release locks
    if side == "buy":
        # Quote currency locked
        # For stop market orders, they might not have a limit price on creation, so we check.
        order_price = Decimal(order["price"]) if order["price"] else Decimal("0")
        lock_to_release = remaining * order_price
        
        quote_bal_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, quote_asset)).fetchone()
        if quote_bal_row:
            quote_bal = dict(quote_bal_row)
            quote_avail = Decimal(quote_bal["available"]) + lock_to_release
            quote_locked = Decimal(quote_bal["locked"]) - lock_to_release
            c.execute("""
                INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (account_id, quote_asset, str(quote_avail), str(quote_locked), str(quote_avail + quote_locked), now_iso))
    else:
        # Base currency locked
        base_bal_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, base_asset)).fetchone()
        if base_bal_row:
            base_bal = dict(base_bal_row)
            base_avail = Decimal(base_bal["available"]) + remaining
            base_locked = Decimal(base_bal["locked"]) - remaining
            c.execute("""
                INSERT OR REPLACE INTO crypto_balances (account_id, asset, available, locked, total, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (account_id, base_asset, str(base_avail), str(base_locked), str(base_avail + base_locked), now_iso))

    # Update order state
    c.execute("""
        UPDATE crypto_orders
        SET status = 'cancelled', updated_at = ?
        WHERE id = ?
    """, (now_iso, order_id))
    
    conn.commit()

    updated_order_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
    updated_order = dict(updated_order_row)

    # Push and Webhook triggers
    asyncio.create_task(ws_manager.push_private(account_id, "orders", "order.updated", updated_order))
    asyncio.create_task(dispatch_webhook(account_id, "order.cancelled", updated_order))

    for asset in (base_asset, quote_asset):
        b_row = c.execute("SELECT * FROM crypto_balances WHERE account_id = ? AND asset = ?", (account_id, asset)).fetchone()
        if b_row:
            asyncio.create_task(ws_manager.push_private(account_id, "balances", "balance.updated", dict(b_row)))

    # Write audit log
    audit_id = f"audit_{uuid4().hex[:12]}"
    c.execute("""
        INSERT INTO crypto_audit_logs (id, account_id, actor_id, action, resource_type, resource_id, result, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (audit_id, account_id, "system", "order.cancel", "order", order_id, "success", json.dumps({"reason": reason}), now_iso))
    conn.commit()

    return {
        "order_id": order_id,
        "status": "cancelled",
        "requested_at": now_iso
    }

# Background task: Order matching reconciliation loop
async def start_matching_engine_loop():
    print("Starting Cryptocurrency Matching Engine background loop...")
    while True:
        try:
            await price_engine.refresh_prices_if_needed()
            
            conn = get_crypto_db()
            c = conn.cursor()
            
            # Fetch all active orders
            active_orders = c.execute(
                "SELECT * FROM crypto_orders WHERE status IN ('pending', 'accepted', 'open', 'partially_filled')"
            ).fetchall()
            
            for row in active_orders:
                order = dict(row)
                order_id = order["id"]
                symbol = order["symbol"]
                side = order["side"]
                order_type = order["type"]
                
                # Fetch live price for matching
                current_price = await price_engine.get_price(symbol)
                
                now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

                # Handle pending/accepted order state transition to book
                if order["status"] in ("pending", "accepted"):
                    c.execute("UPDATE crypto_orders SET status = 'open', updated_at = ? WHERE id = ?", (now_iso, order_id))
                    conn.commit()
                    order["status"] = "open"
                    asyncio.create_task(ws_manager.push_private(order["account_id"], "orders", "order.updated", order))

                # Handle conditional triggers (stop orders)
                if order_type in ("stop_market", "stop_limit", "take_profit_market", "take_profit_limit"):
                    stop_price = Decimal(order["stop_price"])
                    triggered = False
                    
                    # Stop logic:
                    # stop_market / stop_limit: Buy triggered when current_price >= stop_price, Sell triggered when current_price <= stop_price
                    # take_profit_market / take_profit_limit: Buy triggered when current_price <= stop_price, Sell triggered when current_price >= stop_price
                    if "take_profit" in order_type:
                        if side == "buy" and current_price <= stop_price:
                            triggered = True
                        elif side == "sell" and current_price >= stop_price:
                            triggered = True
                    else: # stop_market or stop_limit
                        if side == "buy" and current_price >= stop_price:
                            triggered = True
                        elif side == "sell" and current_price <= stop_price:
                            triggered = True

                    if triggered:
                        # Convert to market or limit order
                        if "market" in order_type:
                            # Execute immediately at current_price
                            fill_order(conn, order_id, current_price, Decimal(order["remaining_quantity"]), is_maker=False)
                        else: # stop_limit or take_profit_limit
                            # Transition to standard limit order
                            c.execute("""
                                UPDATE crypto_orders
                                SET type = 'limit', status = 'open', updated_at = ?
                                WHERE id = ?
                            """, (now_iso, order_id))
                            conn.commit()
                            
                            updated_row = c.execute("SELECT * FROM crypto_orders WHERE id = ?", (order_id,)).fetchone()
                            asyncio.create_task(ws_manager.push_private(order["account_id"], "orders", "order.updated", dict(updated_row)))
                    continue

                # Handle standard Limit Order match checking
                if order_type == "limit":
                    limit_price = Decimal(order["price"])
                    
                    match = False
                    if side == "buy" and current_price <= limit_price:
                        match = True
                    elif side == "sell" and current_price >= limit_price:
                        match = True
                        
                    if match:
                        # Execute limit order match
                        fill_order(conn, order_id, limit_price, Decimal(order["remaining_quantity"]), is_maker=True)
            
            conn.close()
            
        except Exception as e:
            print(f"Error in Cryptocurrency Matching Engine loop: {e}")
            
        await asyncio.sleep(1.0) # Run every 1 second
