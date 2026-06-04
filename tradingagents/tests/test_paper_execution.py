"""Tests for conservative paper execution simulation."""

from paper_execution import (
    PaperAccountState,
    PaperOrderIntent,
    PaperRiskLimits,
    check_order_risk,
    handle_unknown_order_state,
    simulate_limit_order,
    simulate_market_order,
)


def test_market_order_consumes_ask_depth_with_vwap_and_partial_fill():
    intent = PaperOrderIntent(
        symbol="BTCUSDT",
        side="buy",
        quantity=3.0,
        order_type="market",
        signal_price=100.0,
    )
    result = simulate_market_order(
        intent,
        {"asks": [{"price": 101.0, "size": 1.0}, {"price": 102.0, "size": 1.5}]},
        fee_rate=0.001,
    )

    assert result["state"] == "partially_filled"
    assert result["filled_qty"] == 2.5
    assert result["unfilled_qty"] == 0.5
    assert result["avg_price"] == 101.6
    assert result["fee"] == 0.254
    assert result["slippage_bps"] == 160.0


def test_market_sell_uses_bid_side_depth():
    intent = PaperOrderIntent(
        symbol="BTCUSDT",
        side="sell",
        quantity=2.0,
        order_type="market",
        signal_price=100.0,
    )
    result = simulate_market_order(
        intent,
        {"bids": [{"price": 99.5, "size": 1.0}, {"price": 99.0, "size": 1.0}]},
    )

    assert result["state"] == "filled"
    assert result["avg_price"] == 99.25
    assert result["slippage_bps"] == 75.0


def test_limit_order_does_not_fill_only_because_price_touched():
    intent = PaperOrderIntent(
        symbol="ETHUSDT",
        side="buy",
        quantity=10.0,
        order_type="limit",
        price=50.0,
        signal_price=50.1,
    )

    result = simulate_limit_order(
        intent,
        price_touched=True,
        traded_volume_at_price=5.0,
        queue_ahead_qty=5.0,
    )

    assert result["state"] == "open"
    assert result["reason"] == "touched_but_queue_not_filled"
    assert result["filled_qty"] == 0.0


def test_limit_order_supports_partial_fill_after_queue_volume():
    intent = PaperOrderIntent(
        symbol="ETHUSDT",
        side="buy",
        quantity=10.0,
        order_type="limit",
        price=50.0,
    )

    result = simulate_limit_order(
        intent,
        price_touched=True,
        traded_volume_at_price=8.0,
        queue_ahead_qty=3.0,
        fee_rate=0.001,
    )

    assert result["state"] == "partially_filled"
    assert result["filled_qty"] == 5.0
    assert result["unfilled_qty"] == 5.0
    assert result["fee"] == 0.25


def test_risk_control_rejects_before_execution():
    account = PaperAccountState(cash={"USD": 1_000.0})
    intent = PaperOrderIntent(symbol="ABAT", side="buy", quantity=100.0, order_type="market")
    limits = PaperRiskLimits(max_order_notional=500.0, min_notional=10.0)

    result = check_order_risk(intent, account, limits, current_price=6.0)

    assert result["approved"] is False
    assert result["reason"] == "max_order_notional_exceeded"


def test_risk_control_rejects_when_kill_switch_active():
    account = PaperAccountState(cash={"USD": 10_000.0})
    intent = PaperOrderIntent(symbol="ABAT", side="buy", quantity=10.0, order_type="market")
    limits = PaperRiskLimits(kill_switch_active=True)

    result = check_order_risk(intent, account, limits, current_price=5.0)

    assert result["approved"] is False
    assert result["reason"] == "kill_switch_active"


def test_unknown_order_state_never_allows_blind_resubmit():
    intent = PaperOrderIntent(
        symbol="BTCUSDT",
        side="buy",
        quantity=1.0,
        order_type="market",
        client_order_id="client-123",
    )

    result = handle_unknown_order_state(intent, query_attempted=False, found_on_exchange=None)

    assert result["state"] == "unknown"
    assert result["suspend_trading"] is True
    assert result["reconcile_required"] is True
    assert result["allow_resubmit"] is False
    assert result["reason"] == "query_by_client_order_id_required"
