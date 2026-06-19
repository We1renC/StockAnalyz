import sqlite3
import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

MIGRATIONS = [
    (1, "ALTER TABLE positions ADD COLUMN target_entry REAL", "positions target_entry"),
    (2, "ALTER TABLE positions ADD COLUMN target_profit REAL", "positions target_profit"),
    (3, "ALTER TABLE positions ADD COLUMN target_stop REAL", "positions target_stop"),
    (4, "ALTER TABLE market_state ADD COLUMN sox REAL", "market_state sox"),
    (5, "ALTER TABLE market_state ADD COLUMN sox_ma60 REAL", "market_state sox_ma60"),
    (6, "ALTER TABLE market_state ADD COLUMN ndx REAL", "market_state ndx"),
    (7, "ALTER TABLE market_state ADD COLUMN ndx_ma20 REAL", "market_state ndx_ma20"),
    (8, "ALTER TABLE market_state ADD COLUMN tnx REAL", "market_state tnx"),
    (9, "ALTER TABLE market_state ADD COLUMN dxy REAL", "market_state dxy"),
    (10, "ALTER TABLE market_state ADD COLUMN hsntech REAL", "market_state hsntech"),
    (11, "ALTER TABLE market_state ADD COLUMN twii_ma20 REAL", "market_state twii_ma20"),
    (12, "ALTER TABLE market_state ADD COLUMN twii_ma120 REAL", "market_state twii_ma120"),
    (13, "ALTER TABLE market_state ADD COLUMN spx_ma20 REAL", "market_state spx_ma20"),
    (14, "ALTER TABLE market_state ADD COLUMN spx_ma120 REAL", "market_state spx_ma120"),
    (15, "ALTER TABLE price_cache ADD COLUMN nav REAL", "price_cache nav"),
    (16, "ALTER TABLE price_cache ADD COLUMN pb REAL", "price_cache pb"),
    (17, "ALTER TABLE price_cache ADD COLUMN quote_type TEXT", "price_cache quote_type"),
    (18, "ALTER TABLE trades ADD COLUMN name TEXT DEFAULT ''", "trades name"),
    (19, "ALTER TABLE trades ADD COLUMN fee REAL DEFAULT 0", "trades fee"),
    (20, "ALTER TABLE trades ADD COLUMN tax REAL DEFAULT 0", "trades tax"),
    (21, "ALTER TABLE trades ADD COLUMN settle_date TEXT", "trades settle_date"),
    (22, "ALTER TABLE trades ADD COLUMN notes TEXT DEFAULT ''", "trades notes"),
    (23, "ALTER TABLE smc_backtest_trades ADD COLUMN mae REAL", "smc_backtest_trades mae"),
    (24, "ALTER TABLE smc_backtest_trades ADD COLUMN mfe REAL", "smc_backtest_trades mfe"),
]

def run_migrations(conn: sqlite3.Connection):
    """Run centralized schema migrations with versioning."""
    cursor = conn.cursor()
    # 1. Create schema_migrations table if not exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT
        )
    """)
    conn.commit()

    # 2. Get currently applied migration version
    row = cursor.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    current_version = row[0] if (row and row[0] is not None) else 0

    # 3. Apply missing migrations
    for version, sql, description in MIGRATIONS:
        if version > current_version:
            _logger.info("Applying migration v%d: %s", version, description)
            try:
                cursor.execute(sql)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                    (version, datetime.now(timezone.utc).isoformat(), description)
                )
                conn.commit()
            except sqlite3.OperationalError as e:
                # If the column already exists (e.g. from legacy DB pre-migration system),
                # we mark the migration as applied to align versioning cleanly.
                err_msg = str(e).lower()
                if "duplicate column name" in err_msg or "already exists" in err_msg:
                    _logger.warning("Migration v%d already exists in schema. Marking as applied.", version)
                    cursor.execute(
                        "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                        (version, datetime.now(timezone.utc).isoformat(), description)
                    )
                    conn.commit()
                else:
                    conn.rollback()
                    raise
            except Exception:
                conn.rollback()
                raise
