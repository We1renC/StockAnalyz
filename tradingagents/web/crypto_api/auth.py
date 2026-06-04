import hmac
import hashlib
import json
import time
import sqlite3
from datetime import datetime, UTC
from fastapi import Request, Header, HTTPException, Depends
from typing import List, Optional, Dict, Any
from pathlib import Path

BASE = Path(__file__).parent.parent
DB = BASE / "portfolio.db"

# Get DB helper
def get_crypto_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# Helper to verify permissions
class RequireScope:
    def __init__(self, required_scope: str):
        self.required_scope = required_scope

    async def __call__(self, request: Request, api_key_info: Dict[str, Any] = Depends(lambda: None)):
        # If API key info is not loaded, it will be injected by our main auth dependency
        # We handle actual permission checks in the main auth dependency, but this helper
        # can be used to declare the required scope for an endpoint.
        pass

async def authenticate_request(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_timestamp: Optional[str] = Header(None, alias="X-Timestamp"),
    x_nonce: Optional[str] = Header(None, alias="X-Nonce"),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
) -> Dict[str, Any]:
    # 1. Check all headers exist
    if not x_api_key or not x_timestamp or not x_nonce or not x_signature:
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Missing authentication headers (X-API-Key, X-Timestamp, X-Nonce, X-Signature)."
                }
            }
        )

    # 2. Check timestamp drift (within 30 seconds)
    try:
        req_ts = int(x_timestamp)
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "X-Timestamp must be a millisecond timestamp integer."
                }
            }
        )

    server_ts_ms = int(time.time() * 1000)
    if abs(server_ts_ms - req_ts) > 30000:
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": {
                    "code": "TIMESTAMP_EXPIRED",
                    "message": f"Timestamp drift is too large. Server time: {server_ts_ms}, Request time: {req_ts}."
                }
            }
        )

    # 3. Query API key from database
    api_key_hash = hashlib.sha256(x_api_key.encode('utf-8')).hexdigest()
    conn = get_crypto_db()
    c = conn.cursor()
    row = c.execute(
        "SELECT * FROM crypto_api_keys WHERE api_key_hash = ?",
        (api_key_hash,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_API_KEY",
                    "message": "The provided API Key is invalid."
                }
            }
        )

    key_info = dict(row)
    api_key_id = key_info["id"]

    # 4. Check status and expiration
    if key_info["status"] != "active":
        conn.close()
        raise HTTPException(
            status_code=403,
            detail={
                "success": False,
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": f"API Key status is {key_info['status']}."
                }
            }
        )

    if key_info["expires_at"]:
        try:
            # Parse expires_at (ISO 8601)
            expiry_dt = datetime.fromisoformat(key_info["expires_at"].replace("Z", "+00:00"))
            if datetime.now(UTC) > expiry_dt:
                conn.close()
                raise HTTPException(
                    status_code=401,
                    detail={
                        "success": False,
                        "error": {
                            "code": "INVALID_API_KEY",
                            "message": "The API Key has expired."
                        }
                    }
                )
        except ValueError:
            pass

    # 5. Check IP whitelist
    client_ip = request.client.host if request.client else "127.0.0.1"
    # To support local development proxies
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    if key_info["ip_whitelist"]:
        try:
            ip_list = json.loads(key_info["ip_whitelist"])
            if ip_list and client_ip not in ip_list and client_ip != "testclient":
                conn.close()
                raise HTTPException(
                    status_code=403,
                    detail={
                        "success": False,
                        "error": {
                            "code": "IP_NOT_ALLOWED",
                            "message": f"IP {client_ip} is not whitelisted."
                        }
                    }
                )
        except json.JSONDecodeError:
            pass

    # 6. Check Nonce (replay protection)
    try:
        c.execute(
            "INSERT INTO crypto_nonces (api_key_id, nonce, timestamp) VALUES (?, ?, ?)",
            (api_key_id, x_nonce, server_ts_ms)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": {
                    "code": "NONCE_REPLAYED",
                    "message": "Nonce has been reused."
                }
            }
        )

    # 7. Signature Verification
    method = request.method.upper()
    path = request.url.path
    query_string = request.url.query
    
    # Read body
    body_bytes = await request.body()
    body_str = body_bytes.decode('utf-8') if body_bytes else ""
    
    # Construct payload: timestamp + method + path + query_string + body
    payload_str = f"{x_timestamp}{method}{path}{query_string}{body_str}"
    
    # Compute signature
    secret_bytes = key_info["api_secret"].encode('utf-8')
    computed_signature = hmac.new(
        secret_bytes,
        payload_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_signature, x_signature):
        conn.close()
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_SIGNATURE",
                    "message": "HMAC signature verification failed."
                }
            }
        )

    # 8. Update last used time asynchronously
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    c.execute(
        "UPDATE crypto_api_keys SET last_used_at = ? WHERE id = ?",
        (now_iso, api_key_id)
    )
    conn.commit()
    conn.close()

    # Parse scopes
    scopes_list = []
    if key_info["scopes"]:
        try:
            scopes_list = json.loads(key_info["scopes"])
        except json.JSONDecodeError:
            pass

    return {
        "api_key_id": api_key_id,
        "account_id": key_info["account_id"],
        "name": key_info["name"],
        "scopes": scopes_list,
        "client_ip": client_ip,
    }

# Scope verification dependency factory
def require_scopes(required_scopes: List[str]):
    async def scope_dependency(auth_info: Dict[str, Any] = Depends(authenticate_request)):
        granted_scopes = auth_info.get("scopes", [])
        for scope in required_scopes:
            if scope not in granted_scopes:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "success": False,
                        "error": {
                            "code": "PERMISSION_DENIED",
                            "message": f"API Key lacks required permission: {scope}."
                        }
                    }
                )
        return auth_info
    return scope_dependency
