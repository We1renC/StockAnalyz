"""Learning-DB separation tooling.

Audit fix E2. ``portfolio.db`` holds 54 tables mixing three concerns:

  • PERSONAL    positions / watchlist / trades / portfolio_snapshots
  • EXCHANGE    crypto_api_keys / crypto_orders / crypto_fills / ...
  • LEARNING    smc_* / paper_acceptance_* / training history / adaptive

Mixing the (sensitive, never-committed) personal book with the learning
data means: a single-file lock bottleneck, and "reset the learning data"
risks touching real holdings.

This module does NOT auto-cut-over (a live DB split is a major op — see
CLAUDE.md "重大操作執行前要確認"). It provides:

  • categorize_tables(conn)  → {personal, exchange, learning, other}
  • split_learning_db(src, dst, dry_run=True) → copy LEARNING tables to a
    new DB, returning a per-table row-count report. dry_run=True only
    reports, writes nothing.
  • verify_split(src, dst)   → assert row counts match post-copy.

Cut-over remains an explicit operator action; once the operator trusts
the copy, app wiring can point learning reads/writes at the new file via
``SMC_LEARNING_DB``.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


# Prefix-based classification. Order matters: exchange before learning so
# crypto_* doesn't get mis-bucketed if a learning prefix overlaps.
_PERSONAL_PREFIXES = ("positions", "watchlist", "trades", "portfolio_")
_PERSONAL_EXACT = {"trades"}
_EXCHANGE_PREFIXES = ("crypto_",)
_LEARNING_PREFIXES = (
    "smc_", "paper_acceptance_", "adaptive", "alerts",
)
_LEARNING_EXACT = {
    "smc_training_history", "smc_baseline_equity", "smc_adaptive_trade_ledger",
    "smc_adaptive_audit_logs", "smc_adaptive_config_patches",
    "smc_adaptive_kill_switch", "smc_backtest_runs", "smc_backtest_trades",
    "smc_trade_journal", "crypto_kill_switch",
}


def _all_tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def categorize_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Bucket every table into personal / exchange / learning / other."""
    out: dict[str, list[str]] = {
        "personal": [], "exchange": [], "learning": [], "other": [],
    }
    for t in _all_tables(conn):
        if t in _LEARNING_EXACT or t.startswith(_LEARNING_PREFIXES):
            # crypto_kill_switch is learning despite crypto_ prefix
            if t.startswith("crypto_") and t not in _LEARNING_EXACT:
                out["exchange"].append(t)
            else:
                out["learning"].append(t)
        elif t.startswith(_EXCHANGE_PREFIXES):
            out["exchange"].append(t)
        elif t in _PERSONAL_EXACT or t.startswith(_PERSONAL_PREFIXES):
            out["personal"].append(t)
        else:
            out["other"].append(t)
    return out


def _table_sql(conn: sqlite3.Connection, table: str) -> Optional[str]:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else None


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0])
    except sqlite3.Error:
        return -1


def split_learning_db(
    src_path: str,
    dst_path: str,
    *,
    dry_run: bool = True,
) -> dict:
    """Copy LEARNING-category tables from ``src_path`` into ``dst_path``.

    dry_run=True (default): report only, no writes.
    dry_run=False: create dst, copy schema + rows for each learning table.

    Returns ``{"tables": [...], "copied": {table: rows}, "dry_run": bool}``.
    The source is never modified (copy, not move) — operator drops the
    old tables manually only after verifying.
    """
    src = sqlite3.connect(src_path)
    try:
        cats = categorize_tables(src)
        learning = cats["learning"]
        report = {
            "tables": learning,
            "copied": {},
            "dry_run": dry_run,
            "categories": {k: len(v) for k, v in cats.items()},
        }
        if dry_run:
            for t in learning:
                report["copied"][t] = _row_count(src, t)
            return report

        dst = sqlite3.connect(dst_path)
        try:
            dst.execute("PRAGMA journal_mode=WAL")
            for t in learning:
                ddl = _table_sql(src, t)
                if not ddl:
                    continue
                dst.execute(f"DROP TABLE IF EXISTS '{t}'")
                dst.execute(ddl)
                rows = src.execute(f"SELECT * FROM '{t}'").fetchall()
                if rows:
                    placeholders = ",".join("?" * len(rows[0]))
                    dst.executemany(
                        f"INSERT INTO '{t}' VALUES ({placeholders})", rows
                    )
                report["copied"][t] = len(rows)
            dst.commit()
        finally:
            dst.close()
        return report
    finally:
        src.close()


def verify_split(src_path: str, dst_path: str) -> dict:
    """Confirm every learning table's row count matches between src and dst."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        learning = categorize_tables(src)["learning"]
        mismatches = {}
        for t in learning:
            s = _row_count(src, t)
            try:
                d = _row_count(dst, t)
            except sqlite3.Error:
                d = -1
            if s != d:
                mismatches[t] = {"src": s, "dst": d}
        return {
            "ok": not mismatches,
            "checked": len(learning),
            "mismatches": mismatches,
        }
    finally:
        src.close()
        dst.close()


def _cli() -> None:
    """Operator entry point.

    Dry-run (default, safe):
        python -m learning.db_split portfolio.db learning.db
    Execute the copy:
        python -m learning.db_split portfolio.db learning.db --execute
    Then verify, and only after that set SMC_LEARNING_DB=learning.db and
    drop the old learning tables from portfolio.db manually.
    """
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    execute = "--execute" in sys.argv
    if len(args) < 2:
        print("usage: python -m learning.db_split <src.db> <dst.db> [--execute]")
        raise SystemExit(2)
    src, dst = args[0], args[1]
    rep = split_learning_db(src, dst, dry_run=not execute)
    print(f"mode: {'EXECUTE' if execute else 'DRY-RUN'}")
    print(f"categories: {rep['categories']}")
    print(f"learning tables ({len(rep['tables'])}):")
    for t, n in sorted(rep["copied"].items(), key=lambda x: -x[1]):
        print(f"  {t:45s} {n:8d} rows")
    if execute:
        v = verify_split(src, dst)
        print(f"\nverify: ok={v['ok']} checked={v['checked']} "
              f"mismatches={v['mismatches']}")
        if v["ok"]:
            print("\n✓ copy verified. Next steps (manual, after backup):")
            print("  1) export SMC_LEARNING_DB=" + dst)
            print("  2) restart the service")
            print("  3) confirm dashboards read correctly")
            print("  4) drop the old learning tables from " + src)


if __name__ == "__main__":
    _cli()
