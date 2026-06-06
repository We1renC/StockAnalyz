"""Unified trading system — SMC strategy × crypto-api execution × paper acceptance.

Bundles the three branches that landed on codex/smc-quant-system-v2:

  • codex/smc-quant-system-v2  → SMC strategy engine (build_smc_analysis,
    six entry models, §6 risk pipeline, §18 trade ledger, …)
  • codex/crypto-trading-api   → Binance-Spot-compatible mock matching
    engine with HMAC auth, pre-trade risk gates, kill switch
  • codex/quant-paper-acceptance → 21-gate paper-trading acceptance
    standard with SQLite-persisted reports

The unified pipeline:

  propose_signals(symbols)          §3–§17 SMC analysis → ranked candidates
        ↓
  dry_run_signals(signals)          paper_execution.simulate_market_order →
                                    slippage + fee + fill-state per trade
                                    (zero side-effects, perfect for cron)
        ↓
  live_paper_session(signals)       Signed POST /v1/orders → crypto-api
                                    matching engine; mirror fills back
        ↓
  build_acceptance_report(trades)   paper_acceptance.build_acceptance_report
                                    → run gates, derive conclusion
        ↓
  persist_acceptance_report()       paper_acceptance_store → SQLite,
                                    timeline events, gate evidence cache

``UnifiedTradingSession.run()`` is the cron-friendly one-shot entry: it
executes the four phases above and returns the consolidated report.
"""

from __future__ import annotations

import sqlite3
import uuid
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from smc_quant import (
    SMCConfig,
    build_smc_analysis,
    build_trade_record,
    load_runtime_cluster_weight_table,
)
from smc_paper_runner import CryptoApiClient, SmcPaperRunner, PaperRunConfig
from paper_execution import PaperOrderIntent, simulate_market_order
from paper_acceptance import build_acceptance_report, render_acceptance_markdown
from paper_acceptance_store import (
    ensure_paper_acceptance_schema,
    persist_acceptance_report,
    record_acceptance_event,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class UnifiedSessionConfig:
    """All knobs for one unified-system run."""

    symbols: list[str] = field(default_factory=lambda: ["BTC-USDT"])
    interval: str = "15m"
    bars: int = 500
    swing_length: int = 4
    internal_swing_length: int = 2
    # Strategy gating
    min_confluence_score: int = 8
    min_rr: float = 1.5
    # Position sizing
    account_equity: float = 100_000.0
    risk_pct: float = 0.02
    max_notional_usdt: float = 5_000.0
    price_deviation_pct: float = 0.02
    # Acceptance metadata
    strategy_id: str = "smc.v2.default"
    strategy_version: str = "v2.0"
    parameter_version: str = "p2026.06"
    stage: str = "paper"
    # Persistence
    journal_dir: str = "tmp/smc_unified"
    paper_db_path: Optional[str] = None      # if None → in-memory
    probe: bool = False



@dataclass
class SymbolDecision:
    """Per-symbol outcome of one unified-session run."""

    symbol: str
    action: str
    bias: Optional[str] = None
    entry: Optional[dict] = None
    sizing: Optional[dict] = None
    dry_run: Optional[dict] = None        # paper_execution.simulate_* result
    live_order: Optional[dict] = None     # crypto-api response
    trade_record: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified session
# ---------------------------------------------------------------------------

class UnifiedTradingSession:
    """One-shot orchestrator across the three branches."""

    def __init__(self, api: CryptoApiClient, config: Optional[UnifiedSessionConfig] = None):
        self.api = api
        self.config = config or UnifiedSessionConfig()
        Path(self.config.journal_dir).mkdir(parents=True, exist_ok=True)
        if self.config.paper_db_path:
            self.conn: sqlite3.Connection = sqlite3.connect(self.config.paper_db_path)
        else:
            self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_paper_acceptance_schema(self.conn)

    # --- Phase 1: propose signals --------------------------------------

    def propose_signals(self) -> list[SymbolDecision]:
        """Run §3–§17 SMC engine across every configured symbol."""
        cfg = self.config
        decisions: list[SymbolDecision] = []
        cluster_weight_table = load_runtime_cluster_weight_table()
        for sym in cfg.symbols:
            runner = SmcPaperRunner(
                self.api,
                PaperRunConfig(
                    symbol=sym, interval=cfg.interval, bars=cfg.bars,
                    account_equity=cfg.account_equity,
                    risk_pct=cfg.risk_pct,
                    min_confluence_score=cfg.min_confluence_score,
                    min_rr=cfg.min_rr,
                    max_notional_usdt=cfg.max_notional_usdt,
                    price_deviation_pct=cfg.price_deviation_pct,
                    swing_length=cfg.swing_length,
                    internal_swing_length=cfg.internal_swing_length,
                    journal_path=str(Path(cfg.journal_dir) / f"{sym}.jsonl"),
                ),
            )
            df = runner._fetch_ohlcv(sym)
            if df is None or len(df) < 30:
                decisions.append(SymbolDecision(symbol=sym, action="skipped:no_data"))
                continue
            analysis = build_smc_analysis(
                df, symbol=sym,
                timeframe=cfg.interval,
                config=SMCConfig(
                    swing_length=cfg.swing_length,
                    internal_swing_length=cfg.internal_swing_length,
                ),
                account_equity=cfg.account_equity,
                cluster_weight_table=cluster_weight_table,
                cluster_key_hint=("runtime", sym, cfg.interval, None),
            )
            bias = (analysis.get("summary") or {}).get("bias")
            entry = runner._pick_best_entry(analysis)
            if entry is None:
                decisions.append(SymbolDecision(symbol=sym, action="skipped:no_entry", bias=bias))
                continue
            decisions.append(SymbolDecision(
                symbol=sym, action="proposed", bias=bias,
                entry={
                    "model": entry.get("model"),
                    "direction": entry.get("direction"),
                    "entry": entry.get("entry"),
                    "stop": entry.get("stop"),
                    "target": entry.get("target"),
                    "rr": entry.get("rr"),
                    "confluence": entry.get("confluence"),
                    "factors": entry.get("factors"),
                    "dol_target": entry.get("dol_target"),
                    "_raw": entry,
                },
            ))
        return decisions

    # --- Phase 2: dry-run via paper_execution ---------------------------

    def dry_run_signals(self, decisions: list[SymbolDecision]) -> list[SymbolDecision]:
        """Simulate fills via paper_execution.simulate_market_order.

        Builds a synthetic 1-level order book from the live ticker so we
        get realistic slippage + fee + partial-fill data without touching
        the real matching engine. Useful for §10.5 dry-run journaling.
        """
        cfg = self.config
        for dec in decisions:
            if dec.action != "proposed" or not dec.entry:
                continue
            ticker = self.api.ticker(dec.symbol)
            ref_price = None
            if ticker["status"] == 200:
                p = (ticker["payload"] or {}).get("price") or (ticker["payload"] or {}).get("last_price")
                if p:
                    try:
                        ref_price = float(p)
                    except (TypeError, ValueError):
                        ref_price = None
            if ref_price is None:
                ref_price = float(dec.entry["entry"])
            side = "buy" if dec.entry["direction"] == 1 else "sell"
            qty = max(1e-6, cfg.max_notional_usdt / ref_price)
            # Synthetic 1-level book just above/below the ref price
            spread = ref_price * 0.0005
            order_book = {
                "asks": [(ref_price + spread, qty * 1.2)],
                "bids": [(ref_price - spread, qty * 1.2)],
            }
            intent = PaperOrderIntent(
                symbol=dec.symbol, side=side, quantity=qty,
                order_type="market", signal_price=float(dec.entry["entry"]),
                strategy_version=cfg.strategy_version,
                parameter_version=cfg.parameter_version,
                signal_source=dec.entry.get("model", "smc"),
                client_order_id=f"smc-dry-{uuid.uuid4().hex[:8]}",
            )
            # P2-13 audit fix: use empirical slippage from past fills instead
            # of the fixed 0.05% impact baked into simulate_market_order's
            # default. ``_slippage_sampler`` is built once per session and
            # falls back to 5 bps when there isn't enough fill history.
            slip_bps = self._slippage_sampler_for(dec.symbol)(dec.symbol, side)
            result = simulate_market_order(
                intent, order_book, fee_rate=0.001,
                liquidity_impact_bps=slip_bps,
            )
            if isinstance(result, dict):
                result["empirical_slippage_bps_used"] = slip_bps
            dec.dry_run = result
        return decisions

    def _slippage_sampler_for(self, symbol: str):
        """Build a per-session slippage sampler from real fills (P2-13)."""
        if not hasattr(self, "_slippage_sampler_cache"):
            try:
                from learning.slippage_model import build_runtime_sampler
                self._slippage_sampler_cache = build_runtime_sampler(self.api)
            except Exception:
                self._slippage_sampler_cache = lambda _s, _side: 5.0
        return self._slippage_sampler_cache

    # --- Phase 3: live POST to crypto-api -------------------------------

    def live_paper_session(self, decisions: list[SymbolDecision]) -> list[SymbolDecision]:
        cfg = self.config
        for dec in decisions:
            if dec.action != "proposed" or not dec.entry:
                continue
            runner = SmcPaperRunner(
                self.api,
                PaperRunConfig(
                    symbol=dec.symbol, interval=cfg.interval, bars=cfg.bars,
                    account_equity=cfg.account_equity,
                    risk_pct=cfg.risk_pct,
                    min_confluence_score=cfg.min_confluence_score,
                    min_rr=cfg.min_rr,
                    max_notional_usdt=cfg.max_notional_usdt,
                    price_deviation_pct=cfg.price_deviation_pct,
                    swing_length=cfg.swing_length,
                    internal_swing_length=cfg.internal_swing_length,
                    journal_path=str(Path(cfg.journal_dir) / f"{dec.symbol}_live.jsonl"),
                    probe=cfg.probe,
                ),
            )
            result = runner.run_once()
            dec.action = result.action
            dec.sizing = result.sizing
            dec.live_order = result.order_response
            dec.trade_record = result.trade_record
        return decisions

    # --- Phase 4: acceptance gates --------------------------------------

    def build_session_acceptance(self, decisions: list[SymbolDecision]) -> dict[str, Any]:
        """Roll all decisions into a paper-acceptance report context."""
        cfg = self.config
        trades = []
        for dec in decisions:
            if dec.dry_run and dec.dry_run.get("state") in {"filled", "partially_filled"}:
                # Treat the dry-run fill as a "paper trade" for acceptance stats.
                # r_multiple is unknown until close; use a neutral placeholder.
                qty = float(dec.dry_run.get("filled_qty") or 0)
                slip = dec.dry_run.get("slippage_bps") or 0
                trades.append({
                    "symbol": dec.symbol,
                    "model": (dec.entry or {}).get("model"),
                    "direction": (dec.entry or {}).get("direction"),
                    "entry_price": dec.dry_run.get("avg_price"),
                    "qty": qty,
                    "fee": dec.dry_run.get("fee"),
                    "slippage": slip / 10_000 if slip is not None else 0,
                    "r_multiple": 0.0,        # paper-acceptance treats 0 as un-realised
                    "pnl": 0.0,
                    "state": dec.dry_run.get("state"),
                    "live_order_id": (dec.live_order or {}).get("payload", {}).get("id") if dec.live_order else None,
                })
        context = {
            "strategy": {
                "id": cfg.strategy_id,
                "version": cfg.strategy_version,
                "parameter_version": cfg.parameter_version,
                "stage": cfg.stage,
                "symbols": cfg.symbols,
                "interval": cfg.interval,
            },
            "trades": trades,
            "stage": cfg.stage,
            "evidence": {
                # Surface the runtime context so gate evaluators can read it
                "smc_decision_summary": {
                    "total_symbols": len(cfg.symbols),
                    "proposed_count": sum(1 for d in decisions if d.entry),
                    "placed_count": sum(1 for d in decisions if d.action == "placed"),
                    "skipped_reasons": [d.action for d in decisions if d.action.startswith("skipped")],
                },
            },
        }
        report = build_acceptance_report(context)
        markdown = render_acceptance_markdown(report)
        return {"context": context, "report": report, "markdown": markdown}

    def persist(self, packet: dict[str, Any]) -> str:
        run_key = persist_acceptance_report(self.conn, packet["report"], markdown=packet["markdown"])
        record_acceptance_event(
            self.conn,
            event_type="session.complete",
            run_key=run_key,
            symbol=",".join(self.config.symbols),
            severity="info",
            status="closed",
            detail={"summary": packet["report"]["summary"]},
        )
        return run_key

    # --- Top-level entry ------------------------------------------------

    def run(self, *, place_live_orders: bool = True) -> dict[str, Any]:
        """Run the four-phase pipeline once and return the full snapshot."""
        t0 = time.time()
        decisions = self.propose_signals()
        decisions = self.dry_run_signals(decisions)
        if place_live_orders:
            decisions = self.live_paper_session(decisions)
        packet = self.build_session_acceptance(decisions)
        try:
            run_id = self.persist(packet)
        except Exception as exc:
            run_id = None
            packet["persist_error"] = repr(exc)

        # Drive the merged paper-acceptance telemetry primitives so the
        # gates have real evidence to evaluate (otherwise everything
        # fails for lack of data).
        evidence_counts = {}
        try:
            from smc_training_loop import ingest_acceptance_evidence
            evidence_counts = {}
            session_snapshot = {
                "acceptance": {
                    "metrics": packet["report"].get("metrics") or {},
                    "blocking_issues": packet["report"].get("blocking_issues") or [],
                },
                "decisions": [asdict(d) for d in decisions],
            }
            for sym in self.config.symbols:
                evidence_counts[sym] = ingest_acceptance_evidence(
                    self.conn, session_snapshot,
                    symbol=sym, stage=self.config.stage,
                )
        except Exception as exc:
            evidence_counts = {"error": repr(exc)}

        return {
            "elapsed_seconds": round(time.time() - t0, 3),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "decisions": [
                {
                    **{k: v for k, v in asdict(d).items() if k != "entry"},
                    "entry": {k: v for k, v in (d.entry or {}).items() if k != "_raw"} if d.entry else None,
                }
                for d in decisions
            ],
            "acceptance": {
                "run_id": run_id,
                "conclusion": packet["report"]["summary"]["conclusion"],
                "conclusion_label": packet["report"]["summary"]["conclusion_label"],
                "passed": packet["report"]["summary"]["passed"],
                "failed": packet["report"]["summary"]["failed"],
                "blocking_issues": packet["report"]["blocking_issues"],
                "metrics": packet["report"]["metrics"],
                "evidence_ingested": evidence_counts,
            },
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
