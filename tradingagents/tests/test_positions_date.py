"""Tests for position purchase date CRUD operations in app.py."""
import pytest
import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import patch
from datetime import date

import app
from app import PositionCreate, PositionUpdate, api_add_position, api_update_position, TradeCreate, api_add_trade


@pytest.fixture(autouse=True)
def temp_db():
    """Sets up an isolated, temporary database for position tests."""
    fd, temp_db_path = tempfile.mkstemp()
    os.close(fd)
    
    with patch("app.DB", Path(temp_db_path)):
        app.init_db()
        yield temp_db_path
        
    if os.path.exists(temp_db_path):
        os.remove(temp_db_path)


def test_api_add_position_with_date(temp_db):
    """Test adding a position with a specified purchase date."""
    p = PositionCreate(
        symbol="TEST.TW",
        name="測試",
        category="測試股",
        shares=10.0,
        cost_price=100.0,
        currency="TWD",
        purchase_date="2026-05-20"
    )
    
    with patch("app.DB", Path(temp_db)):
        res = api_add_position(p)
        assert res == {"ok": True}
        
        # Verify database record
        conn = app.get_db()
        row = conn.execute("SELECT * FROM positions WHERE symbol='TEST.TW'").fetchone()
        conn.close()
        
        assert row is not None
        assert row["purchase_date"] == "2026-05-20"


def test_api_add_position_without_date(temp_db):
    """Test adding a position without specifying purchase date defaults to today."""
    p = PositionCreate(
        symbol="TEST.TW",
        name="測試",
        category="測試股",
        shares=10.0,
        cost_price=100.0,
        currency="TWD"
    )
    
    with patch("app.DB", Path(temp_db)):
        res = api_add_position(p)
        assert res == {"ok": True}
        
        # Verify database record defaults to today
        conn = app.get_db()
        row = conn.execute("SELECT * FROM positions WHERE symbol='TEST.TW'").fetchone()
        conn.close()
        
        assert row is not None
        assert row["purchase_date"] == date.today().isoformat()


def test_api_update_position_date(temp_db):
    """Test updating the purchase date of an existing position."""
    # First add a position
    p_create = PositionCreate(
        symbol="TEST.TW",
        name="測試",
        category="測試股",
        shares=10.0,
        cost_price=100.0,
        currency="TWD",
        purchase_date="2026-05-20"
    )
    
    with patch("app.DB", Path(temp_db)):
        api_add_position(p_create)
        
        # Retrieve its ID
        conn = app.get_db()
        row = conn.execute("SELECT id FROM positions WHERE symbol='TEST.TW'").fetchone()
        pid = row["id"]
        conn.close()
        
        # Now update it
        p_update = PositionUpdate(purchase_date="2026-05-25")
        res = api_update_position(pid, p_update)
        assert res == {"ok": True}
        
        # Verify change
        conn = app.get_db()
        updated_row = conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        conn.close()
        
        assert updated_row["purchase_date"] == "2026-05-25"


def test_api_add_position_creates_trade(temp_db):
    """Test that adding a position automatically creates a corresponding buy trade record."""
    p = PositionCreate(
        symbol="TEST.TW",
        name="測試",
        category="測試股",
        shares=15.0,
        cost_price=120.0,
        currency="TWD",
        purchase_date="2026-05-20"
    )
    
    with patch("app.DB", Path(temp_db)):
        res = api_add_position(p)
        assert res == {"ok": True}
        
        # Verify trade record exists
        conn = app.get_db()
        trade = conn.execute("SELECT * FROM trades WHERE symbol='TEST.TW'").fetchone()
        conn.close()
        
        assert trade is not None
        assert trade["action"] == "buy"
        assert trade["shares"] == 15.0
        assert trade["price"] == 120.0
        assert trade["trade_date"] == "2026-05-20"
        assert trade["notes"] == "新增持倉自動導入"


def test_api_add_sell_trade_autofills_from_position(temp_db):
    """Test that adding a sell trade for a symbol with deficit buy trades automatically creates a buy trade from the active position's cost."""
    with patch("app.DB", Path(temp_db)):
        conn = app.get_db()
        conn.execute(
            "INSERT INTO positions (symbol, name, category, shares, cost_price, currency, purchase_date) "
            "VALUES ('TEST.TW', '測試', '測試股', 10.0, 150.0, 'TWD', '2026-05-15')"
        )
        conn.commit()
        conn.close()
        
        # Now add a sell trade of 5.0 shares
        t = TradeCreate(
            symbol="TEST.TW",
            name="測試",
            action="sell",
            shares=5.0,
            price=200.0,
            currency="TWD",
            trade_date="2026-06-03",
            auto_fee=True
        )
        
        res = api_add_trade(t)
        assert res["ok"] is True
        
        # Verify both buy and sell trades exist in trades table
        conn = app.get_db()
        trades = conn.execute("SELECT * FROM trades WHERE symbol='TEST.TW' ORDER BY action").fetchall()
        conn.close()
        
        assert len(trades) == 2
        buy_trade = [tr for tr in trades if tr["action"] == "buy"][0]
        sell_trade = [tr for tr in trades if tr["action"] == "sell"][0]
        
        assert buy_trade["shares"] == 5.0  # Deficit of sell (5.0) - buy (0.0) = 5.0
        assert buy_trade["price"] == 150.0 # From positions cost_price
        assert buy_trade["trade_date"] == "2026-05-15" # From positions purchase_date
        assert buy_trade["notes"] == "自動補登初始持倉成本"
        
        assert sell_trade["shares"] == 5.0
        assert sell_trade["price"] == 200.0
