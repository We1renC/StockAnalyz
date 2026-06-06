"""Cross-process file lock for jsonl ledger append/read.

Audit fix A1. Both ``smc_paper_runner`` (append) and
``smc_missed_signals_reconciler`` (rewrite-in-place) plus N learning
endpoints (read) can hit the same jsonl simultaneously. Append from
two processes is normally line-atomic on POSIX, but ANY rewrite (which
opens, reads, then truncates+writes) racing with append produces
truncated lines that crash ``load_trade_records``.

Provides:
  • ``locked_append(path)`` — context manager holding LOCK_EX while
    writing one line
  • ``locked_read(path)`` — context manager holding LOCK_SH while
    reading
  • ``locked_rewrite(path)`` — LOCK_EX for read-modify-write cycles

Falls back to a no-op lock on Windows (no fcntl). Safe to use even
when the file doesn't exist yet (the lockfile is created next to it).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:                                  # pragma: no cover
    _HAS_FCNTL = False


def _lockfile_path(path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p.parent / f".{p.name}.lock")


@contextmanager
def _acquire(path: str, exclusive: bool):
    lock_path = _lockfile_path(path)
    fh = open(lock_path, "a+")
    try:
        if _HAS_FCNTL:
            fcntl.flock(fh.fileno(),
                         fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            if _HAS_FCNTL:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


@contextmanager
def locked_append(path: str):
    """Hold exclusive lock for an atomic append."""
    with _acquire(path, exclusive=True):
        yield


@contextmanager
def locked_read(path: str):
    """Hold shared lock for read. Multiple readers OK; blocks writers."""
    with _acquire(path, exclusive=False):
        yield


@contextmanager
def locked_rewrite(path: str):
    """Hold exclusive lock for read-then-truncate-then-write cycles
    (e.g. reconciler filling outcome fields in place)."""
    with _acquire(path, exclusive=True):
        yield
