"""Tests for SMC paper/live trade journal storage and sync."""

from pathlib import Path
from unittest.mock import patch

import app
from app import SMCJournalCreate, SMCJournalUpdate
from learning.trade_store import load_trades_from_db


def _temp_db(tmp_path):
    original = app.DB
    app.DB = str(tmp_path / "smc_journal.db")
    app.init_db()
    return original


def test_api_add_smc_journal_computes_outcome(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "_get_vault", return_value=None):
            res = app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="BTCUSDT",
                    name="Bitcoin",
                    market="crypto",
                    environment="paper",
                    status="closed",
                    direction="long",
                    timeframe="15m",
                    model="liquidity_sweep_reversal",
                    entry_time="2026-06-04T09:00:00Z",
                    exit_time="2026-06-04T11:00:00Z",
                    entry_price=100.0,
                    exit_price=110.0,
                    stop_price=95.0,
                    qty=2.0,
                    confluence_score=8.5,
                    emotion="calm",
                    rationale="Sweep then reclaim",
                    feature_vector={"liquidity_sweep": True},
                    dol_target={"type": "BSL", "level": 112.0},
                )
            )
        assert res["ok"] is True

        conn = app.get_db()
        row = conn.execute("SELECT * FROM smc_trade_journal").fetchone()
        conn.close()
        assert row["symbol"] == "BTCUSDT"
        assert row["pnl"] == 20.0
        assert row["r_multiple"] == 2.0
        assert row["environment"] == "paper"
    finally:
        app.DB = original


def test_api_smc_journal_summary_aggregates_closed_entries(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "_get_vault", return_value=None):
            app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="ETHUSDT",
                    environment="paper",
                    status="closed",
                    direction="long",
                    entry_price=50,
                    exit_price=55,
                    stop_price=47.5,
                    qty=2,
                    emotion="calm",
                    model="ote_ob_reversal",
                )
            )
            app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="ETHUSDT",
                    environment="paper",
                    status="closed",
                    direction="short",
                    entry_price=80,
                    exit_price=84,
                    stop_price=82,
                    qty=1,
                    emotion="anxious",
                    model="liquidity_sweep_reversal",
                )
            )
            app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="SOLUSDT",
                    environment="live",
                    status="open",
                    direction="long",
                    entry_price=140,
                    qty=1,
                    emotion="focused",
                    model="breaker_retest_continuation",
                )
            )

        payload = app.api_smc_journal_summary()
        summary = payload["summary"]
        assert summary["total_entries"] == 3
        assert summary["closed_entries"] == 2
        assert summary["open_entries"] == 1
        assert summary["win_rate"] == 0.5
        assert summary["environment_breakdown"]["paper"] == 2
        assert summary["environment_breakdown"]["live"] == 1
        assert summary["emotion_breakdown"]["calm"] == 1
        assert summary["top_models"][0]["model"] in {"ote_ob_reversal", "liquidity_sweep_reversal"}
    finally:
        app.DB = original


def test_load_trades_from_db_can_include_closed_journal_entries(tmp_path):
    original = _temp_db(tmp_path)
    try:
        with patch.object(app, "_get_vault", return_value=None):
            app.api_add_smc_journal(
                SMCJournalCreate(
                    symbol="XRPUSDT",
                    environment="paper",
                    status="closed",
                    direction="long",
                    entry_price=1.0,
                    exit_price=1.2,
                    stop_price=0.9,
                    qty=100.0,
                    model="ifvg_reversal",
                    emotion="calm",
                    feature_vector={"liquidity_sweep": True, "killzone": False},
                    dol_target={"type": "BSL", "level": 1.22},
                )
            )
        conn = app.get_db()
        df = load_trades_from_db(conn, symbol="XRPUSDT", include_journal=True)
        conn.close()
        assert len(df) == 1
        assert df.iloc[0]["sample_source"] == "journal"
        assert bool(df.iloc[0]["liquidity_sweep"]) is True
        assert df.iloc[0]["dol_type"] == "BSL"
    finally:
        app.DB = original


def test_obsidian_smc_journal_round_trip(tmp_path):
    original = _temp_db(tmp_path)
    try:
        journal = {
            "journal_key": "BTCUSDT-testjournal",
            "symbol": "BTCUSDT",
            "name": "Bitcoin",
            "market": "crypto",
            "environment": "paper",
            "status": "closed",
            "direction": "long",
            "timeframe": "15m",
            "model": "ote_ob_reversal",
            "entry_time": "2026-06-04T09:00:00Z",
            "exit_time": "2026-06-04T10:00:00Z",
            "entry_price": 100.0,
            "exit_price": 107.0,
            "stop_price": 96.5,
            "tp1_price": 108.0,
            "qty": 1.0,
            "pnl": 7.0,
            "r_multiple": 2.0,
            "confluence_score": 8.0,
            "emotion": "calm",
            "rationale": "Paper test",
            "notes": "Keep tracking",
            "screenshots": ["Screenshots/btc-before.png", "Screenshots/btc-after.png"],
            "tags": ["paper", "crypto"],
            "feature_vector": {"liquidity_sweep": True},
            "dol_target": {"type": "BSL", "level": 108.0},
            "created_at": "2026-06-04T09:00:00Z",
            "updated_at": "2026-06-04T10:05:00Z",
        }
        vault = tmp_path / "vault"
        app._obsidian_write_smc_journal(vault, journal)
        note = app._obsidian_smc_journal_note_path(vault, journal)
        assert note.exists()

        parsed = app._obsidian_parse_smc_journal(note)
        assert parsed["journal_key"] == journal["journal_key"]
        assert parsed["screenshots"][0] == "Screenshots/btc-before.png"

        conn = app.get_db()
        conn.execute("DELETE FROM smc_trade_journal")
        conn.commit()
        conn.close()
        result = app._obsidian_sync_smc_journal(vault)
        assert result["synced"] == 1

        conn = app.get_db()
        row = conn.execute("SELECT * FROM smc_trade_journal WHERE journal_key=?", (journal["journal_key"],)).fetchone()
        conn.close()
        assert row is not None
        assert row["emotion"] == "calm"
    finally:
        app.DB = original
