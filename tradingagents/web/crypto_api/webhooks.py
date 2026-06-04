import hmac
import hashlib
import json
import time
import sqlite3
import asyncio
import urllib.request
import urllib.error
from datetime import datetime, UTC
from typing import Dict, Any, List
from pathlib import Path
from uuid import uuid4

BASE = Path(__file__).parent.parent
DB = BASE / "portfolio.db"

def get_crypto_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

async def dispatch_webhook(account_id: str, event_type: str, data: Dict[str, Any]):
    """
    Looks up webhook endpoints registered for the account and event type,
    constructs the signed payload, and sends POST requests asynchronously.
    """
    # 1. Fetch endpoints
    conn = get_crypto_db()
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, url, events, secret FROM crypto_webhook_endpoints WHERE account_id = ? AND status = 'active'",
        (account_id,)
    ).fetchall()
    conn.close()

    if not rows:
        return

    # Filter endpoints that are subscribed to this event
    endpoints = []
    for r in rows:
        try:
            subscribed_events = json.loads(r["events"])
            if event_type in subscribed_events or "*" in subscribed_events:
                endpoints.append(dict(r))
        except json.JSONDecodeError:
            pass

    if not endpoints:
        return

    # 2. Prepare payload
    event_id = f"evt_{uuid4().hex[:12]}"
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "created_at": now_iso,
        "data": data
    }
    body_str = json.dumps(payload)
    body_bytes = body_str.encode('utf-8')

    # 3. Dispatch to all matching endpoints concurrently
    async def send_to_endpoint(ep: Dict[str, Any]):
        url = ep["url"]
        secret = ep["secret"]
        webhook_id = ep["id"]
        
        timestamp = str(int(time.time() * 1000))
        
        # Calculate signature: HMAC-SHA256(timestamp + body, secret)
        sig_payload = f"{timestamp}{body_str}"
        signature = hmac.new(
            secret.encode('utf-8'),
            sig_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Id": webhook_id,
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": signature,
            "User-Agent": "TradingAgents-Webhook/1.0"
        }

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=headers,
            method="POST"
        )

        try:
            # Send HTTP request asynchronously in a threadpool to avoid blocking event loop
            def perform_request():
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.read()

            await asyncio.to_thread(perform_request)
            print(f"Webhook {event_id} ({event_type}) sent successfully to {url}")
        except Exception as e:
            # We don't crash on webhook failure, we just log it
            print(f"Failed to send Webhook {event_id} ({event_type}) to {url}: {e}")

    await asyncio.gather(*(send_to_endpoint(ep) for ep in endpoints), return_exceptions=True)
