"""Tests for portfolio_snapshots immutability + auto-quote on add."""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app
import pandas as pd


def _setup_temp_db(tmp_path):
    """Point app.DB at an isolated sqlite file for this test, init schema."""
    db_file = tmp_path / "test_portfolio.db"
    original = app.DB
    app.DB = str(db_file)
    app.init_db()
    return original


def _restore_db(original):
    app.DB = original


def _insert_position(symbol="2330.TW", name="台積電", shares=10, cost_price=1000,
                    currency="TWD", purchase_date=None):
    conn = app.get_db()
    conn.execute(
        """INSERT INTO positions
           (symbol, name, category, shares, cost_price, currency, purchase_date)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (symbol, name, "半導體", shares, cost_price, currency,
         purchase_date or date.today().isoformat()),
    )
    conn.commit()
    conn.close()


def _set_price_cache(symbol, price):
    conn = app.get_db()
    c = conn.cursor()
    app.store_price_cache(c, symbol, {"price": price, "source": "test"})
    conn.commit()
    conn.close()


def _insert_watchlist(symbol="2330.TW", name="台積電", category="半導體", currency="TWD"):
    conn = app.get_db()
    conn.execute(
        """INSERT INTO watchlist
           (symbol, name, category, currency, target_entry, target_profit, target_stop)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (symbol, name, category, currency, 100.0, 120.0, 90.0),
    )
    conn.commit()
    conn.close()


def test_snapshot_writes_one_row_per_zone_per_day(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("2330.TW", "台積電", shares=10, cost_price=1000, currency="TWD")
        _set_price_cache("2330.TW", 1100)
        app._record_portfolio_snapshot()

        conn = app.get_db()
        rows = conn.execute(
            "SELECT date, zone, total_cost, total_value FROM portfolio_snapshots"
        ).fetchall()
        intraday_rows = conn.execute(
            "SELECT trade_date, zone, total_cost, total_value FROM portfolio_intraday_snapshots"
        ).fetchall()
        conn.close()
        zones = {r["zone"]: dict(r) for r in rows}
        intraday_zones = {r["zone"]: dict(r) for r in intraday_rows}
        assert "tw" in zones
        assert "tw" in intraday_zones
        assert zones["tw"]["total_cost"] == 10 * 1000
        assert zones["tw"]["total_value"] == 10 * 1100
        assert intraday_zones["tw"]["total_cost"] == 10 * 1000
        assert intraday_zones["tw"]["total_value"] == 10 * 1100
    finally:
        _restore_db(original)


def test_snapshot_history_unchanged_after_cost_edit(tmp_path):
    """Past snapshots stay frozen even when user later edits cost_price."""
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("2330.TW", "台積電", shares=10, cost_price=1000)
        _set_price_cache("2330.TW", 1100)

        # Day 1 snapshot
        conn = app.get_db()
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_snapshots
               (date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
               VALUES (?, 'tw', 10000, 11000, 1000, 950, 1)""",
            ((date.today() - timedelta(days=2)).isoformat(),),
        )
        conn.commit()
        conn.close()

        # User edits cost_price drastically (e.g., correcting a typo)
        conn = app.get_db()
        conn.execute("UPDATE positions SET cost_price = 5000 WHERE symbol='2330.TW'")
        conn.commit()
        conn.close()

        # Snapshot for today reflects the edit; yesterday should NOT
        app._record_portfolio_snapshot()

        conn = app.get_db()
        rows = conn.execute(
            "SELECT date, total_cost, total_value FROM portfolio_snapshots WHERE zone='tw' ORDER BY date"
        ).fetchall()
        conn.close()
        assert len(rows) >= 2
        past = rows[0]
        today_row = rows[-1]
        assert past["total_cost"] == 10000  # unchanged
        assert today_row["total_cost"] == 50000  # reflects edit
    finally:
        _restore_db(original)


def test_add_position_triggers_immediate_quote(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        with patch.object(app, "fetch_indicators", return_value={"price": 555.0, "rsi": 65.0, "source": "test"}):
            req = app.PositionCreate(symbol="2330.TW", name="台積電", category="半導體",
                                     shares=5, cost_price=500.0, currency="TWD")
            app.api_add_position(req)

        conn = app.get_db()
        row = conn.execute("SELECT price FROM price_cache WHERE symbol='2330.TW'").fetchone()
        conn.close()
        assert row is not None, "price_cache should have a row right after add"
        assert row["price"] == 555.0
    finally:
        _restore_db(original)


def test_trend_reads_from_snapshots_not_current_positions(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        # Seed snapshots for past two days
        for offset, value in [(2, 9500), (1, 9800)]:
            conn = app.get_db()
            conn.execute(
                """INSERT OR REPLACE INTO portfolio_snapshots
                   (date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
                   VALUES (?, 'tw', 10000, ?, ?, ?, 1)""",
                ((date.today() - timedelta(days=offset)).isoformat(), value, value - 10000, value - 10100),
            )
            conn.commit()
            conn.close()

        result = app.api_portfolio_trend()
        tw = result.get("tw") or []
        # Past snapshots should appear as 2 points
        past_points = [p for p in tw if not p.get("live")]
        assert len(past_points) == 2
        assert past_points[0]["total_value"] == 9500
        assert past_points[1]["total_value"] == 9800
    finally:
        _restore_db(original)


def test_trend_reads_intraday_snapshots_for_today(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        conn = app.get_db()
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_intraday_snapshots
               (ts, trade_date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
               VALUES (?, ?, 'tw', 10000, 10100, 100, 90, 1)""",
            (f"{date.today().isoformat()}T09:00", date.today().isoformat()),
        )
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_intraday_snapshots
               (ts, trade_date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
               VALUES (?, ?, 'tw', 10000, 10250, 250, 230, 1)""",
            (f"{date.today().isoformat()}T09:05", date.today().isoformat()),
        )
        conn.commit()
        conn.close()

        result = app.api_portfolio_trend()
        tw = result.get("tw") or []
        live_points = [p for p in tw if p.get("live")]
        assert len(live_points) >= 2
        assert live_points[-2]["total_value"] == 10100
        assert live_points[-1]["total_value"] == 10250
    finally:
        _restore_db(original)


def test_intraday_snapshot_uses_10_minute_bucket(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("2330.TW", "台積電", shares=10, cost_price=1000, currency="TWD")
        _set_price_cache("2330.TW", 1100)

        conn = app.get_db()
        app._record_portfolio_snapshot(conn)
        conn.execute("DELETE FROM portfolio_intraday_snapshots")
        conn.commit()

        rows = conn.execute(
            """SELECT p.*, pc.price as current_price, pc.nav, pc.pb, pc.quote_type, pc.data AS price_cache_data
               FROM positions p
               LEFT JOIN price_cache pc ON p.symbol = pc.symbol"""
        ).fetchall()
        fees_cfg = (app.load_settings().get("brokerage_fees") or {})
        market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
        market = dict(market_row) if market_row else None
        enriched = [app._enrich_position_for_portfolio(conn, dict(r), market, fees_cfg, None)[0] for r in rows]
        tw_positions, us_positions = app._split_positions_by_zone(enriched)

        app._record_intraday_portfolio_snapshot(
            conn,
            tw_positions,
            us_positions,
            fees_cfg,
            now_dt=app.datetime.fromisoformat(f"{date.today().isoformat()}T09:07:35"),
        )
        app._record_intraday_portfolio_snapshot(
            conn,
            tw_positions,
            us_positions,
            fees_cfg,
            now_dt=app.datetime.fromisoformat(f"{date.today().isoformat()}T09:09:59"),
        )
        app._record_intraday_portfolio_snapshot(
            conn,
            tw_positions,
            us_positions,
            fees_cfg,
            now_dt=app.datetime.fromisoformat(f"{date.today().isoformat()}T09:10:01"),
        )
        conn.commit()

        intraday = conn.execute(
            "SELECT ts FROM portfolio_intraday_snapshots WHERE zone='tw' ORDER BY ts"
        ).fetchall()
        conn.close()
        assert [row["ts"] for row in intraday] == [
            f"{date.today().isoformat()}T09:00",
            f"{date.today().isoformat()}T09:10",
        ]
    finally:
        _restore_db(original)


def test_trend_backfills_daily_history_from_purchase_date(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        purchase_date = (date.today() - timedelta(days=3)).isoformat()
        _insert_position("2330.TW", "台積電", shares=10, cost_price=100, currency="TWD", purchase_date=purchase_date)

        hist = pd.DataFrame(
            {
                "Close": [101.0, 102.0, 103.0],
            },
            index=pd.to_datetime([
                date.today() - timedelta(days=3),
                date.today() - timedelta(days=2),
                date.today() - timedelta(days=1),
            ]),
        )

        with patch.object(app, "_fetch_daily_history_from_date", return_value=hist), \
             patch.object(app, "_get_vault", return_value=None):
            result = app.api_portfolio_trend()

        tw = result.get("tw") or []
        past_points = [p for p in tw if not p.get("live")]
        assert len(past_points) >= 3
        assert past_points[-3]["total_value"] == 1010
        assert past_points[-2]["total_value"] == 1020
        assert past_points[-1]["total_value"] == 1030

        conn = app.get_db()
        rows = conn.execute(
            "SELECT date, total_value FROM portfolio_snapshots WHERE zone='tw' ORDER BY date"
        ).fetchall()
        conn.close()
        assert [row["total_value"] for row in rows[-3:]] == [1010, 1020, 1030]
    finally:
        _restore_db(original)


def test_portfolio_falls_back_to_obsidian_purchase_date_for_annualized(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        purchase_date = (date.today() - timedelta(days=30)).isoformat()
        _insert_position("2330.TW", "台積電", shares=10, cost_price=100)
        conn = app.get_db()
        conn.execute("UPDATE positions SET purchase_date=NULL WHERE symbol='2330.TW'")
        conn.commit()
        conn.close()
        _set_price_cache("2330.TW", 110)

        vault = tmp_path / "vault"
        app._obsidian_write_position(vault, {
            "symbol": "2330.TW",
            "name": "台積電",
            "category": "半導體",
            "shares": 10,
            "cost_price": 100,
            "currency": "TWD",
            "purchase_date": purchase_date,
            "target_entry": None,
            "target_profit": None,
            "target_stop": None,
        })

        with patch.object(app, "_get_vault", return_value=Path(vault)):
            result = app.api_portfolio()

        pos = result["positions"][0]
        assert pos["purchase_date"] == purchase_date
        assert pos["annualized_status"] == "ok"
        assert pos["annualized_return_pct"] is not None

        conn = app.get_db()
        row = conn.execute("SELECT purchase_date FROM positions WHERE symbol='2330.TW'").fetchone()
        conn.close()
        assert row["purchase_date"] == purchase_date
    finally:
        _restore_db(original)


def test_portfolio_backfills_etf_nav_into_sqlite_price_cache(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("0050.TW", "元大台灣50", shares=2, cost_price=100, currency="TWD")
        _set_price_cache("0050.TW", 110)

        with patch.object(app, "_get_yf_quote_info", return_value={
            "realtime": 110.0,
            "nav": 108.0,
            "quote_type": "ETF",
        }):
            result = app.api_portfolio()

        pos = result["positions"][0]
        assert pos["is_etf"] is True
        assert pos["premium_status"] == "ok"
        assert pos["premium_pct"] == round((110 / 108 - 1) * 100, 2)

        conn = app.get_db()
        row = conn.execute("SELECT data FROM price_cache WHERE symbol='0050.TW'").fetchone()
        conn.close()
        payload = json.loads(row["data"])
        assert payload["nav"] == 108.0
        assert payload["quote_type"] == "ETF"
    finally:
        _restore_db(original)


def test_portfolio_backfills_equity_pb_into_sqlite_price_cache(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_position("AAPL", "Apple", shares=1, cost_price=100, currency="USD")
        _set_price_cache("AAPL", 110)

        with patch.object(app, "_get_yf_quote_info", return_value={
            "realtime": 110.0,
            "nav": None,
            "quote_type": "EQUITY",
            "pb": 12.34,
        }):
            result = app.api_portfolio()

        pos = result["positions"][0]
        assert pos["is_etf"] is False
        assert pos["pb"] == 12.34

        conn = app.get_db()
        row = conn.execute("SELECT data FROM price_cache WHERE symbol='AAPL'").fetchone()
        conn.close()
        payload = json.loads(row["data"])
        assert payload["pb"] == 12.34
    finally:
        _restore_db(original)


def test_watchlist_exposes_etf_premium_and_equity_pb(tmp_path):
    original = _setup_temp_db(tmp_path)
    try:
        _insert_watchlist("0050.TW", "元大台灣50", category="ETF", currency="TWD")
        _insert_watchlist("AAPL", "Apple", category="科技", currency="USD")
        _set_price_cache("0050.TW", 110)
        _set_price_cache("AAPL", 110)

        def fake_quote_info(symbol, want_info=True):
            if symbol == "0050.TW":
                return {"realtime": 110.0, "nav": 108.0, "quote_type": "ETF", "pb": None}
            if symbol == "AAPL":
                return {"realtime": 110.0, "nav": None, "quote_type": "EQUITY", "pb": 12.34}
            return {}

        with patch.object(app, "_get_yf_quote_info", side_effect=fake_quote_info):
            result = app.api_watchlist()

        rows = {item["symbol"]: item for item in result["watchlist"]}
        assert rows["0050.TW"]["is_etf"] is True
        assert rows["0050.TW"]["premium_status"] == "ok"
        assert rows["0050.TW"]["premium_pct"] == round((110 / 108 - 1) * 100, 2)
        assert rows["AAPL"]["is_etf"] is False
        assert rows["AAPL"]["pb"] == 12.34

        conn = app.get_db()
        etf_row = conn.execute("SELECT nav, quote_type FROM price_cache WHERE symbol='0050.TW'").fetchone()
        equity_row = conn.execute("SELECT pb, quote_type FROM price_cache WHERE symbol='AAPL'").fetchone()
        conn.close()
        assert etf_row["nav"] == 108.0
        assert etf_row["quote_type"] == "ETF"
        assert equity_row["pb"] == 12.34
        assert equity_row["quote_type"] == "EQUITY"
    finally:
        _restore_db(original)
