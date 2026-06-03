"""Event-driven backtesting utilities for the SMC engine.

The backtester intentionally rebuilds SMC state from the visible slice on every
decision bar. This is slower than vectorized testing, but it keeps the first
implementation lookahead-proof and easy to audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from smc_quant import SMCConfig, build_smc_analysis, infer_market, normalize_ohlcv


@dataclass(frozen=True)
class SMCBacktestConfig:
    min_bars: int = 30
    max_hold_bars: int = 20
    account_equity: float = 100_000
    risk_pct: float = 0.01
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    require_qualified: bool = True


def run_smc_event_backtest(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str = "1d",
    smc_config: Optional[SMCConfig] = None,
    backtest_config: Optional[SMCBacktestConfig] = None,
    weights: Optional[dict[str, int]] = None,
) -> dict:
    cfg = backtest_config or SMCBacktestConfig()
    h = normalize_ohlcv(df)
    if len(h) < cfg.min_bars + 2:
        return _empty_result(symbol, timeframe, len(h), "insufficient_history")

    trades: list[dict] = []
    equity_curve = [{"bar": 0, "time": h.index[0].isoformat(), "equity": round(cfg.account_equity, 2)}]
    equity = float(cfg.account_equity)
    open_trade: Optional[dict] = None
    i = max(cfg.min_bars, (smc_config or SMCConfig()).swing_length * 2 + 5)

    while i < len(h) - 1:
        if open_trade is None:
            visible = h.iloc[: i + 1]
            analysis = build_smc_analysis(
                visible,
                symbol=symbol,
                timeframe=timeframe,
                config=smc_config,
                weights=weights,
                account_equity=equity,
            )
            signal = _select_signal(analysis, require_qualified=cfg.require_qualified)
            if signal:
                entry_idx = i + 1
                entry_bar = h.iloc[entry_idx]
                open_trade = _open_trade(signal, symbol, timeframe, entry_idx, h.index[entry_idx], entry_bar, cfg)
                i = entry_idx
                continue
        else:
            exit_trade = _maybe_exit_trade(open_trade, h, i, cfg)
            if exit_trade:
                trade = exit_trade
                equity += trade["pnl"]
                trade["equity_after"] = round(equity, 2)
                trades.append(trade)
                equity_curve.append({"bar": i, "time": h.index[i].isoformat(), "equity": round(equity, 2)})
                open_trade = None
        i += 1

    if open_trade is not None:
        last_idx = len(h) - 1
        trade = _close_trade(open_trade, h, last_idx, "time_exit", cfg)
        equity += trade["pnl"]
        trade["equity_after"] = round(equity, 2)
        trades.append(trade)
        equity_curve.append({"bar": last_idx, "time": h.index[last_idx].isoformat(), "equity": round(equity, 2)})

    return {
        "symbol": symbol,
        "market": infer_market(symbol),
        "timeframe": timeframe,
        "bars": len(h),
        "trades": trades,
        "metrics": _metrics(trades, cfg.account_equity, equity_curve),
        "equity_curve": equity_curve,
        "lookahead_policy": "signals are computed from df.iloc[:i+1], entries occur on i+1",
        "config": cfg.__dict__,
    }


def trade_record_schema() -> dict:
    return {
        "identifiers": ["trade_id", "symbol", "market", "timeframe", "direction", "entry_time", "exit_time"],
        "features": [
            "model",
            "score",
            "threshold",
            "feature_vector",
            "entry_source",
            "dol_target",
            "rr_plan",
            "market",
        ],
        "execution": ["entry", "stop", "tp1", "tp2", "qty", "risk_amount", "fee", "slippage"],
        "outcomes": ["exit", "exit_reason", "pnl", "r_multiple", "win", "mae", "mfe", "holding_bars"],
    }


def _empty_result(symbol: str, timeframe: str, bars: int, reason: str) -> dict:
    return {
        "symbol": symbol,
        "market": infer_market(symbol),
        "timeframe": timeframe,
        "bars": bars,
        "trades": [],
        "metrics": {"total_trades": 0, "reason": reason},
        "equity_curve": [],
    }


def _select_signal(analysis: dict, require_qualified: bool) -> Optional[dict]:
    signals = analysis.get("signals") or []
    if not signals:
        return None
    if require_qualified:
        return next((s for s in signals if s.get("qualified")), None)
    return signals[0]


def _open_trade(
    signal: dict,
    symbol: str,
    timeframe: str,
    entry_idx: int,
    entry_time,
    entry_bar: pd.Series,
    cfg: SMCBacktestConfig,
) -> dict:
    direction = 1 if signal.get("direction") == "long" else -1
    raw_entry = float(entry_bar["open"])
    entry = raw_entry * (1 + cfg.slippage_pct * direction)
    sizing = (signal.get("risk") or {}).get("position_sizing") or {}
    qty = max(int(sizing.get("qty") or 0), 1)
    return {
        "trade_id": f"{symbol}-{entry_idx}-{signal.get('model')}",
        "symbol": symbol,
        "market": infer_market(symbol),
        "timeframe": timeframe,
        "model": signal.get("model"),
        "direction": signal.get("direction"),
        "direction_value": direction,
        "entry_index": entry_idx,
        "entry_time": pd.Timestamp(entry_time).isoformat(),
        "entry": round(entry, 4),
        "planned_entry": signal.get("entry"),
        "stop": float(signal["stop"]),
        "tp1": float(signal["tp1"]),
        "tp2": signal.get("tp2"),
        "qty": qty,
        "risk_amount": sizing.get("risk_amount"),
        "score": signal.get("score"),
        "threshold": signal.get("threshold"),
        "feature_vector": signal.get("feature_vector") or {},
        "entry_source": signal.get("entry_source"),
        "dol_target": signal.get("dol_target"),
        "rr_plan": signal.get("rr"),
        "mae": 0.0,
        "mfe": 0.0,
    }


def _maybe_exit_trade(trade: dict, h: pd.DataFrame, i: int, cfg: SMCBacktestConfig) -> Optional[dict]:
    row = h.iloc[i]
    direction = trade["direction_value"]
    low = float(row["low"])
    high = float(row["high"])
    if direction == 1:
        trade["mae"] = min(trade["mae"], low - trade["entry"])
        trade["mfe"] = max(trade["mfe"], high - trade["entry"])
        stop_hit = low <= trade["stop"]
        tp_hit = high >= trade["tp1"]
    else:
        trade["mae"] = min(trade["mae"], trade["entry"] - high)
        trade["mfe"] = max(trade["mfe"], trade["entry"] - low)
        stop_hit = high >= trade["stop"]
        tp_hit = low <= trade["tp1"]
    if stop_hit:
        return _close_trade(trade, h, i, "stop", cfg, exit_price=trade["stop"])
    if tp_hit:
        return _close_trade(trade, h, i, "tp1", cfg, exit_price=trade["tp1"])
    if i - trade["entry_index"] >= cfg.max_hold_bars:
        return _close_trade(trade, h, i, "time_exit", cfg)
    return None


def _close_trade(
    trade: dict,
    h: pd.DataFrame,
    exit_idx: int,
    reason: str,
    cfg: SMCBacktestConfig,
    exit_price: Optional[float] = None,
) -> dict:
    row = h.iloc[exit_idx]
    direction = trade["direction_value"]
    exit_px = float(exit_price if exit_price is not None else row["close"])
    exit_px = exit_px * (1 - cfg.slippage_pct * direction)
    gross = (exit_px - trade["entry"]) * direction * trade["qty"]
    fee = (abs(trade["entry"]) + abs(exit_px)) * trade["qty"] * cfg.fee_pct
    pnl = gross - fee
    unit_risk = abs(trade["entry"] - trade["stop"])
    r_multiple = ((exit_px - trade["entry"]) * direction / unit_risk) if unit_risk > 0 else 0
    out = dict(trade)
    out.update(
        {
            "exit_index": exit_idx,
            "exit_time": h.index[exit_idx].isoformat(),
            "exit": round(exit_px, 4),
            "exit_reason": reason,
            "holding_bars": exit_idx - trade["entry_index"],
            "fee": round(fee, 4),
            "pnl": round(pnl, 4),
            "r_multiple": round(r_multiple, 4),
            "win": pnl > 0,
            "mae": round(trade["mae"], 4),
            "mfe": round(trade["mfe"], 4),
        }
    )
    return out


def _metrics(trades: list[dict], initial_equity: float, equity_curve: list[dict]) -> dict:
    if not trades:
        return {"total_trades": 0, "win_rate": 0, "profit_factor": 0, "expectancy_r": 0, "max_drawdown": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    r_values = [t["r_multiple"] for t in trades]
    max_dd = _max_drawdown([p["equity"] for p in equity_curve] or [initial_equity])
    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "expectancy_r": round(sum(r_values) / len(r_values), 4),
        "avg_r": round(sum(r_values) / len(r_values), 4),
        "max_drawdown": round(max_dd, 4),
        "ending_equity": round(equity_curve[-1]["equity"], 2) if equity_curve else round(initial_equity, 2),
    }


def _max_drawdown(values: list[float]) -> float:
    peak = values[0] if values else 0
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return max_dd
