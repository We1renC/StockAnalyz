"""Operator alerting — phone/desktop notification when the system needs a human.

Phase-1 gap (全自動量化交易 roadmap): selfcheck failures, kill-switch
trips, repeated tick errors and dead processes were only visible in a
log nobody watches. For unattended 24/7 operation, failures must reach
the operator.

Channels (tried in order, first success wins; all best-effort):
  1. telegram  — if settings.json has ``telegram_bot_token`` +
     ``telegram_chat_id``. Reaches your phone anywhere.
  2. macos     — ``osascript display notification``; zero-config local
     desktop banner. Works out of the box on this Mac.
  3. log       — structured log line (always emitted regardless).

Anti-spam: per-title cooldown (default 30 min) so a flapping check
doesn't fire hundreds of notifications.

Usage:
    from learning.alerting import send_alert
    send_alert("selfcheck failed", "ledger_integrity: dup_rate=40%",
               severity="critical")
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

from learning.obs_log import get_logger, log_event

_log = get_logger(__name__)
_LOCK = threading.RLock()

# title → last-sent monotonic timestamp
_last_sent: dict[str, float] = {}
DEFAULT_COOLDOWN_S = 1800.0


def _telegram_config() -> Optional[tuple]:
    try:
        from llm_providers import load_settings
        s = load_settings() or {}
        token = str(s.get("telegram_bot_token") or "").strip()
        chat = str(s.get("telegram_chat_id") or "").strip()
        if token and chat:
            return token, chat
    except Exception:
        pass
    return None


def _send_telegram(title: str, message: str) -> bool:
    cfg = _telegram_config()
    if not cfg:
        return False
    token, chat = cfg
    try:
        body = urllib.parse.urlencode({
            "chat_id": chat,
            "text": f"[SMC] {title}\n{message}",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status == 200
    except Exception as exc:
        log_event(_log, "telegram_send_failed", err=type(exc).__name__)
        return False


def _send_macos(title: str, message: str) -> bool:
    try:
        # osascript is macOS-native; escape double quotes.
        t = title.replace('"', "'")
        m = message.replace('"', "'")[:200]
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{m}" with title "SMC: {t}" sound name "Basso"'],
            timeout=5, capture_output=True,
        )
        return True
    except Exception as exc:
        log_event(_log, "macos_notify_failed", err=type(exc).__name__)
        return False


def send_alert(
    title: str,
    message: str,
    *,
    severity: str = "warning",
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    now_fn=time.monotonic,
) -> dict:
    """Deliver an operator alert. Returns {sent, channel, suppressed}."""
    # Always leave a structured log trail.
    log_event(_log, "operator_alert", severity=severity, title=title)

    with _LOCK:
        last = _last_sent.get(title)
        now = now_fn()
        if last is not None and (now - last) < float(cooldown_s):
            return {"sent": False, "channel": None, "suppressed": True}
        _last_sent[title] = now

    if _send_telegram(title, message):
        return {"sent": True, "channel": "telegram", "suppressed": False}
    if _send_macos(title, message):
        return {"sent": True, "channel": "macos", "suppressed": False}
    return {"sent": False, "channel": "log_only", "suppressed": False}


def reset_cooldowns() -> None:
    with _LOCK:
        _last_sent.clear()
