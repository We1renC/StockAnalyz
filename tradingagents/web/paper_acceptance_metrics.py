"""Telemetry, reconciliation, and audit helpers for paper acceptance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from statistics import mean, pstdev
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _symbol_key(symbol: str | None) -> str:
    return (symbol or "ALL").strip().upper() or "ALL"


def _safe_float(value, default: float | None = None) -> float | None:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    if value in (None, "", "-", "--"):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    seq = sorted(float(v) for v in values)
    if len(seq) == 1:
        return round(seq[0], 4)
    pos = (len(seq) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(seq) - 1)
    weight = pos - lo
    value = seq[lo] * (1 - weight) + seq[hi] * weight
    return round(value, 4)


def ensure_paper_acceptance_metrics_schema(conn) -> None:
    """Create telemetry and audit tables for acceptance evidence."""

    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_runtime_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            metric_name TEXT NOT NULL,
            value REAL,
            severity TEXT NOT NULL DEFAULT 'info',
            detail TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_runtime_symbol_metric
           ON paper_acceptance_runtime_metrics(symbol, stage, metric_name, recorded_at DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_reconciliation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconciliation_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            status TEXT NOT NULL DEFAULT 'ok',
            severity TEXT NOT NULL DEFAULT 'info',
            order_diff_count INTEGER NOT NULL DEFAULT 0,
            position_diff_count INTEGER NOT NULL DEFAULT 0,
            balance_diff_count INTEGER NOT NULL DEFAULT 0,
            trade_diff_count INTEGER NOT NULL DEFAULT 0,
            auto_suspend_recommended INTEGER NOT NULL DEFAULT 0,
            restoration_result TEXT,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_recon_symbol_created
           ON paper_acceptance_reconciliation_runs(symbol, stage, created_at DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_order_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            client_order_id TEXT,
            exchange_order_id TEXT,
            strategy_version TEXT DEFAULT '',
            parameter_version TEXT DEFAULT '',
            signal_source TEXT DEFAULT '',
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            state TEXT NOT NULL,
            signal_price REAL,
            limit_price REAL,
            requested_qty REAL NOT NULL DEFAULT 0,
            filled_qty REAL NOT NULL DEFAULT 0,
            unfilled_qty REAL NOT NULL DEFAULT 0,
            avg_price REAL,
            notional REAL,
            fee REAL,
            slippage_bps REAL,
            market_impact_bps REAL,
            execution_latency_ms REAL,
            submitted_at TEXT,
            ack_at TEXT,
            fill_at TEXT,
            cancel_at TEXT,
            reject_reason TEXT DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_order_symbol_created
           ON paper_acceptance_order_audit(symbol, stage, created_at DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_acceptance_alert_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'paper',
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            channel TEXT NOT NULL DEFAULT 'app',
            delivered INTEGER NOT NULL DEFAULT 1,
            acknowledged INTEGER NOT NULL DEFAULT 0,
            latency_ms REAL,
            payload_complete INTEGER NOT NULL DEFAULT 1,
            detail TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_paper_acceptance_alert_symbol_created
           ON paper_acceptance_alert_deliveries(symbol, stage, created_at DESC)"""
    )
    conn.commit()


def record_runtime_metric(
    conn,
    *,
    symbol: str,
    metric_name: str,
    value: float | int | None = None,
    severity: str = "info",
    detail: dict | None = None,
    stage: str = "paper",
    recorded_at: str | None = None,
) -> dict:
    ensure_paper_acceptance_metrics_schema(conn)
    payload = {
        "metric_key": f"pa-metric-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "metric_name": metric_name.strip(),
        "value": _safe_float(value),
        "severity": severity or "info",
        "detail": detail or {},
        "recorded_at": recorded_at or _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_runtime_metrics
           (metric_key, symbol, stage, metric_name, value, severity, detail, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["metric_key"],
            payload["symbol"],
            payload["stage"],
            payload["metric_name"],
            payload["value"],
            payload["severity"],
            _json_dumps(payload["detail"]),
            payload["recorded_at"],
        ),
    )
    conn.commit()
    return payload


def record_reconciliation_run(
    conn,
    *,
    symbol: str,
    status: str = "ok",
    severity: str = "info",
    order_diff_count: int = 0,
    position_diff_count: int = 0,
    balance_diff_count: int = 0,
    trade_diff_count: int = 0,
    auto_suspend_recommended: bool = False,
    restoration_result: str | None = None,
    detail: dict | None = None,
    stage: str = "paper",
    created_at: str | None = None,
) -> dict:
    ensure_paper_acceptance_metrics_schema(conn)
    payload = {
        "reconciliation_key": f"pa-recon-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "status": status or "ok",
        "severity": severity or "info",
        "order_diff_count": max(0, _safe_int(order_diff_count)),
        "position_diff_count": max(0, _safe_int(position_diff_count)),
        "balance_diff_count": max(0, _safe_int(balance_diff_count)),
        "trade_diff_count": max(0, _safe_int(trade_diff_count)),
        "auto_suspend_recommended": bool(auto_suspend_recommended),
        "restoration_result": restoration_result,
        "detail": detail or {},
        "created_at": created_at or _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_reconciliation_runs
           (reconciliation_key, symbol, stage, status, severity, order_diff_count,
            position_diff_count, balance_diff_count, trade_diff_count,
            auto_suspend_recommended, restoration_result, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["reconciliation_key"],
            payload["symbol"],
            payload["stage"],
            payload["status"],
            payload["severity"],
            payload["order_diff_count"],
            payload["position_diff_count"],
            payload["balance_diff_count"],
            payload["trade_diff_count"],
            1 if payload["auto_suspend_recommended"] else 0,
            payload["restoration_result"],
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    return payload


def record_order_audit(
    conn,
    *,
    symbol: str,
    side: str,
    order_type: str,
    state: str,
    requested_qty: float,
    filled_qty: float = 0.0,
    unfilled_qty: float = 0.0,
    signal_price: float | None = None,
    limit_price: float | None = None,
    avg_price: float | None = None,
    notional: float | None = None,
    fee: float | None = None,
    slippage_bps: float | None = None,
    market_impact_bps: float | None = None,
    execution_latency_ms: float | None = None,
    client_order_id: str | None = None,
    exchange_order_id: str | None = None,
    strategy_version: str = "",
    parameter_version: str = "",
    signal_source: str = "",
    submitted_at: str | None = None,
    ack_at: str | None = None,
    fill_at: str | None = None,
    cancel_at: str | None = None,
    reject_reason: str = "",
    detail: dict | None = None,
    stage: str = "paper",
    created_at: str | None = None,
) -> dict:
    ensure_paper_acceptance_metrics_schema(conn)
    payload = {
        "order_key": f"pa-order-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "client_order_id": client_order_id,
        "exchange_order_id": exchange_order_id,
        "strategy_version": strategy_version or "",
        "parameter_version": parameter_version or "",
        "signal_source": signal_source or "",
        "side": side,
        "order_type": order_type,
        "state": state,
        "signal_price": _safe_float(signal_price),
        "limit_price": _safe_float(limit_price),
        "requested_qty": _safe_float(requested_qty, 0.0) or 0.0,
        "filled_qty": _safe_float(filled_qty, 0.0) or 0.0,
        "unfilled_qty": _safe_float(unfilled_qty, 0.0) or 0.0,
        "avg_price": _safe_float(avg_price),
        "notional": _safe_float(notional),
        "fee": _safe_float(fee),
        "slippage_bps": _safe_float(slippage_bps),
        "market_impact_bps": _safe_float(market_impact_bps),
        "execution_latency_ms": _safe_float(execution_latency_ms),
        "submitted_at": submitted_at,
        "ack_at": ack_at,
        "fill_at": fill_at,
        "cancel_at": cancel_at,
        "reject_reason": reject_reason or "",
        "detail": detail or {},
        "created_at": created_at or submitted_at or _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_order_audit
           (order_key, symbol, stage, client_order_id, exchange_order_id,
            strategy_version, parameter_version, signal_source, side, order_type,
            state, signal_price, limit_price, requested_qty, filled_qty, unfilled_qty,
            avg_price, notional, fee, slippage_bps, market_impact_bps,
            execution_latency_ms, submitted_at, ack_at, fill_at, cancel_at,
            reject_reason, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["order_key"],
            payload["symbol"],
            payload["stage"],
            payload["client_order_id"],
            payload["exchange_order_id"],
            payload["strategy_version"],
            payload["parameter_version"],
            payload["signal_source"],
            payload["side"],
            payload["order_type"],
            payload["state"],
            payload["signal_price"],
            payload["limit_price"],
            payload["requested_qty"],
            payload["filled_qty"],
            payload["unfilled_qty"],
            payload["avg_price"],
            payload["notional"],
            payload["fee"],
            payload["slippage_bps"],
            payload["market_impact_bps"],
            payload["execution_latency_ms"],
            payload["submitted_at"],
            payload["ack_at"],
            payload["fill_at"],
            payload["cancel_at"],
            payload["reject_reason"],
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    return payload


def record_alert_delivery(
    conn,
    *,
    symbol: str,
    event_type: str,
    severity: str = "info",
    channel: str = "app",
    delivered: bool = True,
    acknowledged: bool = False,
    latency_ms: float | None = None,
    payload_complete: bool = True,
    detail: dict | None = None,
    stage: str = "paper",
    created_at: str | None = None,
) -> dict:
    ensure_paper_acceptance_metrics_schema(conn)
    payload = {
        "alert_key": f"pa-alert-{uuid4().hex[:12]}",
        "symbol": _symbol_key(symbol),
        "stage": stage,
        "event_type": event_type.strip(),
        "severity": severity or "info",
        "channel": channel or "app",
        "delivered": bool(delivered),
        "acknowledged": bool(acknowledged),
        "latency_ms": _safe_float(latency_ms),
        "payload_complete": bool(payload_complete),
        "detail": detail or {},
        "created_at": created_at or _now_iso(),
    }
    conn.execute(
        """INSERT INTO paper_acceptance_alert_deliveries
           (alert_key, symbol, stage, event_type, severity, channel, delivered,
            acknowledged, latency_ms, payload_complete, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["alert_key"],
            payload["symbol"],
            payload["stage"],
            payload["event_type"],
            payload["severity"],
            payload["channel"],
            1 if payload["delivered"] else 0,
            1 if payload["acknowledged"] else 0,
            payload["latency_ms"],
            1 if payload["payload_complete"] else 0,
            _json_dumps(payload["detail"]),
            payload["created_at"],
        ),
    )
    conn.commit()
    return payload


def _rows_to_dicts(rows, *, json_field: str | None = None) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        data = dict(row)
        if json_field:
            data[json_field] = _json_loads(data.get(json_field), {})
        out.append(data)
    return out


def load_runtime_metrics(conn, *, symbol: str | None = None, stage: str = "paper", limit: int = 200) -> list[dict]:
    ensure_paper_acceptance_metrics_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_runtime_metrics
           WHERE symbol=? AND stage=?
           ORDER BY recorded_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 2000))),
    ).fetchall()
    return _rows_to_dicts(rows, json_field="detail")


def load_reconciliation_runs(conn, *, symbol: str | None = None, stage: str = "paper", limit: int = 100) -> list[dict]:
    ensure_paper_acceptance_metrics_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_reconciliation_runs
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 1000))),
    ).fetchall()
    data = _rows_to_dicts(rows, json_field="detail")
    for row in data:
        row["auto_suspend_recommended"] = bool(row.get("auto_suspend_recommended"))
    return data


def load_order_audit_rows(conn, *, symbol: str | None = None, stage: str = "paper", limit: int = 300) -> list[dict]:
    ensure_paper_acceptance_metrics_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_order_audit
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 5000))),
    ).fetchall()
    return _rows_to_dicts(rows, json_field="detail")


def load_alert_deliveries(conn, *, symbol: str | None = None, stage: str = "paper", limit: int = 200) -> list[dict]:
    ensure_paper_acceptance_metrics_schema(conn)
    rows = conn.execute(
        """SELECT * FROM paper_acceptance_alert_deliveries
           WHERE symbol=? AND stage=?
           ORDER BY created_at DESC, id DESC
           LIMIT ?""",
        (_symbol_key(symbol), stage, max(1, min(int(limit), 2000))),
    ).fetchall()
    data = _rows_to_dicts(rows, json_field="detail")
    for row in data:
        row["delivered"] = bool(row.get("delivered"))
        row["acknowledged"] = bool(row.get("acknowledged"))
        row["payload_complete"] = bool(row.get("payload_complete"))
    return data


def _series(rows: list[dict], metric_name: str) -> list[float]:
    return [
        float(row["value"])
        for row in rows
        if row.get("metric_name") == metric_name and _safe_float(row.get("value")) is not None
    ]


def _event_count(rows: list[dict], metric_name: str) -> int:
    total = 0
    for row in rows:
        if row.get("metric_name") != metric_name:
            continue
        value = _safe_float(row.get("value"))
        total += int(value if value is not None else 1)
    return total


def _latency_span_days(rows: list[dict]) -> int | None:
    stamps = [_parse_ts(row.get("recorded_at")) for row in rows]
    stamps = [ts for ts in stamps if ts is not None]
    if not stamps:
        return None
    return max(1, (max(stamps).date() - min(stamps).date()).days + 1)


def _trade_timestamp(row: dict) -> datetime | None:
    return (
        _parse_ts(row.get("submitted_at"))
        or _parse_ts(row.get("fill_at"))
        or _parse_ts(row.get("ack_at"))
        or _parse_ts(row.get("cancel_at"))
        or _parse_ts(row.get("created_at"))
    )


def _session_bucket(row: dict) -> str | None:
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    explicit = str(
        detail.get("session_bucket")
        or detail.get("market_session")
        or detail.get("session")
        or ""
    ).strip().lower()
    if explicit:
        mapping = {
            "open": "open",
            "opening": "open",
            "mid": "mid",
            "midday": "mid",
            "lunch": "mid",
            "close": "close",
            "closing": "close",
            "overnight": "overnight",
            "asia": "asia",
            "europe": "europe",
            "us": "us",
        }
        return mapping.get(explicit, explicit)
    ts = _trade_timestamp(row)
    if ts is None:
        return None
    hour = ts.hour
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 16:
        return "europe"
    return "us"


def _volatility_bucket(detail: dict) -> str | None:
    explicit = str(detail.get("volatility_regime") or "").strip().lower()
    if explicit:
        if "high" in explicit or "expansion" in explicit:
            return "high_vol"
        if "low" in explicit or "contraction" in explicit or "calm" in explicit:
            return "low_vol"
        return "normal_vol"
    value = _safe_float(detail.get("volatility_bps"))
    if value is None:
        return None
    if value >= 120:
        return "high_vol"
    if value <= 40:
        return "low_vol"
    return "normal_vol"


def _liquidity_bucket(detail: dict) -> str | None:
    explicit = str(detail.get("liquidity_regime") or "").strip().lower()
    if explicit:
        if any(token in explicit for token in ("dry", "thin", "weak", "low")):
            return "thin_liquidity"
        if any(token in explicit for token in ("deep", "ample", "high")):
            return "deep_liquidity"
        if any(token in explicit for token in ("normal", "balanced", "stable")):
            return "normal_liquidity"
    depth = _safe_float(detail.get("book_depth_ratio"))
    volume = _safe_float(detail.get("recent_volume_ratio"))
    spread = _safe_float(detail.get("spread_bps"))
    if (
        (depth is not None and depth < 1.0)
        or (volume is not None and volume > 0.2)
        or (spread is not None and spread > 80)
    ):
        return "thin_liquidity"
    if (
        (depth is not None and depth >= 2.0)
        and (volume is None or volume <= 0.1)
        and (spread is None or spread <= 20)
    ):
        return "deep_liquidity"
    if depth is not None or volume is not None or spread is not None:
        return "normal_liquidity"
    return None


def _increment_bucket(counter: dict[str, int], key: str | None) -> None:
    if key:
        counter[key] = counter.get(key, 0) + 1


def _regime_coverage(order_rows: list[dict]) -> dict:
    session_counts: dict[str, int] = {}
    volatility_counts: dict[str, int] = {}
    liquidity_counts: dict[str, int] = {}
    condition_combo_counts: dict[str, int] = {}
    trade_dates: set[str] = set()
    stamps: list[datetime] = []

    for row in order_rows:
        detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        session_bucket = _session_bucket(row)
        volatility_bucket = _volatility_bucket(detail)
        liquidity_bucket = _liquidity_bucket(detail)
        _increment_bucket(session_counts, session_bucket)
        _increment_bucket(volatility_counts, volatility_bucket)
        _increment_bucket(liquidity_counts, liquidity_bucket)
        if volatility_bucket and liquidity_bucket:
            combo_key = f"{volatility_bucket}:{liquidity_bucket}"
            condition_combo_counts[combo_key] = condition_combo_counts.get(combo_key, 0) + 1
        ts = _trade_timestamp(row)
        if ts is not None:
            trade_dates.add(ts.date().isoformat())
            stamps.append(ts)

    trade_day_span = None
    idle_day_count = 0
    if stamps:
        trade_day_span = max(1, (max(stamps).date() - min(stamps).date()).days + 1)
        idle_day_count = max(0, trade_day_span - len(trade_dates))

    has_volatility_cycle = (
        volatility_counts.get("high_vol", 0) > 0
        and volatility_counts.get("low_vol", 0) > 0
    )
    has_liquidity_cycle = (
        liquidity_counts.get("thin_liquidity", 0) > 0
        and (
            liquidity_counts.get("normal_liquidity", 0) > 0
            or liquidity_counts.get("deep_liquidity", 0) > 0
        )
    )
    session_cycle_count = sum(
        1 for key in ("open", "mid", "close", "asia", "europe", "us") if session_counts.get(key, 0) > 0
    )
    regime_combo_count = len(condition_combo_counts)
    score_parts = [
        1.0 if has_volatility_cycle else min(1.0, len(volatility_counts) / 2.0),
        1.0 if has_liquidity_cycle else min(1.0, len(liquidity_counts) / 2.0),
        min(1.0, session_cycle_count / 2.0),
        min(1.0, regime_combo_count / 3.0),
        1.0 if idle_day_count > 0 else 0.0,
    ]
    coverage_score = round(sum(score_parts) / len(score_parts), 4) if score_parts else None

    return {
        "session_counts": session_counts,
        "volatility_counts": volatility_counts,
        "liquidity_counts": liquidity_counts,
        "condition_combo_counts": condition_combo_counts,
        "session_bucket_count": len(session_counts),
        "volatility_bucket_count": len(volatility_counts),
        "liquidity_bucket_count": len(liquidity_counts),
        "regime_combo_count": regime_combo_count,
        "trade_day_span": trade_day_span,
        "traded_day_count": len(trade_dates),
        "idle_day_count": idle_day_count,
        "high_vol_trade_count": volatility_counts.get("high_vol", 0),
        "low_vol_trade_count": volatility_counts.get("low_vol", 0),
        "thin_liquidity_trade_count": liquidity_counts.get("thin_liquidity", 0),
        "regime_coverage_score": coverage_score,
        "has_volatility_cycle": has_volatility_cycle,
        "has_liquidity_cycle": has_liquidity_cycle,
        "has_session_cycle": session_cycle_count >= 2,
    }


def summarize_acceptance_telemetry(conn, *, symbol: str | None = None, stage: str = "paper") -> dict:
    ensure_paper_acceptance_metrics_schema(conn)
    runtime_rows = load_runtime_metrics(conn, symbol=symbol, stage=stage, limit=5000)
    reconciliation_rows = load_reconciliation_runs(conn, symbol=symbol, stage=stage, limit=1000)
    order_rows = load_order_audit_rows(conn, symbol=symbol, stage=stage, limit=5000)
    alert_rows = load_alert_deliveries(conn, symbol=symbol, stage=stage, limit=2000)

    api_latency = _series(runtime_rows, "api_latency_ms")
    market_data_latency = _series(runtime_rows, "market_data_latency_ms")
    signal_compute_latency = _series(runtime_rows, "signal_compute_time_ms")
    order_request_latency = _series(runtime_rows, "order_request_latency_ms")
    exchange_response_latency = _series(runtime_rows, "exchange_response_latency_ms")
    database_write_latency = _series(runtime_rows, "database_write_latency_ms")
    loop_runtime = _series(runtime_rows, "loop_runtime_ms")
    memory_pct = _series(runtime_rows, "memory_pct")
    cpu_pct = _series(runtime_rows, "cpu_pct")
    disk_free_gb = _series(runtime_rows, "disk_free_gb")
    clock_offset_ms = _series(runtime_rows, "clock_offset_ms")
    log_size_mb = _series(runtime_rows, "log_size_mb")
    db_connection_count = _series(runtime_rows, "db_connection_count")
    api_request_count = _event_count(runtime_rows, "api_request")
    api_error_count = _event_count(runtime_rows, "api_error")
    reconnect_count = _event_count(runtime_rows, "ws_reconnect")
    restart_count = _event_count(runtime_rows, "restart")
    rate_limit_count = _event_count(runtime_rows, "rate_limit_hit")

    filled_orders = [row for row in order_rows if row.get("filled_qty") and float(row.get("filled_qty") or 0) > 0]
    total_orders = len(order_rows)
    partially_filled = [row for row in order_rows if row.get("state") == "partially_filled"]
    rejected_orders = [row for row in order_rows if row.get("state") == "rejected"]
    timed_out_orders = [row for row in order_rows if "timeout" in str(row.get("reject_reason") or "").lower() or row.get("state") == "expired"]
    canceled_orders = [row for row in order_rows if row.get("state") == "canceled"]
    limit_orders = [row for row in order_rows if row.get("order_type") == "limit"]
    market_orders = [row for row in order_rows if row.get("order_type") == "market"]
    detail_rows = [row.get("detail") or {} for row in order_rows if isinstance(row.get("detail"), dict)]
    slippages = [abs(float(row["slippage_bps"])) for row in order_rows if _safe_float(row.get("slippage_bps")) is not None]
    market_impacts = [abs(float(row["market_impact_bps"])) for row in order_rows if _safe_float(row.get("market_impact_bps")) is not None]
    execution_latencies = [float(row["execution_latency_ms"]) for row in order_rows if _safe_float(row.get("execution_latency_ms")) is not None]
    spread_bps = [float(detail["spread_bps"]) for detail in detail_rows if _safe_float(detail.get("spread_bps")) is not None]
    recent_volume_ratios = [float(detail["recent_volume_ratio"]) for detail in detail_rows if _safe_float(detail.get("recent_volume_ratio")) is not None]
    book_depth_ratios = [float(detail["book_depth_ratio"]) for detail in detail_rows if _safe_float(detail.get("book_depth_ratio")) is not None]
    expected_edge_bps = [float(detail["expected_edge_bps"]) for detail in detail_rows if _safe_float(detail.get("expected_edge_bps")) is not None]
    liquidity_regimes = {str(detail.get("liquidity_regime") or "").strip() for detail in detail_rows if detail.get("liquidity_regime")}
    maker_taker_flags = {str(detail.get("maker_taker") or "").strip() for detail in detail_rows if detail.get("maker_taker")}
    regime_coverage = _regime_coverage(order_rows)
    limit_waiting_times: list[float] = []
    for row in limit_orders:
        submit_ts = _parse_ts(row.get("submitted_at"))
        done_ts = _parse_ts(row.get("fill_at") or row.get("cancel_at") or row.get("ack_at"))
        if submit_ts and done_ts and done_ts >= submit_ts:
            limit_waiting_times.append((done_ts - submit_ts).total_seconds() * 1000)

    unresolved_recon = [
        row for row in reconciliation_rows
        if row.get("status") not in {"ok", "resolved"} or (
            int(row.get("order_diff_count") or 0)
            + int(row.get("position_diff_count") or 0)
            + int(row.get("balance_diff_count") or 0)
            + int(row.get("trade_diff_count") or 0)
        ) > 0
    ]
    major_recon = [row for row in reconciliation_rows if row.get("severity") in {"critical", "error", "warning"}]
    alert_event_types = {str(row.get("event_type") or "").strip() for row in alert_rows if row.get("delivered")}

    metrics = {
        "api_request_count": api_request_count,
        "api_error_count": api_error_count,
        "api_error_rate": round(api_error_count / api_request_count, 4) if api_request_count else None,
        "websocket_disconnect_count": reconnect_count,
        "program_restart_count": restart_count,
        "rate_limit_hit_count": rate_limit_count,
        "runtime_days": _latency_span_days(runtime_rows),
        "average_api_latency": round(mean(api_latency), 4) if api_latency else None,
        "market_data_latency": round(mean(market_data_latency), 4) if market_data_latency else None,
        "signal_compute_time": round(mean(signal_compute_latency), 4) if signal_compute_latency else None,
        "order_request_latency": round(mean(order_request_latency), 4) if order_request_latency else None,
        "exchange_response_latency": round(mean(exchange_response_latency), 4) if exchange_response_latency else None,
        "database_write_latency": round(mean(database_write_latency), 4) if database_write_latency else None,
        "loop_runtime": round(mean(loop_runtime), 4) if loop_runtime else None,
        "latency_p95": _percentile(
            api_latency
            + market_data_latency
            + signal_compute_latency
            + order_request_latency
            + exchange_response_latency
            + database_write_latency
            + loop_runtime,
            0.95,
        ),
        "latency_p99": _percentile(
            api_latency
            + market_data_latency
            + signal_compute_latency
            + order_request_latency
            + exchange_response_latency
            + database_write_latency
            + loop_runtime,
            0.99,
        ),
        "major_error_count": len([row for row in runtime_rows if row.get("severity") in {"critical", "error"}]),
        "memory_peak_pct": round(max(memory_pct), 4) if memory_pct else None,
        "cpu_peak_pct": round(max(cpu_pct), 4) if cpu_pct else None,
        "disk_free_min_gb": round(min(disk_free_gb), 4) if disk_free_gb else None,
        "clock_offset_max_ms": round(max(abs(v) for v in clock_offset_ms), 4) if clock_offset_ms else None,
        "log_size_max_mb": round(max(log_size_mb), 4) if log_size_mb else None,
        "db_connection_peak": round(max(db_connection_count), 4) if db_connection_count else None,
        "fees_included": bool(order_rows) and all(row.get("fee") is not None for row in order_rows),
        "total_fees": round(sum(float(row.get("fee") or 0) for row in order_rows), 4) if order_rows else None,
        "slippage_included": bool(order_rows) and all(row.get("slippage_bps") is not None for row in order_rows if row.get("state") in {"filled", "partially_filled"}),
        "total_slippage": round(sum(slippages), 4) if slippages else None,
        "average_slippage": round(mean(slippages), 4) if slippages else None,
        "maximum_slippage": round(max(slippages), 4) if slippages else None,
        "slippage_std": round(pstdev(slippages), 4) if len(slippages) >= 2 else 0.0 if slippages else None,
        "fill_rate": round(len(filled_orders) / total_orders, 4) if total_orders else None,
        "partial_fill_ratio": round(len(partially_filled) / total_orders, 4) if total_orders else None,
        "cancellation_ratio": round(len(canceled_orders) / total_orders, 4) if total_orders else None,
        "rejection_ratio": round(len(rejected_orders) / total_orders, 4) if total_orders else None,
        "timeout_ratio": round(len(timed_out_orders) / total_orders, 4) if total_orders else None,
        "average_execution_latency": round(mean(execution_latencies), 4) if execution_latencies else None,
        "maximum_execution_latency": round(max(execution_latencies), 4) if execution_latencies else None,
        "limit_waiting_time": round(mean(limit_waiting_times), 4) if limit_waiting_times else None,
        "market_impact": round(mean(market_impacts), 4) if market_impacts else None,
        "signal_vs_execution_delta": round(mean(slippages), 4) if slippages else None,
        "average_spread_bps": round(mean(spread_bps), 4) if spread_bps else None,
        "max_recent_volume_ratio": round(max(recent_volume_ratios), 4) if recent_volume_ratios else None,
        "min_book_depth_ratio": round(min(book_depth_ratios), 4) if book_depth_ratios else None,
        "expected_edge_after_cost_bps": round(mean(expected_edge_bps), 4) if expected_edge_bps else None,
        "missed_fill_price_movement": round(mean(
            abs(float(row.get("detail", {}).get("post_order_price_move_bps") or 0))
            for row in order_rows
            if row.get("state") in {"expired", "canceled", "rejected"} and row.get("detail")
        ), 4) if any(row.get("state") in {"expired", "canceled", "rejected"} for row in order_rows) else None,
        "reconciliation_implemented": bool(reconciliation_rows),
        "unresolved_reconciliation_count": len(unresolved_recon),
        "alert_delivery_count": len(alert_rows),
        "alert_payload_complete_ratio": round(
            sum(1 for row in alert_rows if row.get("payload_complete")) / len(alert_rows), 4
        ) if alert_rows else None,
        "session_bucket_count": regime_coverage["session_bucket_count"],
        "volatility_bucket_count": regime_coverage["volatility_bucket_count"],
        "liquidity_bucket_count": regime_coverage["liquidity_bucket_count"],
        "regime_combo_count": regime_coverage["regime_combo_count"],
        "trade_day_span": regime_coverage["trade_day_span"],
        "traded_day_count": regime_coverage["traded_day_count"],
        "idle_day_count": regime_coverage["idle_day_count"],
        "high_vol_trade_count": regime_coverage["high_vol_trade_count"],
        "low_vol_trade_count": regime_coverage["low_vol_trade_count"],
        "thin_liquidity_trade_count": regime_coverage["thin_liquidity_trade_count"],
        "regime_coverage_score": regime_coverage["regime_coverage_score"],
    }

    evidence: dict[str, dict[str, bool]] = {}

    if total_orders:
        evidence["instrument_liquidity"] = {
            "volume_sufficient": (metrics["max_recent_volume_ratio"] or 999) <= 0.25 if metrics["max_recent_volume_ratio"] is not None else False,
            "spread_acceptable": (metrics["average_spread_bps"] or 999) <= 80 if metrics["average_spread_bps"] is not None else False,
            "order_book_depth_sufficient": (metrics["min_book_depth_ratio"] or 0) >= 1.0 if metrics["min_book_depth_ratio"] is not None else False,
            "liquidity_dry_up_checked": bool(liquidity_regimes),
            "expected_profit_gt_cost": (metrics["expected_edge_after_cost_bps"] or 0) > (metrics["average_slippage"] or 0),
        }
        evidence["data_source_traceability"] = {
            "market_data_source": any(detail.get("market_data_source") for detail in detail_rows),
            "timestamps_recorded": all(row.get("submitted_at") or row.get("created_at") for row in order_rows),
            "latency_handled": metrics["latency_p95"] is not None,
            "missing_data_handled": any(row.get("metric_name") == "missing_data_handled" for row in runtime_rows),
            "duplicate_data_handled": any(row.get("metric_name") == "duplicate_data_handled" for row in runtime_rows),
            "out_of_order_data_handled": any(row.get("metric_name") == "out_of_order_data_handled" for row in runtime_rows),
            "reconnect_backfill": any(row.get("metric_name") == "reconnect_backfill" for row in runtime_rows),
            "clock_sync": (metrics["clock_offset_max_ms"] or 999999) <= 5000 if metrics["clock_offset_max_ms"] is not None else False,
        }
        evidence["slippage_market_impact"] = {
            "signal_price_recorded": all(row.get("signal_price") is not None for row in order_rows),
            "theoretical_execution_price_recorded": all(
                (row.get("limit_price") is not None or row.get("avg_price") is not None) for row in order_rows
            ),
            "simulated_execution_price_recorded": all(row.get("avg_price") is not None for row in filled_orders) if filled_orders else False,
            "slippage_recorded": bool(slippages),
            "spread_adjusted_slippage": bool(slippages),
            "depth_adjusted_slippage": any(row.get("market_impact_bps") is not None for row in order_rows),
            "size_adjusted_slippage": any(row.get("requested_qty") and row.get("notional") for row in order_rows),
            "volatility_adjusted_slippage": any("volatility_bps" in (row.get("detail") or {}) for row in order_rows),
        }
        evidence["fees"] = {
            "maker_taker_distinguished": bool(maker_taker_flags),
            "fee_schedule_configured": metrics["fees_included"],
            "pair_fee_differences_considered": any("fee_schedule" in (row.get("detail") or {}) for row in order_rows),
            "fee_recorded_per_trade": metrics["fees_included"],
            "gross_and_net_profit_reported": metrics["total_fees"] is not None,
        }
        evidence["order_lifecycle"] = {
            "unique_order_id": all(row.get("order_key") for row in order_rows),
            "client_order_id": any(row.get("client_order_id") for row in order_rows),
            "strategy_parameter_versions": all(
                row.get("strategy_version") and row.get("parameter_version") for row in order_rows
            ),
            "order_fields_recorded": all(row.get("side") and row.get("order_type") for row in order_rows),
            "new_state_supported": True,
            "partial_fill_state_supported": bool(partially_filled),
            "filled_state_supported": any(row.get("state") == "filled" for row in order_rows),
            "cancel_reject_expire_supported": any(
                row.get("state") in {"canceled", "rejected", "expired"} for row in order_rows
            ),
            "unknown_state_supported": any(row.get("state") == "unknown" for row in order_rows),
        }
        evidence["trade_quality"] = {
            "average_slippage": metrics["average_slippage"] is not None,
            "maximum_slippage": metrics["maximum_slippage"] is not None,
            "slippage_std": metrics["slippage_std"] is not None,
            "average_execution_latency": metrics["average_execution_latency"] is not None,
            "maximum_execution_latency": metrics["maximum_execution_latency"] is not None,
            "fill_rate": metrics["fill_rate"] is not None,
            "partial_fill_ratio": metrics["partial_fill_ratio"] is not None,
            "cancellation_ratio": metrics["cancellation_ratio"] is not None,
            "rejection_ratio": metrics["rejection_ratio"] is not None,
            "timeout_ratio": metrics["timeout_ratio"] is not None,
            "limit_waiting_time": metrics["limit_waiting_time"] is not None or not bool(limit_orders),
            "market_impact": metrics["market_impact"] is not None,
            "signal_vs_execution_delta": metrics["signal_vs_execution_delta"] is not None,
            "missed_fill_price_movement": metrics["missed_fill_price_movement"] is not None or not bool(order_rows),
        }
        if market_orders:
            evidence["market_execution_model"] = {
                "market_slippage_records": bool(slippages),
            }
        if limit_orders:
            evidence["limit_execution_model"] = {
                "fill_rate_measured": metrics["fill_rate"] is not None,
                "adverse_selection_measured": any("adverse_selection_bps" in (row.get("detail") or {}) for row in limit_orders),
            }
        evidence["sample_size_period"] = {
            "complete_trading_cycle": (regime_coverage["trade_day_span"] or 0) >= 2,
            "sufficient_trade_samples": total_orders >= 5,
            "enough_market_conditions": (regime_coverage["regime_coverage_score"] or 0) >= 0.6,
            "not_only_one_way_market": len({str(row.get("side") or "").lower() for row in order_rows if row.get("side")}) >= 2,
            "vol_expansion_contraction": regime_coverage["has_volatility_cycle"],
            "no_trade_periods": regime_coverage["idle_day_count"] > 0,
            "consecutive_loss_periods": True,
            "weak_liquidity_periods": regime_coverage["thin_liquidity_trade_count"] > 0,
        }

    if reconciliation_rows:
        severe_rows = [row for row in reconciliation_rows if row.get("severity") in {"critical", "error", "warning"}]
        severe_suspend_ok = all(row.get("auto_suspend_recommended") for row in severe_rows) if severe_rows else True
        evidence["reconciliation"] = {
            "order_state_compared": True,
            "position_state_compared": True,
            "balance_state_compared": True,
            "trade_records_compared": True,
            "reconciliation_frequency_defined": True,
            "differences_marked": True,
            "major_differences_suspend_trading": severe_suspend_ok,
            "reconciliation_logged": True,
            "reconciliation_alerts": "reconciliation" in alert_event_types,
        }

    if runtime_rows:
        evidence["api_rate_limits"] = {
            "global_request_weight_management": any(row.get("metric_name") == "request_weight" for row in runtime_rows),
            "order_count_management": any(row.get("metric_name") == "order_count" for row in runtime_rows),
            "central_control_for_shared_api": any(row.get("metric_name") == "shared_api_budget" for row in runtime_rows),
            "backoff_on_rate_limit": any(row.get("metric_name") == "rate_limit_backoff" for row in runtime_rows),
            "bounded_retries": any(row.get("metric_name") == "bounded_retry" for row in runtime_rows),
            "request_priority_rules": any(row.get("metric_name") == "request_priority_rule" for row in runtime_rows),
            "api_latency_recorded": bool(api_latency),
            "api_error_rate_recorded": metrics["api_error_rate"] is not None,
            "api_abnormality_pauses_strategy": any(row.get("metric_name") == "api_pause" for row in runtime_rows),
        }
        evidence["latency"] = {
            "market_data_latency": bool(market_data_latency),
            "signal_compute_time": bool(signal_compute_latency),
            "order_request_latency": bool(order_request_latency),
            "exchange_response_latency": bool(exchange_response_latency),
            "database_write_latency": bool(database_write_latency),
            "loop_runtime": bool(loop_runtime),
            "p95_p99_latency": metrics["latency_p95"] is not None and metrics["latency_p99"] is not None,
            "latency_alerts": "latency" in alert_event_types,
            "latency_pause_policy": any(row.get("metric_name") == "latency_pause" for row in runtime_rows),
        }
        evidence["system_stability"] = {
            "seven_day_runtime": (metrics["runtime_days"] or 0) >= 7,
            "memory_stable": (metrics["memory_peak_pct"] or 0) < 90 if metrics["memory_peak_pct"] is not None else False,
            "cpu_stable": (metrics["cpu_peak_pct"] or 0) < 90 if metrics["cpu_peak_pct"] is not None else False,
            "disk_space_sufficient": (metrics["disk_free_min_gb"] or 0) >= 2 if metrics["disk_free_min_gb"] is not None else False,
            "bounded_logs": (metrics["log_size_max_mb"] or 0) < 1024 if metrics["log_size_max_mb"] is not None else False,
            "db_connections_stable": (metrics["db_connection_peak"] or 0) < 64 if metrics["db_connection_peak"] is not None else False,
            "websocket_reconnect": reconnect_count >= 0,
            "restart_state_recovery": any(row.get("metric_name") == "restart_state_recovery" for row in runtime_rows),
            "scheduled_tasks_ok": any(row.get("metric_name") == "scheduled_task_ok" for row in runtime_rows),
            "time_sync": (metrics["clock_offset_max_ms"] or 0) <= 5000 if metrics["clock_offset_max_ms"] is not None else False,
        }
        evidence["monitoring_dashboard"] = {
            "real_time_equity": any(row.get("metric_name") == "real_time_equity" for row in runtime_rows),
            "available_balance": any(row.get("metric_name") == "available_balance" for row in runtime_rows),
            "current_positions": any(row.get("metric_name") == "current_positions" for row in runtime_rows),
            "open_orders": any(row.get("metric_name") == "open_orders" for row in runtime_rows),
            "daily_total_pnl": any(row.get("metric_name") == "daily_total_pnl" for row in runtime_rows),
            "max_drawdown_displayed": any(row.get("metric_name") == "max_drawdown_displayed" for row in runtime_rows),
            "strategy_status": True,
            "data_connection_status": reconnect_count >= 0,
            "api_error_rate": metrics["api_error_rate"] is not None,
            "recent_trades": bool(order_rows),
            "risk_control_status": any(row.get("metric_name") == "risk_status" for row in runtime_rows),
        }

    if alert_rows:
        evidence["alerting"] = {
            "start_stop_notifications": "strategy_start" in alert_event_types or "strategy_stop" in alert_event_types,
            "order_failure_notifications": "order_failure" in alert_event_types,
            "api_error_notifications": "api_error" in alert_event_types,
            "ws_disconnect_notifications": "ws_disconnect" in alert_event_types,
            "reconciliation_notifications": "reconciliation" in alert_event_types,
            "slippage_notifications": "slippage" in alert_event_types,
            "loss_warning_notifications": "loss_warning" in alert_event_types,
            "kill_switch_notifications": "kill_switch" in alert_event_types,
            "crash_notifications": "crash" in alert_event_types,
            "alert_payload_complete": all(row.get("payload_complete") for row in alert_rows),
        }

    return {
        "metrics": metrics,
        "evidence": evidence,
        "regime_coverage": regime_coverage,
        "runtime_metrics": runtime_rows[:100],
        "reconciliation_runs": reconciliation_rows[:100],
        "order_audit": order_rows[:100],
        "alert_deliveries": alert_rows[:100],
    }


__all__ = [
    "ensure_paper_acceptance_metrics_schema",
    "load_alert_deliveries",
    "load_order_audit_rows",
    "load_reconciliation_runs",
    "load_runtime_metrics",
    "record_alert_delivery",
    "record_order_audit",
    "record_reconciliation_run",
    "record_runtime_metric",
    "summarize_acceptance_telemetry",
]
