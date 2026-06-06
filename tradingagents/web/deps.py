"""Shared FastAPI dependencies — extracted from app.py (audit fix F1-cont).

The app.py monolith owned ``get_db`` / ``_portfolio_db_path`` /
``_crypto_api_client``, which every router needed → forced endpoints to
live in app.py or risk a circular import (``_crypto_api_client`` wraps the
``app`` object itself).

This module breaks the cycle:
  • get_db() / portfolio_db_path() are app-independent (just a path +
    WAL-enabled connection), so routers import them directly.
  • make_crypto_api_client(app) takes the app explicitly; router
    endpoints obtain it via FastAPI's ``request.app`` instead of a
    module-global — the idiomatic loopback pattern.

app.py re-exports get_db / _portfolio_db_path so its ~130 existing
call-sites keep working unchanged.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def portfolio_db_path() -> str:
    """Absolute path to portfolio.db (next to this package)."""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "portfolio.db")


# Backward-compatible alias (app.py used the underscore name).
_portfolio_db_path = portfolio_db_path


def _apply_db_pragmas(conn: sqlite3.Connection) -> None:
    # Audit fix E3: WAL + busy_timeout on every connection.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """WAL-enabled connection to portfolio.db (or an explicit path).

    When no path is given, resolve ``app.DB`` lazily at CALL time (not
    import time, which would create a cycle). This preserves the test
    mechanism ``patch("app.DB", tmp)`` — get_db sees the patched value.
    """
    path = db_path
    if path is None:
        try:
            import app as _app
            path = str(_app.DB)
        except Exception:
            path = str(Path(__file__).parent / "portfolio.db")
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    _apply_db_pragmas(conn)
    return conn


def make_crypto_api_client(app):
    """Build an in-process CryptoApiClient wrapping the given FastAPI app.

    Routers call this with ``request.app`` so they don't need a reference
    to the module-global ``app`` (which is what created the import cycle).
    """
    from fastapi.testclient import TestClient
    from smc_paper_runner import CryptoApiClient
    return CryptoApiClient(TestClient(app))
