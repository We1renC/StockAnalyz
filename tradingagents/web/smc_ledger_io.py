"""Trade-ledger I/O — extracted from smc_quant (audit fix F3).

Self-contained read/write layer for the jsonl/parquet trade ledgers:
LedgerPaths, connect_db (WAL), schema versioning, dedup persist, cached
read. No back-reference into smc_quant, so no circular import. smc_quant
re-exports these names for backward compatibility — every existing
``from smc_quant import load_trade_records`` keeps working.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from pathlib import Path


# Audit fix A4: schema version stamped on every trade record so future
# field renames can route by version instead of silently breaking.
TRADE_LEDGER_SCHEMA_VERSION = 2
TRADE_RECORD_SCHEMA_VERSION = TRADE_LEDGER_SCHEMA_VERSION


def _trade_dedup_key(rec: dict) -> str:
    """Deterministic uniqueness key for a trade_record.

    Audit fix P0-1: prevents the same backtest entry being counted as a
    "new" sample on every training tick. We key on the immutable identity
    triple (symbol, model, entry_time_or_entry_index, entry_price) so two
    runs over the same OHLCV produce one row, not many.
    """
    tid = rec.get("trade_id")
    if tid:
        return str(tid)
    return "|".join([
        str(rec.get("symbol") or ""),
        str(rec.get("model") or ""),
        str(rec.get("entry_time") or rec.get("entry_index") or ""),
        str(rec.get("entry_price") or rec.get("entry") or ""),
    ])


# Audit fix C2: hardcoded paths were repeated 17× across app.py / runner /
# training_loop / tests. Centralize so a single edit retargets the whole
# system (e.g. to switch the prod ledger to /var/lib/smc/ or to parquet).
class LedgerPaths:
    """Single source of truth for ledger jsonl paths.

    Override via env var ``SMC_LEDGER_DIR``. Without override, resolve to the
    module-local ``web/tmp`` so CLI calls from the repo root and the running
    web app both hit the same ledger directory.
    """

    @staticmethod
    def _dir() -> str:
        import os
        env_dir = os.environ.get("SMC_LEDGER_DIR")
        if env_dir:
            return env_dir
        return str((Path(__file__).resolve().parent / "tmp").resolve())

    @classmethod
    def training_ledger(cls) -> str:
        import os
        return os.path.join(cls._dir(), "smc_training_ledger.jsonl")

    @classmethod
    def paper_journal(cls) -> str:
        import os
        return os.path.join(cls._dir(), "smc_paper_journal.jsonl")

    @classmethod
    def paper_trades(cls) -> str:
        import os
        return os.path.join(cls._dir(), "smc_paper_journal_trades.jsonl")

    @classmethod
    def missed_signals(cls) -> str:
        import os
        return os.path.join(cls._dir(), "smc_missed_signals.jsonl")


def connect_db(db_path: str, *, row_factory: bool = False):
    """Audit fix E3: shared SQLite connect with WAL + busy_timeout.

    All writer paths (training_loop / auto_workflow / orchestrator) used
    to call ``sqlite3.connect(db_path)`` with default journal_mode=delete
    → random "database is locked" under the concurrent async loops + UI
    ticks. Route them through here so every connection gets WAL +
    busy_timeout + NORMAL sync.
    """
    import sqlite3
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
    if row_factory:
        conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def _stamp_schema_version(record: dict) -> dict:
    """Upgrade persisted records to the current schema version."""
    try:
        current = int(record.get("schema_version") or 0)
    except (TypeError, ValueError):
        current = 0
    if current < TRADE_LEDGER_SCHEMA_VERSION:
        record["schema_version"] = TRADE_LEDGER_SCHEMA_VERSION
    return record


def _normalize_record_by_version(record: dict) -> dict:
    """Map older-version records to current schema.

    v1 (pre-A4): no ``schema_version`` field. Treated as v1.
    v2: ``schema_version`` stamped at write time; ``source`` / ``interval``
         / ``regime`` may be present from B-layer fixes.

    Currently a passthrough since v1 → v2 is field-additive only. Future
    breaking changes route here.
    """
    v = int(record.get("schema_version") or 1)
    if v == 1:
        # v1 → v2: add the new fields as ``None`` (legacy marker)
        record.setdefault("source", "legacy")
        record.setdefault("interval", None)
        record.setdefault("regime", None)
        record["schema_version"] = 2
    return record


# Audit fix D5: write-time numeric rail. Mirrors the D2 cap in
# evaluate_entry_models so a record from ANY producer (live runner,
# seeder, future importers) can never land an absurd R in the ledger.
LEDGER_R_MULTIPLE_CAP = 20.0


def _validate_record_for_persist(rec: dict, *, r_cap: float = LEDGER_R_MULTIPLE_CAP) -> dict:
    """Clamp / annotate a record before it hits disk.

    |r_multiple| winsorized to ±r_cap; original kept as ``r_raw`` with
    ``r_clipped: true`` stamped so audits can see it happened.
    """
    rm = rec.get("r_multiple")
    if rm is not None:
        try:
            val = float(rm)
            if abs(val) > float(r_cap):
                rec["r_raw"] = val
                rec["r_clipped"] = True
                rec["r_multiple"] = max(-float(r_cap), min(float(r_cap), val))
        except (TypeError, ValueError):
            pass
    return rec


def persist_trade_records(records: list[dict], path: str, *, dedup: bool = True) -> int:
    """Append-write trade records as JSONL (one row per line).

    ``dedup=True`` (default) skips records whose key (per ``_trade_dedup_key``)
    already exists on disk — this is the P0-1 audit fix. Pass dedup=False to
    preserve the historical "append everything" behaviour.

    Parquet is the §18.2 preferred format but we keep the default to
    JSONL to avoid a hard dependency on pyarrow.
    """
    import json, os
    if not records:
        return 0
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    # Audit fix A1: serialize dedup-read + append under a single
    # exclusive lock so concurrent runners can't race the dedup window.
    try:
        from learning.file_lock import locked_append as _locked_append
    except Exception:
        from contextlib import contextmanager
        @contextmanager
        def _locked_append(_p):  # type: ignore[no-redef]
            yield
    # Audit fix A4 + D5: stamp schema version + numeric rail before write.
    records = [_validate_record_for_persist(_stamp_schema_version(dict(r)))
                for r in records]
    with _locked_append(path):
        if dedup:
            existing_keys: set[str] = set()
            if path.endswith(".parquet"):
                try:
                    import pyarrow  # noqa: F401
                    if os.path.exists(path):
                        for row in pd.read_parquet(path).to_dict(orient="records"):
                            existing_keys.add(_trade_dedup_key(row))
                except Exception:
                    pass
            else:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                existing_keys.add(_trade_dedup_key(json.loads(line)))
                            except Exception:
                                continue
            records = [r for r in records if _trade_dedup_key(r) not in existing_keys]
            if not records:
                return 0

        if path.endswith(".parquet"):
            try:
                import pyarrow  # noqa: F401
                existing = []
                if os.path.exists(path):
                    existing = pd.read_parquet(path).to_dict(orient="records")
                pd.DataFrame(existing + list(records)).to_parquet(path, index=False)
                return len(records)
            except Exception:
                path = path[:-len(".parquet")] + ".jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        return len(records)


def load_trade_records(path: str) -> list[dict]:
    """Read a JSONL or parquet trade ledger back into a list of dicts."""
    import json, os
    if not os.path.exists(path):
        return []
    if path.endswith(".parquet"):
        try:
            return pd.read_parquet(path).to_dict(orient="records")
        except Exception:
            return []
    records: list[dict] = []
    try:
        from learning.file_lock import locked_read
    except Exception:
        from contextlib import contextmanager
        @contextmanager
        def locked_read(_path):  # type: ignore[no-redef]
            yield
    with locked_read(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    # Audit fix A4: route through version normalizer so v1
                    # records get backfilled fields and a v2 stamp.
                    records.append(_normalize_record_by_version(json.loads(line)))
                except Exception:
                    continue
    return records


def load_cached_trade_records(path: str) -> list[dict]:
    """Shared cached ledger read for read-heavy workflows.

    This keeps cache ownership inside ``smc_quant`` so callers do not need
    to know about ``learning.ledger_cache`` directly. If the cache layer is
    unavailable, we safely fall back to a fresh read.
    """
    try:
        from learning.ledger_cache import cached_load_trade_records
    except Exception:
        return load_trade_records(path)
    return cached_load_trade_records(path)


def read_trade_ledger(
    path: str,
    *,
    symbol: Optional[str] = None,
    use_cache: bool = True,
    copy_records: bool = False,
) -> list[dict]:
    """Unified trade-ledger read policy.

    Args:
        path: Ledger path.
        symbol: Optional symbol filter.
        use_cache: Use the shared in-process cache for read-heavy paths.
        copy_records: Return a caller-owned list when subsequent mutation
            (extend/sort/pop/annotation) is expected.

    Returns:
        A list of normalized ledger records, optionally symbol-filtered.
    """
    records = load_cached_trade_records(path) if use_cache else load_trade_records(path)
    if symbol:
        # Filtering already produces a caller-owned list.
        return [r for r in records if r.get("symbol") == symbol]
    if copy_records:
        return list(records)
    return records
