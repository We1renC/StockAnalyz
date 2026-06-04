import hmac
import hashlib
import json
import time
import sqlite3
import asyncio
from datetime import datetime, UTC
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, List, Set, Any, Optional
from pathlib import Path

BASE = Path(__file__).parent.parent
DB = BASE / "portfolio.db"

def get_crypto_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

class WSConnection:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.authenticated = False
        self.api_key_id: Optional[str] = None
        self.account_id: Optional[str] = None
        # Format: {"channel_name": {"symbol1", "symbol2"}}
        self.subscribed_channels: Dict[str, Set[str]] = {}

class WebSocketManager:
    def __init__(self):
        self.connections: Set[WSConnection] = set()

    async def connect(self, websocket: WebSocket) -> WSConnection:
        await websocket.accept()
        conn = WSConnection(websocket)
        self.connections.add(conn)
        # Start heartbeat monitor for this connection
        asyncio.create_task(self._heartbeat(conn))
        return conn

    def disconnect(self, conn: WSConnection):
        self.connections.discard(conn)

    async def _heartbeat(self, conn: WSConnection):
        """Sends a ping every 30 seconds and checks for client presence."""
        try:
            while conn in self.connections:
                await asyncio.sleep(30)
                await conn.websocket.send_json({"op": "ping", "timestamp": int(time.time() * 1000)})
        except Exception:
            self.disconnect(conn)

    async def handle_message(self, conn: WSConnection, msg_str: str):
        try:
            msg = json.loads(msg_str)
        except json.JSONDecodeError:
            await conn.websocket.send_json({"success": False, "error": "Invalid JSON format"})
            return

        op = msg.get("op")
        if not op:
            await conn.websocket.send_json({"success": False, "error": "Missing field 'op'"})
            return

        if op == "auth":
            await self._handle_auth(conn, msg)
        elif op == "subscribe":
            await self._handle_subscribe(conn, msg)
        elif op == "unsubscribe":
            await self._handle_unsubscribe(conn, msg)
        elif op == "pong":
            # Client pong response
            pass
        else:
            await conn.websocket.send_json({"success": False, "error": f"Unknown operation '{op}'"})

    async def _handle_auth(self, conn: WSConnection, msg: Dict[str, Any]):
        api_key = msg.get("api_key")
        timestamp = msg.get("timestamp")
        nonce = msg.get("nonce")
        signature = msg.get("signature")

        if not api_key or not timestamp or not nonce or not signature:
            await conn.websocket.send_json({"op": "auth", "success": False, "error": "Missing auth credentials"})
            return

        # Signature verification: computed signature = HMAC-SHA256(timestamp + nonce, api_secret)
        api_key_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
        
        db = get_crypto_db()
        c = db.cursor()
        row = c.execute("SELECT * FROM crypto_api_keys WHERE api_key_hash = ?", (api_key_hash,)).fetchone()
        
        if not row:
            db.close()
            await conn.websocket.send_json({"op": "auth", "success": False, "error": "Invalid API Key"})
            return

        key_info = dict(row)
        if key_info["status"] != "active":
            db.close()
            await conn.websocket.send_json({"op": "auth", "success": False, "error": f"API Key status is {key_info['status']}"})
            return

        # Verify timestamp drift
        try:
            req_ts = int(timestamp)
        except ValueError:
            db.close()
            await conn.websocket.send_json({"op": "auth", "success": False, "error": "Invalid timestamp format"})
            return

        server_ts_ms = int(time.time() * 1000)
        if abs(server_ts_ms - req_ts) > 30000:
            db.close()
            await conn.websocket.send_json({"op": "auth", "success": False, "error": "Timestamp expired"})
            return

        # Verify nonce
        try:
            c.execute(
                "INSERT INTO crypto_nonces (api_key_id, nonce, timestamp) VALUES (?, ?, ?)",
                (key_info["id"], nonce, server_ts_ms)
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.close()
            await conn.websocket.send_json({"op": "auth", "success": False, "error": "Nonce reused"})
            return

        # Check signature: timestamp + nonce
        payload = f"{timestamp}{nonce}"
        secret_bytes = key_info["api_secret"].encode('utf-8')
        computed_signature = hmac.new(
            secret_bytes,
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed_signature, signature):
            db.close()
            await conn.websocket.send_json({"op": "auth", "success": False, "error": "Invalid signature"})
            return

        db.close()
        conn.authenticated = True
        conn.api_key_id = key_info["id"]
        conn.account_id = key_info["account_id"]
        
        await conn.websocket.send_json({
            "op": "auth",
            "success": True,
            "connection_id": f"conn_{id(conn)}"
        })

    async def _handle_subscribe(self, conn: WSConnection, msg: Dict[str, Any]):
        channels = msg.get("channels", [])
        if not channels:
            await conn.websocket.send_json({"success": False, "error": "Missing 'channels'"})
            return

        private_channels = {"orders", "fills", "balances", "risk", "account", "system"}
        
        subscribed = {}
        for chan in channels:
            name = chan.get("name")
            symbols = chan.get("symbols", ["*"]) # default to wildcard for symbols

            if not name:
                continue

            if name in private_channels and not conn.authenticated:
                await conn.websocket.send_json({
                    "success": False,
                    "error": f"Authentication required for private channel '{name}'"
                })
                continue

            if name not in conn.subscribed_channels:
                conn.subscribed_channels[name] = set()

            for sym in symbols:
                conn.subscribed_channels[name].add(sym)

            subscribed[name] = list(conn.subscribed_channels[name])

        await conn.websocket.send_json({
            "op": "subscribed",
            "subscribed": subscribed
        })

    async def _handle_unsubscribe(self, conn: WSConnection, msg: Dict[str, Any]):
        channels = msg.get("channels", [])
        if not channels:
            await conn.websocket.send_json({"success": False, "error": "Missing 'channels'"})
            return

        unsubscribed = {}
        for chan in channels:
            name = chan.get("name")
            symbols = chan.get("symbols", [])

            if not name or name not in conn.subscribed_channels:
                continue

            if not symbols or "*" in symbols:
                conn.subscribed_channels.pop(name, None)
                unsubscribed[name] = []
            else:
                for sym in symbols:
                    conn.subscribed_channels[name].discard(sym)
                unsubscribed[name] = list(conn.subscribed_channels[name])

        await conn.websocket.send_json({
            "op": "unsubscribed",
            "subscribed": unsubscribed
        })

    async def broadcast_public(self, channel: str, symbol: str, event_type: str, data: Dict[str, Any]):
        """Pushes events to any client subscribed to a public channel."""
        payload = {
            "channel": channel,
            "event": event_type,
            "symbol": symbol,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
        }
        for conn in list(self.connections):
            # Check subscription
            if channel in conn.subscribed_channels:
                syms = conn.subscribed_channels[channel]
                if "*" in syms or symbol in syms:
                    try:
                        await conn.websocket.send_json(payload)
                    except Exception:
                        self.disconnect(conn)

    async def push_private(self, account_id: str, channel: str, event_type: str, data: Dict[str, Any]):
        """Pushes private events to authenticated connections matching the account."""
        payload = {
            "channel": channel,
            "event": event_type,
            "sequence": int(time.time() * 1000), # Simple millisecond-based sequence ID
            "data": data,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z")
        }
        for conn in list(self.connections):
            if conn.authenticated and conn.account_id == account_id:
                if channel in conn.subscribed_channels:
                    syms = conn.subscribed_channels[channel]
                    # check if the event matches symbols subscribed or wildcard
                    symbol = data.get("symbol", "*")
                    if "*" in syms or symbol in syms or symbol == "*":
                        try:
                            await conn.websocket.send_json(payload)
                        except Exception:
                            self.disconnect(conn)

# Singleton manager
ws_manager = WebSocketManager()
