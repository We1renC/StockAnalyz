import sqlite3
import json
from datetime import datetime, UTC
from typing import Dict, Any, List, Optional
from decimal import Decimal

# Helper to serialize values to JSON/strings
def decimal_to_str(d: Optional[Decimal]) -> Optional[str]:
    return str(d) if d is not None else None

def init_crypto_db(conn: sqlite3.Connection):
    c = conn.cursor()
    
    # Optional: drop tables if structure changes (for development convenience, we drop the tables once to reset them)
    # We can do this safely since it's a new feature and there is no production data yet.
    c.executescript("""
    DROP TABLE IF EXISTS crypto_api_keys;
    DROP TABLE IF EXISTS crypto_markets;
    DROP TABLE IF EXISTS crypto_balances;
    DROP TABLE IF EXISTS crypto_orders;
    DROP TABLE IF EXISTS crypto_fills;
    DROP TABLE IF EXISTS crypto_ledger;
    DROP TABLE IF EXISTS crypto_audit_logs;
    DROP TABLE IF EXISTS crypto_risk_limits;
    DROP TABLE IF EXISTS crypto_kill_switch;
    DROP TABLE IF EXISTS crypto_webhook_endpoints;
    DROP TABLE IF EXISTS crypto_nonces;
    """)
    conn.commit()

    c.executescript("""
    -- API Keys table
    CREATE TABLE IF NOT EXISTS crypto_api_keys (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        name TEXT,
        api_key_hash TEXT UNIQUE NOT NULL,
        api_secret TEXT NOT NULL, -- Stored as plaintext/encrypted (we use plaintext for simplicity)
        scopes TEXT, -- JSON array of strings
        ip_whitelist TEXT, -- JSON array of strings
        status TEXT NOT NULL DEFAULT 'active',
        expires_at TEXT,
        last_used_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_crypto_api_keys_hash ON crypto_api_keys(api_key_hash);

    -- Markets table
    CREATE TABLE IF NOT EXISTS crypto_markets (
        symbol TEXT PRIMARY KEY,
        base_asset TEXT NOT NULL,
        quote_asset TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'trading',
        price_precision INTEGER NOT NULL,
        quantity_precision INTEGER NOT NULL,
        min_quantity TEXT NOT NULL,
        max_quantity TEXT NOT NULL,
        min_notional TEXT NOT NULL,
        tick_size TEXT NOT NULL,
        lot_size TEXT NOT NULL,
        maker_fee_rate TEXT NOT NULL,
        taker_fee_rate TEXT NOT NULL
    );

    -- Balances table
    CREATE TABLE IF NOT EXISTS crypto_balances (
        account_id TEXT NOT NULL,
        asset TEXT NOT NULL,
        available TEXT NOT NULL DEFAULT '0',
        locked TEXT NOT NULL DEFAULT '0',
        total TEXT NOT NULL DEFAULT '0',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (account_id, asset)
    );

    -- Orders table
    CREATE TABLE IF NOT EXISTS crypto_orders (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        exchange TEXT NOT NULL DEFAULT 'simulated',
        exchange_order_id TEXT,
        client_order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        price TEXT,
        quantity TEXT,
        quote_quantity TEXT,
        stop_price TEXT,
        filled_quantity TEXT NOT NULL DEFAULT '0',
        remaining_quantity TEXT NOT NULL,
        average_price TEXT,
        fee TEXT NOT NULL DEFAULT '0',
        fee_asset TEXT,
        time_in_force TEXT NOT NULL DEFAULT 'GTC',
        post_only INTEGER NOT NULL DEFAULT 0,
        self_trade_prevention TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_crypto_orders_account ON crypto_orders(account_id);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_crypto_orders_client_id ON crypto_orders(account_id, client_order_id);
    CREATE INDEX IF NOT EXISTS idx_crypto_orders_symbol ON crypto_orders(symbol);
    CREATE INDEX IF NOT EXISTS idx_crypto_orders_status ON crypto_orders(status);

    -- Fills table
    CREATE TABLE IF NOT EXISTS crypto_fills (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        client_order_id TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        price TEXT NOT NULL,
        quantity TEXT NOT NULL,
        notional TEXT NOT NULL,
        fee TEXT NOT NULL,
        fee_asset TEXT NOT NULL,
        liquidity TEXT NOT NULL, -- maker / taker
        executed_at TEXT NOT NULL,
        FOREIGN KEY(order_id) REFERENCES crypto_orders(id)
    );
    CREATE INDEX IF NOT EXISTS idx_crypto_fills_order ON crypto_fills(order_id);
    CREATE INDEX IF NOT EXISTS idx_crypto_fills_account ON crypto_fills(account_id);

    -- Ledger table
    CREATE TABLE IF NOT EXISTS crypto_ledger (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        asset TEXT NOT NULL,
        type TEXT NOT NULL, -- trade / fee / adjustment
        amount TEXT NOT NULL,
        balance_after TEXT NOT NULL,
        reference_type TEXT NOT NULL, -- fill / fee / adjustment
        reference_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_crypto_ledger_account ON crypto_ledger(account_id, asset);

    -- Audit Logs table
    CREATE TABLE IF NOT EXISTS crypto_audit_logs (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        api_key_id TEXT,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT,
        ip_address TEXT,
        user_agent TEXT,
        request_id TEXT,
        result TEXT NOT NULL, -- success / failed
        metadata TEXT, -- JSON string
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_crypto_audit_logs_created ON crypto_audit_logs(created_at DESC);

    -- Risk Limits table
    CREATE TABLE IF NOT EXISTS crypto_risk_limits (
        account_id TEXT PRIMARY KEY,
        max_single_order_notional TEXT NOT NULL DEFAULT '10000',
        max_daily_notional TEXT NOT NULL DEFAULT '100000',
        max_open_orders INTEGER NOT NULL DEFAULT 100,
        max_price_deviation_percent TEXT NOT NULL DEFAULT '5',
        max_asset_exposure TEXT NOT NULL DEFAULT '{}', -- JSON map: asset -> limit
        allowed_symbols TEXT NOT NULL DEFAULT '[]', -- JSON array
        blocked_symbols TEXT NOT NULL DEFAULT '[]', -- JSON array
        updated_at TEXT NOT NULL
    );

    -- Kill Switch table
    CREATE TABLE IF NOT EXISTS crypto_kill_switch (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        active INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        activated_at TEXT,
        activated_by TEXT
    );

    -- Webhook Endpoints table
    CREATE TABLE IF NOT EXISTS crypto_webhook_endpoints (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        url TEXT NOT NULL,
        events TEXT NOT NULL, -- JSON array of strings
        secret TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    );

    -- Nonce table to prevent replay attacks
    CREATE TABLE IF NOT EXISTS crypto_nonces (
        api_key_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        timestamp INTEGER NOT NULL, -- Server time received (milliseconds)
        PRIMARY KEY (api_key_id, nonce)
    );
    CREATE INDEX IF NOT EXISTS idx_crypto_nonces_timestamp ON crypto_nonces(timestamp);
    """)
    conn.commit()

def seed_crypto_data(conn: sqlite3.Connection):
    c = conn.cursor()
    
    # 1. Seed standard markets if they don't exist
    markets = [
        ("BTC-USDT", "BTC", "USDT", "trading", 2, 6, "0.00001", "100", "5", "0.01", "0.000001", "0.001", "0.001"),
        ("ETH-USDT", "ETH", "USDT", "trading", 2, 5, "0.0001", "1000", "5", "0.01", "0.00001", "0.0015", "0.002"),
        ("SOL-USDT", "SOL", "USDT", "trading", 3, 3, "0.01", "10000", "5", "0.001", "0.001", "0.002", "0.0025"),
        ("XRP-USDT", "XRP", "USDT", "trading", 4, 1, "0.1", "100000", "5", "0.0001", "0.1", "0.002", "0.0025")
    ]
    for m in markets:
        c.execute("""
        INSERT OR IGNORE INTO crypto_markets 
        (symbol, base_asset, quote_asset, status, price_precision, quantity_precision, min_quantity, max_quantity, min_notional, tick_size, lot_size, maker_fee_rate, taker_fee_rate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, m)

    # 2. Seed default API key if not exists
    # Key: api_key_xxx, Secret: secret_xxx
    import hashlib
    api_key = "api_key_xxx"
    api_secret = "secret_xxx"
    key_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
    
    scopes = json.dumps([
        "read:market", "read:account", "read:orders", "read:fills",
        "trade:spot", "risk:read", "risk:write", "webhook:read", "webhook:write",
        "audit:read", "admin:keys", "admin:system"
    ])
    ip_whitelist = json.dumps(["127.0.0.1", "203.0.113.10"])
    
    c.execute("""
    INSERT OR IGNORE INTO crypto_api_keys
    (id, account_id, name, api_key_hash, api_secret, scopes, ip_whitelist, status, expires_at, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("key_123", "acct_123", "default-test-key", key_hash, api_secret, scopes, ip_whitelist, "active", "2030-12-31T23:59:59Z", "2026-06-04T10:00:00Z"))

    # 3. Seed default balances for acct_123
    balances = [
        ("acct_123", "USDT", "100000.00", "0.00", "100000.00"),
        ("acct_123", "BTC", "2.000000", "0.00", "2.000000"),
        ("acct_123", "ETH", "10.00000", "0.00", "10.00000"),
        ("acct_123", "SOL", "100.000", "0.00", "100.000")
    ]
    for bal in balances:
        c.execute("""
        INSERT OR IGNORE INTO crypto_balances
        (account_id, asset, available, locked, total, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (bal[0], bal[1], bal[2], bal[3], bal[4], "2026-06-04T10:00:00Z"))

    # 4. Seed default risk limits for acct_123
    allowed_syms = json.dumps(["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"])
    c.execute("""
    INSERT OR IGNORE INTO crypto_risk_limits
    (account_id, max_single_order_notional, max_daily_notional, max_open_orders, max_price_deviation_percent, max_asset_exposure, allowed_symbols, blocked_symbols, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("acct_123", "10000.00", "100000.00", 100, "5.00", "{}", allowed_syms, "[]", "2026-06-04T10:00:00Z"))

    # 5. Seed default kill switch state
    c.execute("""
    INSERT OR IGNORE INTO crypto_kill_switch (id, active, reason, activated_at, activated_by)
    VALUES (1, 0, NULL, NULL, NULL)
    """)
    
    conn.commit()
