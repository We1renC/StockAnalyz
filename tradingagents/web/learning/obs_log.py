"""Structured logging + error-swallow accounting.

Audit fix G1 + F2. The codebase had ~176 ``except Exception: pass``
blocks — great for resilience, terrible for observability: "why didn't
it learn?" had no answer because failures vanished silently.

This module provides:

  • get_logger(name) — a logging.Logger with a JSON-ish formatter,
    level from env ``LOG_LEVEL`` (default INFO).
  • log_event(logger, event, **fields) — structured one-line event.
  • swallow(logger, ctx, *, reraise=False) — context manager that
    REPLACES ``try/except Exception: pass``. It logs the exception with
    context and bumps a per-context counter (inspectable via
    swallow_counts()) so the ops endpoint (G2) can surface silent-failure
    rates instead of them being invisible.

Usage (replacing a bare swallow):

    from learning.obs_log import get_logger, swallow
    log = get_logger(__name__)
    with swallow(log, "edge_decay_trail"):
        record_alert_delivery(...)            # still won't crash caller
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import contextmanager


_CONFIGURED = False
_LOCK = threading.RLock()

# Per-context swallowed-error counters for the ops endpoint.
_swallow_counts: dict[str, int] = {}
_swallow_last: dict[str, str] = {}


def _configure_root() -> None:
    global _CONFIGURED
    with _LOCK:
        if _CONFIGURED:
            return
        level = os.environ.get("LOG_LEVEL", "INFO").upper()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        root = logging.getLogger("smc")
        root.handlers[:] = [handler]
        root.setLevel(getattr(logging, level, logging.INFO))
        root.propagate = False
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    # Namespace everything under "smc." so one handler/level governs all.
    short = name.split(".")[-1]
    return logging.getLogger(f"smc.{short}")


def _fmt_fields(fields: dict) -> str:
    if not fields:
        return ""
    parts = []
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    return " " + " ".join(parts)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO,
              **fields) -> None:
    """Emit a structured one-line event: ``event k1=v1 k2=v2``."""
    logger.log(level, f"{event}{_fmt_fields(fields)}")


@contextmanager
def swallow(logger: logging.Logger, ctx: str, *, reraise: bool = False,
            level: int = logging.WARNING):
    """Drop-in replacement for ``except Exception: pass`` that records.

    On exception: logs at ``level`` with the context, increments the
    per-ctx counter, optionally re-raises.
    """
    try:
        yield
    except Exception as exc:                       # noqa: BLE001 (intentional)
        with _LOCK:
            _swallow_counts[ctx] = _swallow_counts.get(ctx, 0) + 1
            _swallow_last[ctx] = f"{type(exc).__name__}: {exc}"
        log_event(logger, "swallowed_error", level=level,
                  ctx=ctx, err=type(exc).__name__)
        if reraise:
            raise


def swallow_counts() -> dict:
    """Snapshot of swallowed-error counters for the ops endpoint (G2)."""
    with _LOCK:
        return {
            "total": sum(_swallow_counts.values()),
            "by_context": dict(_swallow_counts),
            "last_error": dict(_swallow_last),
        }


def reset_swallow_counts() -> None:
    with _LOCK:
        _swallow_counts.clear()
        _swallow_last.clear()
