"""Adaptive calibration storage, audit, and config patch helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4


ADAPTIVE_LEDGER_SCHEMA_VERSION = 1
ADAPTIVE_MODEL_VERSION = "smc_adaptive_v1"
FEATURE_COLUMNS = (
    "bos_score",
    "choch_score",
    "order_block_score",
    "fvg_score",
    "liquidity_sweep_score",
    "premium_discount_score",
    "htf_bias_score",
    "market_structure_score",
    "volume_imbalance_score",
    "session_score",
    "volatility_regime_score",
    "risk_reward_score",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any, fallback: Any) -> Any:
    if not value:
        return copy.deepcopy(fallback)
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    try:
        return json.loads(value)
    except Exception:
        return copy.deepcopy(fallback)


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if value in (None, "", "-", "--"):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> Optional[str]:
    if value in (None, "", "-", "--"):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def _bool_int(value: Any) -> int:
    if value in (True, 1, 1.0, "1", "true", "TRUE", "yes", "YES"):
        return 1
    return 0


def _resolve_strategy_yaml_path(strategy_yaml_path: str = "config/strategy.yaml") -> Path:
    path = Path(strategy_yaml_path)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / strategy_yaml_path


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def compute_config_hash(config_text: str) -> str:
    return hashlib.sha256((config_text or "").encode("utf-8")).hexdigest()


def strategy_config_snapshot(strategy_yaml_path: str = "config/strategy.yaml") -> dict:
    path = _resolve_strategy_yaml_path(strategy_yaml_path)
    text = _read_text(path)
    payload: dict[str, Any] = {}
    if text:
        try:
            import yaml

            payload = yaml.safe_load(text) or {}
        except Exception:
            payload = {}
    return {
        "path": str(path),
        "exists": path.exists(),
        "text": text,
        "data": payload if isinstance(payload, dict) else {},
        "hash": compute_config_hash(text),
    }


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _feature_scores_from_record(record: dict) -> dict[str, float]:
    explicit = _json_loads(
        record.get("smc_feature_scores") or record.get("feature_scores"),
        {},
    )
    if explicit:
        return {name: _safe_float(explicit.get(name), 0.0) or 0.0 for name in FEATURE_COLUMNS}

    factors = _json_loads(record.get("factors"), {})
    regime = _json_loads(record.get("regime"), {})
    direction = record.get("direction")
    rr_planned = _safe_float(record.get("rr_planned"), 0.0) or 0.0
    market_structure = 0.0
    if direction in (1, "1", "long", "LONG"):
        market_structure = 1.0
    elif direction in (-1, "-1", "short", "SHORT"):
        market_structure = -1.0

    return {
        "bos_score": _safe_float(factors.get("bos_score"), 0.0) or 0.0,
        "choch_score": 1.0 if factors.get("ltf_choch") else 0.0,
        "order_block_score": 1.0 if factors.get("unmitigated_ob") else 0.0,
        "fvg_score": 1.0 if factors.get("unfilled_fvg") else 0.0,
        "liquidity_sweep_score": 1.0 if factors.get("liquidity_swept") else 0.0,
        "premium_discount_score": 1.0 if factors.get("premium_discount_side") else 0.0,
        "htf_bias_score": 1.0 if factors.get("htf_bias_aligned") else 0.0,
        "market_structure_score": market_structure,
        "volume_imbalance_score": 1.0 if factors.get("volume_displacement") else 0.0,
        "session_score": 1.0 if factors.get("killzone") else 0.0,
        "volatility_regime_score": _safe_float(regime.get("volatility_score"), 0.0) or 0.0,
        "risk_reward_score": rr_planned,
    }


def normalize_trade_record(
    record: dict,
    *,
    config_hash: Optional[str] = None,
    model_version: str = ADAPTIVE_MODEL_VERSION,
    source: str = "backtest",
) -> dict:
    direction = record.get("side") or record.get("direction")
    if isinstance(direction, (int, float)):
        side = "long" if int(direction) >= 0 else "short"
    else:
        side = str(direction or "").strip().lower()
        side = "long" if side in {"1", "buy", "long"} else "short" if side in {"-1", "sell", "short"} else side

    pnl_r = _safe_float(record.get("pnl_R"), None)
    if pnl_r is None:
        pnl_r = _safe_float(record.get("r_multiple"), 0.0) or 0.0

    pnl_usdt = _safe_float(record.get("pnl_usdt"), None)
    if pnl_usdt is None:
        pnl_usdt = _safe_float(record.get("pnl"), 0.0) or 0.0

    label = record.get("label")
    if label is None:
        label = 1 if pnl_r > 0 else 0

    normalized = {
        "trade_id": str(record.get("trade_id") or uuid4()),
        "symbol": str(record.get("symbol") or "").upper(),
        "side": side or "long",
        "entry_time": _parse_ts(record.get("entry_time")),
        "exit_time": _parse_ts(record.get("exit_time")),
        "entry_price": _safe_float(record.get("entry_price"), 0.0) or 0.0,
        "exit_price": _safe_float(record.get("exit_price"), None),
        "stop_price": _safe_float(record.get("stop_price"), None),
        "target_price": _safe_float(record.get("target_price"), None),
        "pnl_usdt": pnl_usdt,
        "pnl_R": pnl_r,
        "label": _safe_int(label, 0),
        "confluence_score": _safe_float(record.get("confluence_score"), 0.0) or 0.0,
        "probe": _bool_int(record.get("probe")),
        "model_version": str(record.get("model_version") or model_version),
        "config_hash": str(record.get("config_hash") or config_hash or ""),
        "source": str(record.get("source") or source),
        "state_hint": str(record.get("state_hint") or ""),
        "timeframe": str(record.get("timeframe") or ""),
        "market": str(record.get("market") or ""),
        "metadata_json": _json_dumps(
            {
                "schema_version": record.get("schema_version"),
                "model": record.get("model"),
                "factors": _json_loads(record.get("factors"), {}),
                "crypto_factors": _json_loads(record.get("crypto_factors"), {}),
                "regime": _json_loads(record.get("regime"), {}),
                "dol_kind": record.get("dol_kind"),
                "dol_distance": record.get("dol_distance"),
                "bars_held": record.get("bars_held"),
                "mae": record.get("mae"),
                "mfe": record.get("mfe"),
            }
        ),
    }
    normalized.update(_feature_scores_from_record(record))
    return normalized


def ensure_adaptive_calibration_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS smc_adaptive_trade_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_time TEXT,
            exit_time TEXT,
            entry_price REAL,
            exit_price REAL,
            stop_price REAL,
            target_price REAL,
            pnl_usdt REAL,
            pnl_r REAL,
            label INTEGER NOT NULL DEFAULT 0,
            confluence_score REAL,
            probe INTEGER NOT NULL DEFAULT 0,
            model_version TEXT NOT NULL DEFAULT '',
            config_hash TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'backtest',
            state_hint TEXT NOT NULL DEFAULT '',
            timeframe TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT '',
            bos_score REAL NOT NULL DEFAULT 0,
            choch_score REAL NOT NULL DEFAULT 0,
            order_block_score REAL NOT NULL DEFAULT 0,
            fvg_score REAL NOT NULL DEFAULT 0,
            liquidity_sweep_score REAL NOT NULL DEFAULT 0,
            premium_discount_score REAL NOT NULL DEFAULT 0,
            htf_bias_score REAL NOT NULL DEFAULT 0,
            market_structure_score REAL NOT NULL DEFAULT 0,
            volume_imbalance_score REAL NOT NULL DEFAULT 0,
            session_score REAL NOT NULL DEFAULT 0,
            volatility_regime_score REAL NOT NULL DEFAULT 0,
            risk_reward_score REAL NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_smc_adaptive_ledger_symbol_entry
           ON smc_adaptive_trade_ledger(symbol, entry_time DESC, id DESC)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_smc_adaptive_ledger_probe
           ON smc_adaptive_trade_ledger(symbol, probe, entry_time DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS smc_adaptive_config_patches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patch_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL DEFAULT 'ALL',
            patch_type TEXT NOT NULL DEFAULT 'strategy',
            reason TEXT NOT NULL DEFAULT '',
            patch_payload TEXT NOT NULL DEFAULT '{}',
            before_hash TEXT NOT NULL DEFAULT '',
            after_hash TEXT NOT NULL DEFAULT '',
            before_config TEXT NOT NULL DEFAULT '',
            after_config TEXT NOT NULL DEFAULT '',
            applied INTEGER NOT NULL DEFAULT 0,
            rolled_back INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            applied_at TEXT,
            rolled_back_at TEXT
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_smc_adaptive_patches_symbol_created
           ON smc_adaptive_config_patches(symbol, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS smc_adaptive_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL DEFAULT 'ALL',
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            state_before TEXT NOT NULL DEFAULT '{}',
            state_after TEXT NOT NULL DEFAULT '{}',
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_smc_adaptive_audit_symbol_created
           ON smc_adaptive_audit_logs(symbol, created_at DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS smc_adaptive_kill_switch (
            scope TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'DRY_RUN',
            reason TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}',
            triggered_at TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO smc_adaptive_kill_switch
           (scope, state, reason, detail, triggered_at, updated_at)
           VALUES ('global', 'DRY_RUN', '', '{}', NULL, ?)""",
        (_now_iso(),),
    )


def upsert_trade_ledger_records(
    conn: sqlite3.Connection,
    records: Iterable[dict],
    *,
    config_hash: Optional[str] = None,
    model_version: str = ADAPTIVE_MODEL_VERSION,
    source: str = "backtest",
) -> int:
    ensure_adaptive_calibration_schema(conn)
    now = _now_iso()
    rows = [
        normalize_trade_record(
            record,
            config_hash=config_hash,
            model_version=model_version,
            source=source,
        )
        for record in records or []
        if record
    ]
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO smc_adaptive_trade_ledger (
            trade_id, symbol, side, entry_time, exit_time, entry_price, exit_price,
            stop_price, target_price, pnl_usdt, pnl_r, label, confluence_score,
            probe, model_version, config_hash, source, state_hint, timeframe, market,
            bos_score, choch_score, order_block_score, fvg_score, liquidity_sweep_score,
            premium_discount_score, htf_bias_score, market_structure_score,
            volume_imbalance_score, session_score, volatility_regime_score,
            risk_reward_score, metadata_json, created_at, updated_at
        ) VALUES (
            :trade_id, :symbol, :side, :entry_time, :exit_time, :entry_price, :exit_price,
            :stop_price, :target_price, :pnl_usdt, :pnl_R, :label, :confluence_score,
            :probe, :model_version, :config_hash, :source, :state_hint, :timeframe, :market,
            :bos_score, :choch_score, :order_block_score, :fvg_score, :liquidity_sweep_score,
            :premium_discount_score, :htf_bias_score, :market_structure_score,
            :volume_imbalance_score, :session_score, :volatility_regime_score,
            :risk_reward_score, :metadata_json, :created_at, :updated_at
        )
        ON CONFLICT(trade_id) DO UPDATE SET
            symbol=excluded.symbol,
            side=excluded.side,
            entry_time=excluded.entry_time,
            exit_time=excluded.exit_time,
            entry_price=excluded.entry_price,
            exit_price=excluded.exit_price,
            stop_price=excluded.stop_price,
            target_price=excluded.target_price,
            pnl_usdt=excluded.pnl_usdt,
            pnl_r=excluded.pnl_r,
            label=excluded.label,
            confluence_score=excluded.confluence_score,
            probe=excluded.probe,
            model_version=excluded.model_version,
            config_hash=excluded.config_hash,
            source=excluded.source,
            state_hint=excluded.state_hint,
            timeframe=excluded.timeframe,
            market=excluded.market,
            bos_score=excluded.bos_score,
            choch_score=excluded.choch_score,
            order_block_score=excluded.order_block_score,
            fvg_score=excluded.fvg_score,
            liquidity_sweep_score=excluded.liquidity_sweep_score,
            premium_discount_score=excluded.premium_discount_score,
            htf_bias_score=excluded.htf_bias_score,
            market_structure_score=excluded.market_structure_score,
            volume_imbalance_score=excluded.volume_imbalance_score,
            session_score=excluded.session_score,
            volatility_regime_score=excluded.volatility_regime_score,
            risk_reward_score=excluded.risk_reward_score,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at""",
        [{**row, "created_at": now, "updated_at": now} for row in rows],
    )
    return len(rows)


def load_adaptive_trade_ledger(
    conn: sqlite3.Connection,
    *,
    symbol: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    ensure_adaptive_calibration_schema(conn)
    sql = "SELECT * FROM smc_adaptive_trade_ledger"
    params: list[Any] = []
    clauses: list[str] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY entry_time DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) if not isinstance(row, dict) else row for row in rows]


def record_adaptive_audit_event(
    conn: sqlite3.Connection,
    *,
    symbol: str = "ALL",
    event_type: str,
    severity: str = "info",
    state_before: Optional[dict] = None,
    state_after: Optional[dict] = None,
    detail: Optional[dict] = None,
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    row = {
        "audit_key": str(uuid4()),
        "symbol": (symbol or "ALL").upper(),
        "event_type": event_type,
        "severity": severity,
        "state_before": _json_dumps(state_before),
        "state_after": _json_dumps(state_after),
        "detail": _json_dumps(detail),
        "created_at": _now_iso(),
    }
    conn.execute(
        """INSERT INTO smc_adaptive_audit_logs (
            audit_key, symbol, event_type, severity, state_before, state_after, detail, created_at
        ) VALUES (
            :audit_key, :symbol, :event_type, :severity, :state_before, :state_after, :detail, :created_at
        )""",
        row,
    )
    return row


def load_adaptive_audit_logs(
    conn: sqlite3.Connection,
    *,
    symbol: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    ensure_adaptive_calibration_schema(conn)
    sql = "SELECT * FROM smc_adaptive_audit_logs"
    params: list[Any] = []
    if symbol:
        sql += " WHERE symbol = ?"
        params.append(symbol.upper())
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) if not isinstance(row, dict) else row for row in rows]


def create_config_patch(
    conn: sqlite3.Connection,
    *,
    patch: dict,
    symbol: str = "ALL",
    reason: str = "",
    strategy_yaml_path: str = "config/strategy.yaml",
    patch_type: str = "strategy",
    apply: bool = False,
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    before = strategy_config_snapshot(strategy_yaml_path)
    merged = _deep_merge(before["data"], patch or {})
    try:
        import yaml

        after_text = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
    except Exception:
        after_text = before["text"]
    after_hash = compute_config_hash(after_text)
    row = {
        "patch_key": str(uuid4()),
        "symbol": (symbol or "ALL").upper(),
        "patch_type": patch_type,
        "reason": reason,
        "patch_payload": _json_dumps(patch),
        "before_hash": before["hash"],
        "after_hash": after_hash,
        "before_config": before["text"],
        "after_config": after_text,
        "applied": 1 if apply else 0,
        "rolled_back": 0,
        "created_at": _now_iso(),
        "applied_at": _now_iso() if apply else None,
        "rolled_back_at": None,
    }
    conn.execute(
        """INSERT INTO smc_adaptive_config_patches (
            patch_key, symbol, patch_type, reason, patch_payload, before_hash, after_hash,
            before_config, after_config, applied, rolled_back, created_at, applied_at, rolled_back_at
        ) VALUES (
            :patch_key, :symbol, :patch_type, :reason, :patch_payload, :before_hash, :after_hash,
            :before_config, :after_config, :applied, :rolled_back, :created_at, :applied_at, :rolled_back_at
        )""",
        row,
    )
    return row


def apply_atomic_config_patch(
    conn: sqlite3.Connection,
    *,
    patch_key: str,
    strategy_yaml_path: str = "config/strategy.yaml",
    expected_hash: Optional[str] = None,
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    row = conn.execute(
        "SELECT * FROM smc_adaptive_config_patches WHERE patch_key=?",
        (patch_key,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown patch_key: {patch_key}")
    row = dict(row)
    current = strategy_config_snapshot(strategy_yaml_path)
    if expected_hash and current["hash"] != expected_hash:
        raise ValueError(
            f"config hash mismatch: expected {expected_hash}, got {current['hash']}"
        )
    if row["before_hash"] and current["hash"] != row["before_hash"]:
        raise ValueError(
            f"config drift detected before patch apply: current={current['hash']} stored={row['before_hash']}"
        )
    path = _resolve_strategy_yaml_path(strategy_yaml_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="strategy.", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(row["after_config"])
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    applied_at = _now_iso()
    conn.execute(
        """UPDATE smc_adaptive_config_patches
           SET applied=1, applied_at=?, after_hash=?
           WHERE patch_key=?""",
        (applied_at, compute_config_hash(row["after_config"]), patch_key),
    )
    return {
        "patch_key": patch_key,
        "path": str(path),
        "before_hash": current["hash"],
        "after_hash": compute_config_hash(row["after_config"]),
        "applied_at": applied_at,
    }


def rollback_config_patch(
    conn: sqlite3.Connection,
    *,
    patch_key: str,
    strategy_yaml_path: str = "config/strategy.yaml",
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    row = conn.execute(
        "SELECT * FROM smc_adaptive_config_patches WHERE patch_key=?",
        (patch_key,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown patch_key: {patch_key}")
    row = dict(row)
    path = _resolve_strategy_yaml_path(strategy_yaml_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="strategy.rollback.", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(row["before_config"])
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    rolled_back_at = _now_iso()
    conn.execute(
        """UPDATE smc_adaptive_config_patches
           SET rolled_back=1, rolled_back_at=?
           WHERE patch_key=?""",
        (rolled_back_at, patch_key),
    )
    return {
        "patch_key": patch_key,
        "path": str(path),
        "restored_hash": compute_config_hash(row["before_config"]),
        "rolled_back_at": rolled_back_at,
    }


def get_kill_switch_state(conn: sqlite3.Connection, *, scope: str = "global") -> dict:
    ensure_adaptive_calibration_schema(conn)
    row = conn.execute(
        "SELECT * FROM smc_adaptive_kill_switch WHERE scope=?",
        (scope,),
    ).fetchone()
    if row is None:
        return {
            "scope": scope,
            "state": "DRY_RUN",
            "reason": "",
            "detail": {},
            "triggered_at": None,
            "updated_at": None,
        }
    payload = dict(row)
    payload["detail"] = _json_loads(payload.get("detail"), {})
    return payload


def set_kill_switch_state(
    conn: sqlite3.Connection,
    *,
    scope: str = "global",
    state: str,
    reason: str = "",
    detail: Optional[dict] = None,
) -> dict:
    ensure_adaptive_calibration_schema(conn)
    now = _now_iso()
    triggered_at = now if state == "LOCKED" else None
    conn.execute(
        """INSERT INTO smc_adaptive_kill_switch (
            scope, state, reason, detail, triggered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope) DO UPDATE SET
            state=excluded.state,
            reason=excluded.reason,
            detail=excluded.detail,
            triggered_at=excluded.triggered_at,
            updated_at=excluded.updated_at""",
        (scope, state, reason, _json_dumps(detail), triggered_at, now),
    )
    return get_kill_switch_state(conn, scope=scope)

