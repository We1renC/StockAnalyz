"""Minimal API token middleware for the FastAPI dashboard.

Audit fix A2. The 25 ``/api/smc-crypto/*`` endpoints (and others) ran
without any auth — anyone on the host could trigger a sweep / write
calibration / mutate strategy.yaml. 127.0.0.1-binding is not enough
once VS Code remote / Docker / ssh forwarding gets involved.

Policy (intentionally minimal to avoid scope creep):
  • If env var ``DASHBOARD_API_TOKEN`` is unset → middleware no-op
    (preserves dev ergonomics; opt-in for prod).
  • If set → every request to a path matching ``PROTECTED_PREFIXES``
    must carry header ``X-API-Token: <token>``.
  • Token is compared with ``hmac.compare_digest`` (constant-time).
  • GET on these prefixes is also protected — the sweep / cluster /
    real-pnl-gates endpoints are GET but read sensitive ledger.

Health / static / docs paths stay open so reverse-proxy probes work.
"""

from __future__ import annotations

import hmac
import os
from typing import Iterable


PROTECTED_PREFIXES: tuple[str, ...] = (
    "/api/smc-crypto/",
    "/api/smc-crypto-paper/",
    "/api/learning/",
    "/api/strategy/",
)

OPEN_PATHS: frozenset[str] = frozenset({
    "/", "/docs", "/redoc", "/openapi.json", "/health", "/favicon.ico",
})


def _token() -> str:
    return os.environ.get("DASHBOARD_API_TOKEN", "").strip()


def _is_protected(path: str, prefixes: Iterable[str] = PROTECTED_PREFIXES) -> bool:
    return any(path.startswith(p) for p in prefixes)


async def api_token_middleware(request, call_next):
    """ASGI middleware enforcing X-API-Token on protected paths."""
    expected = _token()
    if not expected:
        return await call_next(request)            # opt-in: unset → off
    path = request.url.path
    if path in OPEN_PATHS or not _is_protected(path):
        return await call_next(request)
    provided = (request.headers.get("x-api-token") or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"detail": "missing_or_invalid_api_token"}, status_code=401,
        )
    return await call_next(request)
