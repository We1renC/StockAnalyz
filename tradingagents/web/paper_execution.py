"""Conservative paper execution and risk-control primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional


OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
OrderState = Literal[
    "new",
    "partially_filled",
    "filled",
    "open",
    "canceled",
    "rejected",
    "expired",
    "unknown",
]


@dataclass(frozen=True)
class PaperOrderIntent:
    """Strategy output before risk approval and paper execution."""

    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType
    price: Optional[float] = None
    signal_price: Optional[float] = None
    strategy_version: str = ""
    parameter_version: str = ""
    signal_source: str = ""
    client_order_id: str = ""


@dataclass(frozen=True)
class PaperRiskLimits:
    """Risk-control limits that have higher priority than strategy logic."""

    max_order_notional: float = 0.0
    max_position_notional_per_symbol: float = 0.0
    max_open_orders: int = 0
    max_directional_exposure: float = 0.0
    min_notional: float = 0.0
    min_quantity: float = 0.0
    kill_switch_active: bool = False


@dataclass
class PaperAccountState:
    """Virtual paper account state for pre-trade risk checks."""

    cash: dict[str, float] = field(default_factory=lambda: {"USD": 0.0})
    frozen_cash: dict[str, float] = field(default_factory=dict)
    positions: dict[str, float] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    def available_cash(self, currency: str = "USD") -> float:
        return float(self.cash.get(currency, 0.0)) - float(self.frozen_cash.get(currency, 0.0))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _levels(order_book: Mapping[str, Any], side: OrderSide) -> list[tuple[float, float]]:
    key = "asks" if side == "buy" else "bids"
    levels = []
    for level in order_book.get(key) or []:
        price = _num(level.get("price") if isinstance(level, Mapping) else level[0])
        size = _num(level.get("size") if isinstance(level, Mapping) else level[1])
        if price > 0 and size > 0:
            levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=(side == "sell"))
    return levels


def _slippage_bps(side: OrderSide, signal_price: Optional[float], execution_price: Optional[float]) -> Optional[float]:
    if not signal_price or not execution_price or signal_price <= 0:
        return None
    raw = (execution_price - signal_price) / signal_price
    signed = raw if side == "buy" else -raw
    return round(signed * 10_000, 4)


def simulate_market_order(
    intent: PaperOrderIntent,
    order_book: Mapping[str, Any],
    *,
    fee_rate: float = 0.0,
    volatility_bps: float = 0.0,
    liquidity_impact_bps: float = 0.0,
) -> dict[str, Any]:
    """Simulate a market order by consuming book-side depth.

    Buy orders consume asks; sell orders consume bids. If available depth is
    insufficient, the order is partially filled rather than assumed complete.
    """

    if intent.order_type != "market":
        raise ValueError("simulate_market_order requires a market order")
    if intent.quantity <= 0:
        return {"state": "rejected", "reason": "quantity_must_be_positive", "filled_qty": 0.0}
    remaining = float(intent.quantity)
    notional = 0.0
    fills = []
    for price, size in _levels(order_book, intent.side):
        fill_qty = min(remaining, size)
        if fill_qty <= 0:
            continue
        remaining -= fill_qty
        notional += fill_qty * price
        fills.append({"price": price, "qty": fill_qty})
        if remaining <= 1e-12:
            break
    filled_qty = float(intent.quantity) - remaining
    if filled_qty <= 0:
        return {
            "state": "rejected",
            "reason": "insufficient_order_book_depth",
            "filled_qty": 0.0,
            "unfilled_qty": float(intent.quantity),
            "fills": [],
        }
    avg_price = notional / filled_qty
    impact = max(0.0, float(volatility_bps) + float(liquidity_impact_bps)) / 10_000
    if impact:
        avg_price = avg_price * (1 + impact if intent.side == "buy" else 1 - impact)
    fee = abs(avg_price * filled_qty * float(fee_rate))
    state: OrderState = "filled" if remaining <= 1e-12 else "partially_filled"
    return {
        "state": state,
        "reason": "ok" if state == "filled" else "partial_depth",
        "side": intent.side,
        "symbol": intent.symbol.upper(),
        "filled_qty": round(filled_qty, 8),
        "unfilled_qty": round(max(remaining, 0.0), 8),
        "avg_price": round(avg_price, 8),
        "notional": round(avg_price * filled_qty, 8),
        "fee": round(fee, 8),
        "slippage_bps": _slippage_bps(intent.side, intent.signal_price, avg_price),
        "signal_price": intent.signal_price,
        "simulated_execution_price": round(avg_price, 8),
        "fills": fills,
    }


def simulate_limit_order(
    intent: PaperOrderIntent,
    *,
    price_touched: bool,
    traded_volume_at_price: float,
    queue_ahead_qty: float = 0.0,
    fee_rate: float = 0.0,
    timeout: bool = False,
    post_only_rejected: bool = False,
) -> dict[str, Any]:
    """Simulate a limit order without using touch-price equals filled."""

    if intent.order_type != "limit":
        raise ValueError("simulate_limit_order requires a limit order")
    if intent.quantity <= 0 or not intent.price or intent.price <= 0:
        return {"state": "rejected", "reason": "invalid_limit_order", "filled_qty": 0.0}
    if post_only_rejected:
        return {"state": "rejected", "reason": "post_only_rejected", "filled_qty": 0.0}
    if not price_touched:
        return {
            "state": "expired" if timeout else "open",
            "reason": "price_not_touched",
            "filled_qty": 0.0,
            "unfilled_qty": float(intent.quantity),
        }
    available_after_queue = max(0.0, float(traded_volume_at_price) - float(queue_ahead_qty))
    filled_qty = min(float(intent.quantity), available_after_queue)
    if filled_qty <= 0:
        return {
            "state": "expired" if timeout else "open",
            "reason": "touched_but_queue_not_filled",
            "filled_qty": 0.0,
            "unfilled_qty": float(intent.quantity),
            "queue_ahead_qty": float(queue_ahead_qty),
        }
    remaining = float(intent.quantity) - filled_qty
    state: OrderState = "filled" if remaining <= 1e-12 else "partially_filled"
    fee = abs(float(intent.price) * filled_qty * float(fee_rate))
    return {
        "state": state,
        "reason": "ok" if state == "filled" else "partial_queue_fill",
        "side": intent.side,
        "symbol": intent.symbol.upper(),
        "filled_qty": round(filled_qty, 8),
        "unfilled_qty": round(max(remaining, 0.0), 8),
        "avg_price": round(float(intent.price), 8),
        "notional": round(float(intent.price) * filled_qty, 8),
        "fee": round(fee, 8),
        "slippage_bps": _slippage_bps(intent.side, intent.signal_price, float(intent.price)),
        "queue_ahead_qty": float(queue_ahead_qty),
        "traded_volume_at_price": float(traded_volume_at_price),
    }


def check_order_risk(
    intent: PaperOrderIntent,
    account: PaperAccountState,
    limits: PaperRiskLimits,
    *,
    current_price: float,
    currency: str = "USD",
    open_order_count: int = 0,
    directional_exposure: float = 0.0,
) -> dict[str, Any]:
    """Approve or reject an order before it reaches paper execution."""

    if limits.kill_switch_active:
        return {"approved": False, "reason": "kill_switch_active"}
    if intent.quantity <= 0:
        return {"approved": False, "reason": "quantity_must_be_positive"}
    notional = float(intent.quantity) * float(current_price)
    if limits.min_quantity and intent.quantity < limits.min_quantity:
        return {"approved": False, "reason": "below_min_quantity", "notional": round(notional, 8)}
    if limits.min_notional and notional < limits.min_notional:
        return {"approved": False, "reason": "below_min_notional", "notional": round(notional, 8)}
    if limits.max_order_notional and notional > limits.max_order_notional:
        return {"approved": False, "reason": "max_order_notional_exceeded", "notional": round(notional, 8)}
    if limits.max_open_orders and open_order_count >= limits.max_open_orders:
        return {"approved": False, "reason": "max_open_orders_exceeded", "open_order_count": open_order_count}
    current_position_qty = float(account.positions.get(intent.symbol.upper(), 0.0))
    projected_qty = current_position_qty + (intent.quantity if intent.side == "buy" else -intent.quantity)
    projected_notional = abs(projected_qty * float(current_price))
    if limits.max_position_notional_per_symbol and projected_notional > limits.max_position_notional_per_symbol:
        return {
            "approved": False,
            "reason": "max_symbol_position_exceeded",
            "projected_notional": round(projected_notional, 8),
        }
    projected_exposure = abs(float(directional_exposure) + (notional if intent.side == "buy" else -notional))
    if limits.max_directional_exposure and projected_exposure > limits.max_directional_exposure:
        return {
            "approved": False,
            "reason": "directional_exposure_exceeded",
            "projected_exposure": round(projected_exposure, 8),
        }
    if intent.side == "buy" and account.available_cash(currency) < notional:
        return {
            "approved": False,
            "reason": "insufficient_available_balance",
            "available_cash": round(account.available_cash(currency), 8),
            "required_cash": round(notional, 8),
        }
    return {"approved": True, "reason": "ok", "notional": round(notional, 8)}


def handle_unknown_order_state(intent: PaperOrderIntent, *, query_attempted: bool, found_on_exchange: Optional[bool]) -> dict[str, Any]:
    """Risk action for unknown order state.

    The action intentionally avoids blind resubmission. The caller should pause
    new entries and reconcile by client order id.
    """

    if not intent.client_order_id:
        return {
            "state": "unknown",
            "suspend_trading": True,
            "reconcile_required": True,
            "allow_resubmit": False,
            "reason": "missing_client_order_id",
        }
    if not query_attempted:
        return {
            "state": "unknown",
            "suspend_trading": True,
            "reconcile_required": True,
            "allow_resubmit": False,
            "reason": "query_by_client_order_id_required",
            "client_order_id": intent.client_order_id,
        }
    if found_on_exchange is None:
        reason = "exchange_state_still_unknown"
    elif found_on_exchange:
        reason = "exchange_order_found_reconcile_local_ledger"
    else:
        reason = "exchange_order_not_found_keep_suspended_until_rechecked"
    return {
        "state": "unknown",
        "suspend_trading": True,
        "reconcile_required": True,
        "allow_resubmit": False,
        "reason": reason,
        "client_order_id": intent.client_order_id,
    }


__all__ = [
    "PaperAccountState",
    "PaperOrderIntent",
    "PaperRiskLimits",
    "check_order_risk",
    "handle_unknown_order_state",
    "simulate_limit_order",
    "simulate_market_order",
]
