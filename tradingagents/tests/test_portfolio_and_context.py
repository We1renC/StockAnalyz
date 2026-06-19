import sqlite3
import pytest
import pandas as pd
from unittest.mock import MagicMock, ANY, patch
from portfolio_risk_gate import PortfolioRiskGate
from smc_training_loop import AccountContext, auto_backtest_window
from migrations import run_migrations

def test_portfolio_risk_gate_total_exposure():
    gate = PortfolioRiskGate()
    open_positions = [
        {"symbol": "BTC-USDT", "direction": 1, "notional": 20000.0},
        {"symbol": "ETH-USDT", "direction": 1, "notional": 5000.0},
    ]
    # Total notional is 25,000.0. Equity is 100,000.0. Total exposure is 25%.
    # Adding a position with 6,000.0 notional makes total exposure 31% (> 30%).
    allowed, reason = gate.check_new_position(
        new_symbol="SOL-USDT",
        new_direction=1,
        new_notional=6000.0,
        open_positions=open_positions,
        equity=100000.0
    )
    assert not allowed
    assert "Total exposure" in reason

def test_portfolio_risk_gate_correlation():
    gate = PortfolioRiskGate()
    open_positions = [
        {"symbol": "BTC-USDT", "direction": 1, "notional": 10000.0}, # 10%
    ]
    # BTC-USDT and ETH-USDT correlation is 0.90 (>= 0.70 threshold).
    # Adding ETH-USDT long with 6,000.0 notional (6%) makes the same direction correlated exposure 16% (> 15%).
    allowed, reason = gate.check_new_position(
        new_symbol="ETH-USDT",
        new_direction=1,
        new_notional=6000.0,
        open_positions=open_positions,
        equity=100000.0
    )
    assert not allowed
    assert "Correlated exposure" in reason

def test_portfolio_risk_gate_allowed():
    gate = PortfolioRiskGate()
    open_positions = [
        {"symbol": "BTC-USDT", "direction": 1, "notional": 10000.0}, # 10%
    ]
    # Adding XRP-USDT (uncorrelated or low correlation < 0.7) with 10,000.0 notional.
    # Total exposure is 20% (< 30%), correlated same direction for BTC is 10%, for XRP is 10% (< 15%).
    allowed, reason = gate.check_new_position(
        new_symbol="XRP-USDT",
        new_direction=1,
        new_notional=10000.0,
        open_positions=open_positions,
        equity=100000.0
    )
    assert allowed
    assert reason == "ok"

def test_account_context_injection():
    api = MagicMock()
    # Mock api.klines to return some bars
    api.klines.return_value = {
        "status": 200,
        "payload": {
            "data": [
                {"open_time": "2026-06-18T00:00:00Z", "open": "100.0", "high": "105.0", "low": "95.0", "close": "102.0", "volume": "1000.0"}
                for _ in range(10)
            ]
        }
    }
    
    # Mock profile_for_symbol to return a profile
    from smc_auto_workflow import SmcAutoProfile
    
    profile = SmcAutoProfile(
        tier="major",
        interval="1h",
        bars=500,
        swing_length=5,
        internal_swing_length=5,
        min_confluence_score=6,
        min_rr=1.5,
        risk_pct=0.01,
        max_notional_usdt=5000.0,
        price_deviation_pct=0.02,
        cooldown_minutes=60
    )
    
    account_ctx = AccountContext(equity=50000.0, available_cash=50000.0)
    
    with patch("smc_training_loop.profile_for_symbol", return_value=profile), \
         patch("smc_training_loop.build_smc_analysis") as mock_build:
        
        mock_build.return_value = {"concepts": {"entry_models": {}}}
        
        auto_backtest_window(
            api=api,
            symbol="BTC-USDT",
            account_ctx=account_ctx
        )
        
        # Verify build_smc_analysis was called with account_equity=50000.0
        mock_build.assert_any_call(
            ANY,
            symbol="BTC-USDT",
            timeframe="1h",
            config=ANY,
            account_equity=50000.0,
            cluster_weight_table=ANY,
            cluster_key_hint=ANY
        )

def test_centralized_migrations():
    # Connect to in-memory SQLite database
    conn = sqlite3.connect(":memory:")
    
    # First, let's create a mockup of the tables migrations alter to simulate pre-existing DB
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY)")
    cursor.execute("CREATE TABLE IF NOT EXISTS market_state (id INTEGER PRIMARY KEY)")
    cursor.execute("CREATE TABLE IF NOT EXISTS price_cache (symbol TEXT PRIMARY KEY)")
    cursor.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY)")
    cursor.execute("CREATE TABLE IF NOT EXISTS smc_backtest_trades (id INTEGER PRIMARY KEY)")
    conn.commit()

    # Run migrations
    run_migrations(conn)
    
    # Check if table schema_migrations exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
    assert cursor.fetchone() is not None
    
    # Verify that positions now has target_entry column (migration v1)
    cursor.execute("PRAGMA table_info(positions)")
    columns = [row[1] for row in cursor.fetchall()]
    assert "target_entry" in columns
    
    conn.close()
