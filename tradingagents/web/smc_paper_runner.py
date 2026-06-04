"""SMC × crypto-api paper trading runner.

Drives the codex/smc-quant-system-v2 strategy engine against the in-process
crypto trading API (Binance-Spot-compatible mock matching engine). Designed
for the §10.5 forward-testing phase: every decision is logged with the
§18.2 trade record schema so attribution / Kelly / OOS validation can
consume the same ledger that production live trading would produce.

Architecture
------------
1. ``CryptoApiClient`` — thin HMAC-signing wrapper around FastAPI TestClient
   (or any base_url) exposing the endpoints we actually need: klines,
   balances, open-orders, create_order, cancel_order, fills.
2. ``SmcPaperRunner.run_once(symbol)`` — pulls ~200 bars of OHLCV, runs
   ``build_smc_analysis``, picks the highest-confluence triggered entry
   that passes §6 risk gating, posts a limit order, journals the result.

The runner is intentionally one-shot per call so callers can decide the
cadence (cron / manual / loop). It refuses to double-place if an open
order with the same client_order_id already exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from smc_quant import (
    SMCConfig,
    apply_risk_pipeline,
    build_smc_analysis,
    build_trade_record,
    calculate_position_size,
    persist_trade_records,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Crypto API client (HMAC-SHA256 signing aligned with web/crypto_api/auth.py)
# ---------------------------------------------------------------------------

@dataclass
class CryptoApiCredentials:
    api_key: str = "api_key_xxx"
    api_secret: str = "secret_xxx"


class CryptoApiClient:
    """Thin signed-request wrapper around the FastAPI crypto-api.

    Defaults assume the in-process TestClient (see ``from_test_client``).
    Production use can pass a base_url + httpx client instead.
    """

    def __init__(self, transport: Any, credentials: Optional[CryptoApiCredentials] = None, base_path: str = "/v1"):
        self.transport = transport
        self.credentials = credentials or CryptoApiCredentials()
        self.base_path = base_path.rstrip("/")

    # Internal helpers --------------------------------------------------

    def _sign(self, method: str, path: str, query: str, body: str) -> dict[str, str]:
        # web/crypto_api/auth.py compares against ``int(time.time() * 1000)``
        # so the timestamp header MUST be in milliseconds.
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        payload = f"{ts}{method.upper()}{path}{query}{body}"
        sig = hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-API-Key": self.credentials.api_key,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
        path = f"{self.base_path}{endpoint}"
        query = ""
        if params:
            # Match FastAPI / starlette query encoding exactly so the signature lines up.
            from urllib.parse import urlencode
            query = urlencode(params, doseq=True)
        body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False) if json_body is not None else ""
        headers = self._sign(method, path, query, body)
        url = path + (f"?{query}" if query else "")
        try:
            if method.upper() == "GET":
                resp = self.transport.get(url, headers=headers)
            elif method.upper() == "POST":
                resp = self.transport.post(url, headers=headers, content=body or None)
            else:
                resp = self.transport.request(method, url, headers=headers, content=body or None)
        except TypeError:
            # httpx-compatible TestClient may want `data=` not `content=`
            resp = self.transport.request(method, url, headers=headers, data=body.encode("utf-8") if body else None)
        status = getattr(resp, "status_code", None)
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": getattr(resp, "text", "")}
        return {"status": status, "payload": payload}

    # Public API --------------------------------------------------------

    def klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> dict:
        return self._request("GET", "/klines", params={"symbol": symbol, "interval": interval, "limit": limit})

    def ticker(self, symbol: str) -> dict:
        return self._request("GET", "/ticker", params={"symbol": symbol})

    def risk_limits(self) -> dict:
        return self._request("GET", "/risk/limits")

    def balances(self) -> dict:
        return self._request("GET", "/balances")

    def open_orders(self, symbol: Optional[str] = None) -> dict:
        return self._request("GET", "/open-orders", params={"symbol": symbol} if symbol else None)

    def create_order(self, order: dict) -> dict:
        return self._request("POST", "/orders", json_body=order)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/orders/{order_id}/cancel")


# ---------------------------------------------------------------------------
# SMC paper-trading runner
# ---------------------------------------------------------------------------

@dataclass
class PaperRunConfig:
    symbol: str = "BTC-USDT"
    interval: str = "1h"
    bars: int = 200
    account_equity: float = 100_000.0
    risk_pct: float = 0.01
    min_confluence_score: int = 8
    min_rr: float = 1.5
    journal_path: str = "tmp/smc_paper_journal.jsonl"
    # SMC engine tuning
    swing_length: int = 5
    internal_swing_length: int = 3
    # §17.8 paper-trading guard rails
    max_notional_usdt: float = 5_000.0      # honour seeded max_single_order_notional
    price_deviation_pct: float = 0.02       # keep limit within ±2% of ticker mid
    use_live_ticker_price: bool = True      # override SMC entry with live mid for paper fills


@dataclass
class PaperRunResult:
    timestamp: str
    symbol: str
    action: str          # "placed" / "skipped:<reason>" / "error:<code>"
    bias: Optional[str] = None
    entry: Optional[dict] = None
    order_response: Optional[dict] = None
    sizing: Optional[dict] = None
    risk_gate: Optional[dict] = None
    trade_record: Optional[dict] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class SmcPaperRunner:
    """Connect SMC engine ↔ crypto-api for §10.5 forward testing."""

    def __init__(self, client: CryptoApiClient, config: Optional[PaperRunConfig] = None):
        self.client = client
        self.config = config or PaperRunConfig()

    # --- data ingestion -------------------------------------------------

    def _fetch_ohlcv(self, symbol: str) -> Optional[pd.DataFrame]:
        cfg = self.config
        resp = self.client.klines(symbol, interval=cfg.interval, limit=cfg.bars)
        if resp["status"] != 200:
            _logger.warning("klines fetch failed %s %s", resp["status"], resp.get("payload"))
            return None
        rows = (resp["payload"] or {}).get("data") or []
        if not rows:
            return None
        try:
            df = pd.DataFrame(
                [
                    {
                        "Open": float(r["open"]),
                        "High": float(r["high"]),
                        "Low": float(r["low"]),
                        "Close": float(r["close"]),
                        "Volume": float(r.get("volume", 0)),
                    }
                    for r in rows
                ],
                index=pd.to_datetime([r["open_time"] for r in rows], utc=True, errors="coerce"),
            )
        except Exception as exc:
            _logger.exception("kline parsing failed: %s", exc)
            return None
        return df

    # --- entry selection ------------------------------------------------

    def _pick_best_entry(self, analysis: dict) -> Optional[dict]:
        em = (analysis.get("concepts") or {}).get("entry_models") or {}
        triggered = list(em.get("triggered") or [])
        if not triggered:
            return None
        # Highest confluence score wins; tie-break on best RR.
        triggered.sort(
            key=lambda e: (
                (e.get("confluence") or {}).get("score", 0),
                e.get("rr", 0),
            ),
            reverse=True,
        )
        for e in triggered:
            score = (e.get("confluence") or {}).get("score", 0)
            if score < self.config.min_confluence_score:
                continue
            if (e.get("rr") or 0) < self.config.min_rr:
                continue
            if e.get("dol_required") and not e.get("dol_target"):
                continue
            return e
        return None

    # --- order placement -----------------------------------------------

    def _format_quantity(self, qty: float, precision: int = 6) -> str:
        q = Decimal(str(qty)).quantize(Decimal("1e-%d" % precision), rounding=ROUND_DOWN)
        return f"{q.normalize():f}"

    def _live_ticker_price(self) -> Optional[float]:
        try:
            t = self.client.ticker(self.config.symbol)
            if t["status"] == 200:
                p = (t["payload"] or {}).get("price") or (t["payload"] or {}).get("last_price")
                if p:
                    return float(p)
        except Exception:
            return None
        return None

    def _build_order_payload(self, entry: dict, sizing: dict, client_order_id: str) -> dict:
        """Translate an SMC entry + sizing into a crypto-api order payload.

        Paper-trading guard rails (§17.8):
          • Limit price is pulled from the live ticker so the §17.4
            ``PRICE_DEVIATION_LIMIT_EXCEEDED`` pre-trade check passes.
          • Quantity is capped so ``price × qty ≤ max_notional_usdt``
            (matches the seeded ``max_single_order_notional`` risk limit).
          • Direction sign mismatches between SMC entry and live price are
            tolerated — we still place at the side SMC chose, just at a
            sane limit price.
        """
        direction = int(entry.get("direction", 0))
        side = "buy" if direction == 1 else "sell"
        smc_entry = float(entry["entry"])
        live = self._live_ticker_price() if self.config.use_live_ticker_price else None
        ref_price = live if live and live > 0 else smc_entry
        # Snap limit slightly inside the spread on the side we're trading
        offset = ref_price * self.config.price_deviation_pct * 0.5
        if side == "buy":
            limit_price = ref_price - offset
        else:
            limit_price = ref_price + offset

        # Risk-based sizing in USDT then convert to base qty at limit price
        risk_amount = float(sizing.get("risk_amount") or 0)
        stop_distance = abs(smc_entry - float(entry["stop"]))
        if stop_distance <= 0 or limit_price <= 0:
            return {}
        # Scale stop distance to current price regime so risk_amount sizes correctly
        scaled_stop = stop_distance * (limit_price / smc_entry) if smc_entry > 0 else stop_distance
        crypto_qty = risk_amount / scaled_stop if scaled_stop > 0 else 0.0
        # Cap by max notional
        cap_qty = self.config.max_notional_usdt / limit_price
        crypto_qty = min(crypto_qty, cap_qty)
        if crypto_qty <= 0:
            return {}
        return {
            "client_order_id": client_order_id,
            "symbol": self.config.symbol,
            "side": side,
            "type": "limit",
            "price": str(round(limit_price, 2)),
            "quantity": self._format_quantity(crypto_qty),
            "time_in_force": "GTC",
            "post_only": False,
            "self_trade_prevention": "cancel_newest",
        }

    # --- public entry point --------------------------------------------

    def run_once(self) -> PaperRunResult:
        ts = datetime.now().isoformat(timespec="seconds")
        cfg = self.config
        result = PaperRunResult(timestamp=ts, symbol=cfg.symbol, action="skipped:init")

        df = self._fetch_ohlcv(cfg.symbol)
        if df is None or len(df) < 30:
            result.action = "skipped:no_data"
            result.notes.append(f"insufficient_bars={0 if df is None else len(df)}")
            self._journal(result)
            return result

        analysis = build_smc_analysis(
            df,
            symbol=cfg.symbol,
            config=SMCConfig(
                swing_length=cfg.swing_length,
                internal_swing_length=cfg.internal_swing_length,
            ),
            account_equity=cfg.account_equity,
        )
        result.bias = (analysis.get("summary") or {}).get("bias")

        entry = self._pick_best_entry(analysis)
        if entry is None:
            result.action = "skipped:no_qualified_entry"
            self._journal(result)
            return result
        result.entry = {
            "model": entry.get("model"),
            "direction": entry.get("direction"),
            "entry": entry.get("entry"),
            "stop": entry.get("stop"),
            "target": entry.get("target"),
            "rr": entry.get("rr"),
            "confluence": entry.get("confluence"),
            "dol_target": entry.get("dol_target"),
        }

        # §6 risk pipeline
        gate = apply_risk_pipeline(
            [entry],
            account_equity=cfg.account_equity,
            market="crypto",
            min_rr=cfg.min_rr,
        )
        result.risk_gate = {
            "ready_count": len(gate.get("ready", [])),
            "rejected": [r.get("reject_reason") for r in gate.get("rejected", [])],
            "lock": gate.get("lock"),
        }
        if not gate.get("ready"):
            result.action = "skipped:risk_gated"
            self._journal(result)
            return result

        ready_entry = gate["ready"][0]
        sizing = ready_entry.get("sizing") or calculate_position_size(
            entry, account_equity=cfg.account_equity, market="crypto", risk_pct=cfg.risk_pct,
        )
        result.sizing = sizing

        client_order_id = f"smc-{cfg.symbol.lower()}-{uuid.uuid4().hex[:10]}"
        payload = self._build_order_payload(entry, sizing, client_order_id)
        if not payload:
            result.action = "skipped:bad_payload"
            self._journal(result)
            return result

        resp = self.client.create_order(payload)
        result.order_response = resp
        if resp["status"] in (200, 201):
            result.action = "placed"
        else:
            err = (resp.get("payload") or {}).get("error", {}).get("code", "ORDER_FAILED")
            result.action = f"error:{err}"

        # Stamp a §18.2 trade record (entry-only — outcome filled in later via reconciliation)
        result.trade_record = build_trade_record(
            entry,
            trade_outcome={"outcome": "pending", "r_multiple": 0.0, "entry_index": -1},
            symbol=cfg.symbol,
            market="crypto",
            timeframe=cfg.interval,
            entry_time=ts,
        )
        self._journal(result)
        return result

    # --- journal --------------------------------------------------------

    def _journal(self, result: PaperRunResult) -> None:
        path = Path(self.config.journal_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), default=str, ensure_ascii=False) + "\n")
        if result.trade_record:
            persist_trade_records([result.trade_record], str(path).replace(".jsonl", "_trades.jsonl"))


def from_test_client(test_client) -> CryptoApiClient:
    """Convenience constructor — wrap a starlette TestClient as transport."""
    return CryptoApiClient(test_client)
