"""mtime-aware in-process cache for ledger reads.

Audit fix A3. ``/api/smc-crypto/{learning-curve, real-pnl-gates,
hyperparameter-sweep, cluster-ensemble, ...}`` each call
``load_trade_records("tmp/smc_training_ledger.jsonl")`` on every
request. With a 50k-row jsonl that's 3-10MB I/O + parse per hit; the
sweep then runs 64 simulations over the same list. The dashboard
polls these endpoints every few seconds → death by I/O.

This cache:
  • Keys on ``(abs_path, mtime_ns, size_bytes, content_fingerprint)``.
    Most rewrites bump mtime/size; if a rewrite preserves both, the
    fingerprint still invalidates the cached entry.
  • Returns the SAME ``list[dict]`` reference (read-only contract:
    callers must not mutate).
  • TTL safety net of 60s in case mtime granularity loses an update
    on the same nanosecond (rare on macOS APFS but possible).

Thread-safe. Bounded to 8 entries (LRU) so multiple symbol-scoped
ledgers don't unbounded-grow.
"""

from __future__ import annotations

import os
import threading
import time
import hashlib
from collections import OrderedDict
from typing import Optional


class _LedgerCache:
    def __init__(self, max_entries: int = 8, ttl_sec: float = 60.0):
        self._max = int(max_entries)
        self._ttl = float(ttl_sec)
        self._store: "OrderedDict[str, tuple[tuple, float, list]]" = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def _stat_key(self, path: str) -> Optional[tuple]:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return ("missing", 0, 0)
        return ("ok", st.st_mtime_ns, st.st_size, self._fingerprint(path, st.st_size))

    def _fingerprint(self, path: str, size: int, sample_bytes: int = 4096) -> str:
        """Cheap content signature to catch same-size / same-mtime rewrites."""
        if size <= 0:
            return "empty"
        h = hashlib.blake2b(digest_size=8)
        try:
            with open(path, "rb") as fh:
                if size <= sample_bytes * 2:
                    h.update(fh.read())
                else:
                    h.update(fh.read(sample_bytes))
                    fh.seek(max(size - sample_bytes, 0))
                    h.update(fh.read(sample_bytes))
        except OSError:
            return "io_error"
        return h.hexdigest()

    def get(self, path: str, loader) -> list[dict]:
        abs_path = os.path.abspath(path)
        key = self._stat_key(abs_path)
        now = time.time()
        with self._lock:
            cached = self._store.get(abs_path)
            if cached is not None:
                stat_key, cached_at, records = cached
                if stat_key == key and (now - cached_at) <= self._ttl:
                    self.hits += 1
                    self._store.move_to_end(abs_path)
                    return records
        # Miss: load outside the lock to avoid blocking other readers
        records = loader(path)
        with self._lock:
            self._store[abs_path] = (key, now, records)
            self._store.move_to_end(abs_path)
            if len(self._store) > self._max:
                self._store.popitem(last=False)
            self.misses += 1
        return records

    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self.hits, "misses": self.misses,
                "size": len(self._store),
                "hit_rate": (
                    self.hits / (self.hits + self.misses)
                    if (self.hits + self.misses) > 0 else 0.0
                ),
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0


_singleton = _LedgerCache()


def cached_load_trade_records(path: str) -> list[dict]:
    """Drop-in replacement for ``load_trade_records`` with mtime-cache."""
    from smc_quant import load_trade_records
    return _singleton.get(path, load_trade_records)


def cache_stats() -> dict:
    return _singleton.stats()


def cache_clear() -> None:
    _singleton.clear()
