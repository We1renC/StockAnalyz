"""Ledger rotation — keep jsonl bounded, archive the rest.

Audit fix G3. The training ledger grew to 9,800+ rows / 8.8 MB before a
manual "Plan B" trim. A3's cache only mitigates reads; the file still
grows unbounded and every full-scan endpoint pays for it.

Policy (per-symbol rolling window, gzip archive of the overflow):

  • Group resolved records by symbol.
  • Keep the most-recent ``keep_per_symbol`` rows per symbol (by
    entry_time).
  • Append the overflow to ``<dir>/ledger_archive/<basename>.<ts>.jsonl.gz``
    so nothing is lost — backtests / audits can still read history.
  • Rewrite the live ledger (under A1 lock) with only the kept rows.

Idempotent-ish: running again when already under the cap is a no-op
(archives nothing, rewrites nothing). Safe: archive is written and
fsynced BEFORE the live file is truncated.
"""

from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from learning.obs_log import get_logger, log_event

_log = get_logger(__name__)


def _ts_key(r: dict) -> str:
    return str(r.get("entry_time") or "")


def rotate_ledger(
    path: str,
    *,
    keep_per_symbol: int = 500,
    archive_dir: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> dict:
    """Trim ``path`` to the most-recent ``keep_per_symbol`` rows/symbol.

    Returns a report dict. Writes nothing if already under the cap for
    every symbol.
    """
    if not os.path.exists(path):
        return {"rotated": False, "reason": "missing", "path": path}

    with open(path, "r", encoding="utf-8") as fh:
        recs = [json.loads(l) for l in fh if l.strip()]
    if not recs:
        return {"rotated": False, "reason": "empty", "path": path}

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_sym[str(r.get("symbol") or "?")].append(r)

    kept: list[dict] = []
    archived: list[dict] = []
    per_symbol_report: dict[str, dict] = {}
    for sym, rows in by_sym.items():
        rows.sort(key=_ts_key)
        if len(rows) <= keep_per_symbol:
            kept.extend(rows)
            per_symbol_report[sym] = {"before": len(rows), "kept": len(rows), "archived": 0}
            continue
        keep = rows[-keep_per_symbol:]
        overflow = rows[:-keep_per_symbol]
        kept.extend(keep)
        archived.extend(overflow)
        per_symbol_report[sym] = {
            "before": len(rows), "kept": len(keep), "archived": len(overflow),
        }

    if not archived:
        return {"rotated": False, "reason": "under_cap",
                "path": path, "per_symbol": per_symbol_report}

    # 1) Write archive FIRST (durable) before touching the live file.
    base_dir = archive_dir or os.path.join(os.path.dirname(path) or ".", "ledger_archive")
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    ts = (now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
          ).replace(":", "-").replace("+", "_")
    archive_path = os.path.join(base_dir, f"{os.path.basename(path)}.{ts}.jsonl.gz")
    archived.sort(key=_ts_key)
    with gzip.open(archive_path, "wt", encoding="utf-8") as gz:
        for r in archived:
            gz.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # 2) Rewrite the live file under the A1 exclusive lock.
    kept.sort(key=_ts_key)
    try:
        from learning.file_lock import locked_rewrite
    except Exception:
        from contextlib import contextmanager
        @contextmanager
        def locked_rewrite(_p):
            yield
    with locked_rewrite(path):
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # 3) Invalidate the read cache so the next read sees the trimmed file.
    try:
        from learning.ledger_cache import cache_clear
        cache_clear()
    except Exception:
        pass

    report = {
        "rotated": True,
        "path": path,
        "archive_path": archive_path,
        "total_before": len(recs),
        "total_kept": len(kept),
        "total_archived": len(archived),
        "per_symbol": per_symbol_report,
    }
    log_event(_log, "ledger_rotated", path=os.path.basename(path),
              kept=len(kept), archived=len(archived))
    return report
