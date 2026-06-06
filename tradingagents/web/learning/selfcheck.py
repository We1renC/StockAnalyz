"""Deployment self-check / preflight.

Audit fix (Round P). Across the E→O rounds the system grew many runtime
knobs (SMC_AUTOLEARN_ENABLED, SMC_LEDGER_DIR, DASHBOARD_API_TOKEN,
SMC_MAINTENANCE_INTERVAL, LOG_LEVEL, SMC_LEARNING_DB) and operational
invariants (WAL active, ledger readable, strategy.yaml valid). There was
no single "is this deployment correctly wired?" probe.

run_selfcheck() returns a list of checks, each:
    {"name", "status": "pass"|"warn"|"fail", "detail"}
plus an overall status (worst of the individual ones). Suitable as a
startup gate, a monitoring probe, or a pre-go-live checklist.

Checks are read-only and best-effort — a failing check never raises.
"""

from __future__ import annotations

import os
from typing import Callable


def _check(name: str, fn: Callable[[], tuple]) -> dict:
    try:
        status, detail = fn()
    except Exception as exc:
        status, detail = "fail", f"{type(exc).__name__}: {exc}"
    return {"name": name, "status": status, "detail": detail}


def _db_wal() -> tuple:
    from deps import get_db
    conn = get_db()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    if str(mode).lower() == "wal" and int(bt) >= 1000:
        return "pass", f"journal_mode={mode}, busy_timeout={bt}"
    return "warn", f"journal_mode={mode}, busy_timeout={bt} (E3 expects wal/5000)"


def _ledger_readable() -> tuple:
    from smc_quant import LedgerPaths, read_trade_ledger
    path = LedgerPaths.training_ledger()
    if not os.path.exists(path):
        return "warn", f"no training ledger yet at {path}"
    recs = read_trade_ledger(path)
    return "pass", f"{len(recs)} records readable"


def _strategy_yaml_valid() -> tuple:
    from smc_quant import apply_strategy_yaml_overrides
    out = apply_strategy_yaml_overrides()
    rejected = out.get("rejected") or []
    if rejected:
        return "fail", f"{len(rejected)} rejected weight(s): " + \
            ", ".join(f"{r.get('factor')}={r.get('value')}" for r in rejected[:5])
    return "pass", "all confluence weights valid"


def _autolearn() -> tuple:
    from learning.autolearn_scheduler import is_enabled, configured_symbols
    if is_enabled():
        return "pass", f"server-side learning ON for {configured_symbols()}"
    return "warn", "SMC_AUTOLEARN_ENABLED unset → learning only when UI open"


def _api_token() -> tuple:
    from learning.api_auth import _token
    if _token():
        return "pass", "API token configured"
    return "warn", "no DASHBOARD_API_TOKEN → endpoints unauthenticated"


def _obsidian() -> tuple:
    vault = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not vault:
        # fall back to settings
        try:
            from llm_providers import load_settings
            vault = (load_settings() or {}).get("obsidian_vault_path")
        except Exception:
            vault = None
    if not vault:
        return "warn", "no Obsidian vault → audit notes/digests disabled"
    if os.path.isdir(vault):
        return "pass", f"vault reachable: {vault}"
    return "warn", f"vault path set but not a directory: {vault}"


def _wal_size() -> tuple:
    from deps import portfolio_db_path
    wal = portfolio_db_path() + "-wal"
    if not os.path.exists(wal):
        return "pass", "no -wal sidecar (checkpointed)"
    mb = os.path.getsize(wal) / 1_048_576
    if mb > 64:
        return "warn", f"-wal is {mb:.1f}MB (consider POST /wal-checkpoint)"
    return "pass", f"-wal {mb:.2f}MB"


_CHECKS = [
    ("db_wal", _db_wal),
    ("ledger_readable", _ledger_readable),
    ("strategy_yaml_valid", _strategy_yaml_valid),
    ("autolearn_scheduler", _autolearn),
    ("api_token", _api_token),
    ("obsidian_vault", _obsidian),
    ("wal_size", _wal_size),
]

_RANK = {"pass": 0, "warn": 1, "fail": 2}


def run_selfcheck() -> dict:
    checks = [_check(name, fn) for name, fn in _CHECKS]
    overall = "pass"
    for c in checks:
        if _RANK.get(c["status"], 0) > _RANK.get(overall, 0):
            overall = c["status"]
    return {
        "overall": overall,
        "checks": checks,
        "summary": {
            "pass": sum(1 for c in checks if c["status"] == "pass"),
            "warn": sum(1 for c in checks if c["status"] == "warn"),
            "fail": sum(1 for c in checks if c["status"] == "fail"),
        },
    }
