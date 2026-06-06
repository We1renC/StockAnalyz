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
    LedgerPaths,
    SMCConfig,
    apply_risk_pipeline,
    build_smc_analysis,
    build_trade_record,
    calculate_position_size,
    load_runtime_cluster_weight_table,
    persist_trade_records,
)

_logger = logging.getLogger(__name__)

# Round K (F2): structured swallow accounting for the learning-integration
# steps so "learning didn't affect this decision" is observable, not silent.
try:
    from learning.obs_log import get_logger as _get_obs_logger, swallow as _obs_swallow
    _obs_log = _get_obs_logger(__name__)
except Exception:  # pragma: no cover - obs_log always present
    from contextlib import contextmanager as _cm
    _obs_log = None
    @_cm
    def _obs_swallow(_logger, _ctx, **_kw):
        try:
            yield
        except Exception:
            pass


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
    journal_path: str = field(default_factory=LedgerPaths.paper_journal)
    # SMC engine tuning
    swing_length: int = 5
    internal_swing_length: int = 3
    # §17.8 paper-trading guard rails
    max_notional_usdt: float = 5_000.0      # honour seeded max_single_order_notional
    price_deviation_pct: float = 0.02       # keep limit within ±2% of ticker mid
    use_live_ticker_price: bool = True      # override SMC entry with live mid for paper fills
    probe: bool = False



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
        """Pick the strongest entry candidate.

        If ``config.min_confluence_score`` is BELOW the SMC engine's own
        ``confluence.threshold`` we still consider non-``triggered``
        candidates — this is the explicit "lower the bar for paper
        testing" knob. Production should keep ``min_confluence_score``
        ≥ the engine's threshold (default 8) so only fully-qualified
        signals reach order placement.
        """
        em = (analysis.get("concepts") or {}).get("entry_models") or {}
        # Gather every model's candidates so we can use min_confluence_score
        # as the *effective* gate rather than the engine's default 8.
        all_entries: list[dict] = []
        for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement",
                    "unicorn", "silver_bullet", "power_of_three"):
            for e in (em.get(key) or []):
                all_entries.append(e)
        if not all_entries:
            return None
        all_entries.sort(
            key=lambda e: (
                (e.get("confluence") or {}).get("score", 0),
                e.get("rr", 0),
            ),
            reverse=True,
        )
        for e in all_entries:
            score = (e.get("confluence") or {}).get("score", 0)
            if score < self.config.min_confluence_score:
                continue
            if (e.get("rr") or 0) < self.config.min_rr:
                continue
            if e.get("dol_required") and not e.get("dol_target"):
                continue
            # The engine's own ``triggered`` flag is gated by its default
            # threshold (8). When the runner deliberately accepts lower
            # scores (paper-testing knob), we mirror that decision onto
            # the entry's flag so apply_risk_pipeline's
            # ``confluence_below_threshold`` check doesn't reject again.
            picked = dict(e)
            picked["triggered"] = True
            # P2-12 audit fix: rewrite stop/target from per-(model, direction)
            # MAE/MFE calibration if we have enough history. The original
            # 5%-buffer-and-2R-fallback values become the fallback when
            # calibration data is missing.
            self._apply_mae_mfe_calibration(picked)
            return picked

        # P2-14+ audit fix: NO standard candidate met the threshold. Try
        # an ε-greedy boundary probe — only fires in READY state and pulls
        # a candidate from [min_score - 2, min_score) at 20% normal size.
        # If activated, the entry is tagged is_exploration=True so
        # attribution can isolate exploration P&L.
        return self._try_exploration_probe(all_entries)

    def _try_exploration_probe(self, all_entries: list[dict]) -> Optional[dict]:
        """P2-14+ ε-greedy: occasionally fire a sub-threshold boundary entry."""
        try:
            from learning.exploration import (
                decide_exploration, count_exploration_trades,
            )
            from smc_quant import read_trade_ledger
            # Only fire if state is READY (caller passes via _last_state cache)
            state = getattr(self, "_last_state_hint", None) or "READY"
            try:
                all_recs = read_trade_ledger(
                    LedgerPaths.training_ledger(),
                    symbol=self.config.symbol,
                )
            except Exception:
                all_recs = []
            boundary_n = count_exploration_trades(all_recs, symbol=self.config.symbol)
            decision = decide_exploration(
                all_entries=all_entries,
                min_confluence_score=self.config.min_confluence_score,
                state=state,
                symbol=self.config.symbol,
                boundary_sample_count=boundary_n,
            )
            if not decision.is_exploration:
                return None
            picked = decision.chosen_entry
            if picked is None:
                return None
            picked["triggered"] = True
            picked["source"] = "exploration"
            # Apply MAE/MFE calibration to exploration probe too — keeps
            # stop/target ratios consistent with normal trades.
            self._apply_mae_mfe_calibration(picked)
            return picked
        except Exception:
            return None

    def _build_mae_mfe_table(self) -> dict:
        """Audit fix P2-12+: build per-(model, direction) calibration table.

        Returns {} if no ledger / insufficient samples. Cached on the runner
        so repeat ``run_once`` calls re-use the table; cleared once a new
        trade resolves (see ``_invalidate_mae_mfe_cache``).
        """
        try:
            from learning.mae_mfe_calibration import build_model_calibration_table
            from smc_quant import read_trade_ledger
            if not hasattr(self, "_mae_mfe_cal_cache"):
                ledger_path = getattr(self.config, "journal_path",
                                       LedgerPaths.paper_journal())
                trade_ledger = ledger_path.replace(".jsonl", "_trades.jsonl")
                records = read_trade_ledger(trade_ledger, copy_records=True)
                try:
                    records.extend(
                        read_trade_ledger(
                            LedgerPaths.training_ledger(),
                            symbol=self.config.symbol,
                        )
                    )
                except Exception:
                    pass
                self._mae_mfe_cal_cache = build_model_calibration_table(records) or {}
            return self._mae_mfe_cal_cache or {}
        except Exception:
            return {}

    def _apply_mae_mfe_calibration(self, entry: dict) -> None:
        """Belt-and-suspenders: in case caller bypassed build_smc_analysis
        injection, still calibrate on the picked entry.

        Idempotent — apply_calibration_to_entry checks ``original_stop``
        marker via ``calibration_applied`` annotation.
        """
        if entry.get("calibration_applied"):
            return
        try:
            from learning.mae_mfe_calibration import apply_calibration_to_entry
            table = self._build_mae_mfe_table()
            if table:
                apply_calibration_to_entry(entry, table)
        except Exception:
            entry.setdefault("calibration_applied", None)

    def _invalidate_mae_mfe_cache(self) -> None:
        """Drop cached calibration so the next ``run_once`` rebuilds from
        the freshest ledger. Called after a trade resolves."""
        if hasattr(self, "_mae_mfe_cal_cache"):
            try:
                delattr(self, "_mae_mfe_cal_cache")
            except AttributeError:
                pass

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
        # P2-14+: exploration entries take 20% size by design — caps
        # exploration risk budget regardless of the configured risk_pct.
        exp_mult = entry.get("exploration_size_multiplier")
        if exp_mult is not None:
            try:
                crypto_qty *= float(exp_mult)
            except (TypeError, ValueError):
                pass
        # Cap by max notional
        cap_qty = self.config.max_notional_usdt / limit_price
        if exp_mult is not None:
            # Also halve notional cap for exploration so it never blows
            # the risk budget even if scaling math drifted.
            try:
                cap_qty *= float(exp_mult)
            except (TypeError, ValueError):
                pass
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

        # Audit fix P2-12+: build MAE/MFE calibration once and feed it into
        # the analysis so candidate RR (used by _pick_best_entry) reflects
        # calibrated stop/target.
        cal_table = self._build_mae_mfe_table()
        cluster_weight_table = load_runtime_cluster_weight_table(
            LedgerPaths.training_ledger()
        )
        analysis = build_smc_analysis(
            df,
            symbol=cfg.symbol,
            timeframe=cfg.interval,
            config=SMCConfig(
                swing_length=cfg.swing_length,
                internal_swing_length=cfg.internal_swing_length,
            ),
            account_equity=cfg.account_equity,
            mae_mfe_calibration=cal_table,
            cluster_weight_table=cluster_weight_table,
            cluster_key_hint=("runtime", cfg.symbol, cfg.interval, None),
        )
        result.bias = (analysis.get("summary") or {}).get("bias")

        # Audit fix D3: apply per-(model, symbol, interval) decommission
        # state so dead detectors don't even compete for the entry slot.
        with _obs_swallow(_obs_log, "apply_decommission"):
            from learning.model_decommission import (
                apply_decommission_to_analysis, load_state,
            )
            import os as _os
            decom_path = _os.path.join(
                _os.path.dirname(LedgerPaths.training_ledger()),
                "decommissioned.json",
            )
            state = load_state(decom_path)
            if state:
                apply_decommission_to_analysis(
                    analysis, state,
                    symbol=cfg.symbol, interval=cfg.interval,
                )

        entry = self._pick_best_entry(analysis)
        # Audit fix D4: cross-model ensemble vote — if both sides have
        # qualified candidates, scale size down by confidence.
        if entry is not None:
            with _obs_swallow(_obs_log, "ensemble_vote"):
                from learning.ensemble_vote import annotate_picked_entry_with_vote
                annotate_picked_entry_with_vote(entry, analysis)
        if entry is None:
            result.action = "skipped:no_qualified_entry"
            # P2-15 audit fix: record the BEST candidate (even if below threshold)
            # as a missed_signal so we can later attribute opportunity cost.
            try:
                em = (analysis.get("concepts") or {}).get("entry_models") or {}
                best = None
                best_score = -1
                for key in ("sweep_reversal", "ob_fvg_continuation", "ote_retracement",
                            "unicorn", "silver_bullet", "power_of_three"):
                    for e in (em.get(key) or []):
                        s = (e.get("confluence") or {}).get("score", 0) or 0
                        if s > best_score:
                            best_score = s; best = e
                if best is not None:
                    self._log_missed_signal(best, result.bias, reason="below_threshold")
            except Exception:
                pass
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

        # §6 risk pipeline — propagate the runner's risk_pct so crypto sizing
        # can use a more generous % (math.floor in calculate_position_size
        # zeroes-out tiny notionals at 1% on high-priced assets like BTC).
        gate = apply_risk_pipeline(
            [entry],
            account_equity=cfg.account_equity,
            market="crypto",
            min_rr=cfg.min_rr,
            risk_pct=cfg.risk_pct,
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
        order_resp_payload = (resp.get("payload") or {}) if isinstance(resp, dict) else {}
        order_id = order_resp_payload.get("id") if isinstance(order_resp_payload, dict) else None
        result.trade_record = build_trade_record(
            entry,
            trade_outcome={"outcome": "pending", "r_multiple": 0.0, "entry_index": -1},
            symbol=cfg.symbol,
            market="crypto",
            timeframe=cfg.interval,
            entry_time=ts,
            # P0-2: identity fields so reconcile_paper_trades can match later
            trade_id=f"{cfg.symbol}:{client_order_id}",
        )
        if getattr(cfg, "probe", False):
            result.trade_record["probe"] = 1
        else:
            result.trade_record["probe"] = 0
        # Attach broker identifiers + planning prices for reconciliation
        result.trade_record["broker_order_id"] = order_id
        result.trade_record["client_order_id"] = client_order_id
        result.trade_record["plan_stop"] = float(entry.get("stop") or 0)
        result.trade_record["plan_target"] = float(entry.get("target") or 0)
        result.trade_record["plan_entry"] = float(entry.get("entry") or 0)
        # Audit fix B3 + B4: stamp source / interval / regime so
        # live_vs_backtest_correlation gate and cluster_ensemble can bucket.
        result.trade_record["source"] = "paper"
        result.trade_record["interval"] = cfg.interval
        try:
            from smc_quant import classify_asset_volatility
            df_for_regime = analysis.get("_h") if isinstance(analysis, dict) else None
            if df_for_regime is not None:
                result.trade_record["regime"] = (
                    classify_asset_volatility(df_for_regime) or {}
                ).get("bucket") or "unknown"
            else:
                ai = analysis.get("adaptive_info") if isinstance(analysis, dict) else None
                result.trade_record["regime"] = (ai or {}).get("bucket") or "unknown"
        except Exception:
            result.trade_record["regime"] = "unknown"
        self._journal(result)
        return result

    # --- journal --------------------------------------------------------

    def _log_missed_signal(self, entry: dict, bias: Optional[str], *, reason: str) -> None:
        """P2-15: record signals that the runner saw but did NOT execute.

        Output goes to ``missed_signals_<symbol>.jsonl`` next to the journal.
        A later cron job can compare entry/stop/target against actual price
        movement N bars later to quantify opportunity cost — the missing
        feedback signal that explains "守紀律拒單" outcomes.
        """
        try:
            cfg_path = Path(self.config.journal_path)
            base = cfg_path.parent / f"missed_signals_{self.config.symbol.replace('/', '-')}.jsonl"
            base.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "logged_at": datetime.now().isoformat(timespec="seconds"),
                "symbol": self.config.symbol,
                "interval": self.config.interval,
                "bias": bias,
                "reason": reason,
                "model": entry.get("model"),
                "direction": entry.get("direction"),
                "score": (entry.get("confluence") or {}).get("score"),
                "threshold": (entry.get("confluence") or {}).get("threshold"),
                "min_score_runner": self.config.min_confluence_score,
                "rr": entry.get("rr"),
                "entry": entry.get("entry"),
                "stop": entry.get("stop"),
                "target": entry.get("target"),
                "dol_target": entry.get("dol_target"),
                "outcome_at_5_bars": None,    # filled by reconcile_missed_signals later
                "outcome_at_20_bars": None,
                "max_favorable_R": None,
                "max_adverse_R": None,
            }
            # Audit fix A1: lock around append to prevent interleave with
            # reconciler / concurrent runner.
            from learning.file_lock import locked_append
            with locked_append(base):
                with open(base, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _journal(self, result: PaperRunResult) -> None:
        path = Path(self.config.journal_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Audit fix A1: lock around append.
        from learning.file_lock import locked_append
        with locked_append(str(path)):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(result.to_dict(), default=str, ensure_ascii=False) + "\n")
        if result.trade_record:
            persist_trade_records([result.trade_record], str(path).replace(".jsonl", "_trades.jsonl"))


def from_test_client(test_client) -> CryptoApiClient:
    """Convenience constructor — wrap a starlette TestClient as transport."""
    return CryptoApiClient(test_client)
