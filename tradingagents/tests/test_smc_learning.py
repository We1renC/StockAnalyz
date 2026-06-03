import sqlite3
import pandas as pd
import pytest

from learning.trade_store import load_trades_from_db
from learning.regime import classify_market_regime
from learning.attribution import generate_attribution_report, calculate_expectancy
from learning.feature_importance import calculate_feature_importance
from learning.cross_val import purged_train_test_split, calculate_sharpe_ratio, estimate_backtest_overfitting
from learning.calibration import calibrate_confluence_weights, calculate_kelly_fraction
from learning.decay_monitor import detect_edge_decay


@pytest.fixture
def sample_trades_df():
    # 10 trade records
    data = {
        "id": list(range(1, 11)),
        "symbol": ["AAPL"] * 10,
        "direction": ["long", "short", "long", "long", "short", "long", "short", "long", "long", "short"],
        "model": ["OTE", "Sweep", "OTE", "Unicorn", "Sweep", "OTE", "Sweep", "Unicorn", "OTE", "Sweep"],
        "entry_time": [
            "2026-06-01T09:30:00", "2026-06-01T14:00:00", "2026-06-02T10:00:00",
            "2026-06-02T15:00:00", "2026-06-03T11:00:00", "2026-06-03T16:00:00",
            "2026-06-04T09:30:00", "2026-06-04T13:00:00", "2026-06-05T10:30:00",
            "2026-06-05T15:30:00"
        ],
        "exit_time": [
            "2026-06-01T11:30:00", "2026-06-01T15:00:00", "2026-06-02T12:00:00",
            "2026-06-02T16:30:00", "2026-06-03T13:00:00", "2026-06-03T17:30:00",
            "2026-06-04T11:00:00", "2026-06-04T15:00:00", "2026-06-05T12:30:00",
            "2026-06-05T17:00:00"
        ],
        "entry_price": [100.0, 105.0, 102.0, 104.0, 103.0, 106.0, 107.0, 108.0, 109.0, 110.0],
        "exit_price": [105.0, 103.0, 99.0, 108.0, 105.0, 102.0, 105.0, 112.0, 113.0, 112.0],
        "stop_price": [95.0, 107.0, 99.0, 100.0, 105.0, 103.0, 109.0, 104.0, 106.0, 112.0],
        "tp1_price": [105.0, 101.0, 108.0, 108.0, 99.0, 112.0, 103.0, 112.0, 115.0, 106.0],
        "pnl": [50.0, 20.0, -30.0, 40.0, -20.0, -40.0, 20.0, 40.0, 40.0, -20.0],
        "r_multiple": [1.0, 1.0, -1.0, 1.0, -1.0, -1.33, 1.0, 1.0, 1.33, -1.0],
        "win": [1, 1, 0, 1, 0, 0, 1, 1, 1, 0],
        "mae": [-1.0, -0.5, -3.0, -1.5, -2.0, -4.0, -1.0, -0.5, -0.5, -2.0],
        "mfe": [6.0, 2.5, 0.5, 4.5, 0.2, 0.8, 2.5, 4.5, 4.2, 0.1],
        "htf_bias_alignment": [True, True, False, True, False, False, True, True, True, False],
        "ote_zone": [True, False, True, False, False, True, False, False, True, False],
        "killzone": [True, True, True, True, True, True, True, True, True, True],
    }
    return pd.DataFrame(data)


def test_load_trades_from_db(tmp_path):
    db_path = tmp_path / "test_trades.db"
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE smc_backtest_trades (
        id INTEGER PRIMARY KEY,
        symbol TEXT,
        direction TEXT,
        model TEXT,
        entry_time TEXT,
        exit_time TEXT,
        entry_price REAL,
        exit_price REAL,
        stop_price REAL,
        tp1_price REAL,
        pnl REAL,
        r_multiple REAL,
        win INTEGER,
        feature_vector TEXT,
        dol_target TEXT,
        exit_reason TEXT,
        holding_bars INTEGER,
        mae REAL,
        mfe REAL
    )
    """)
    c.execute("""
    INSERT INTO smc_backtest_trades 
    (symbol, direction, model, entry_time, exit_time, entry_price, exit_price, stop_price, tp1_price, pnl, r_multiple, win, feature_vector, dol_target, mae, mfe)
    VALUES 
    ('AAPL', 'long', 'OTE', '2026-06-01T09:30:00', '2026-06-01T11:30:00', 100.0, 105.0, 95.0, 105.0, 50.0, 1.0, 1, 
     '{"htf_bias_alignment": true, "ote_zone": true}', '{"type": "PDH", "level": 105.0, "distance_pct": 0.0}', -1.0, 6.0)
    """)
    conn.commit()

    df = load_trades_from_db(conn)
    conn.close()

    assert not df.empty
    assert len(df) == 1
    assert "htf_bias_alignment" in df.columns
    assert "ote_zone" in df.columns
    assert df.loc[0, "htf_bias_alignment"] == True
    assert df.loc[0, "ote_zone"] == True
    assert df.loc[0, "dol_type"] == "PDH"


def test_classify_market_regime():
    # Construct synthetic K-line data
    base = pd.date_range("2026-01-01", periods=100)
    # Trending bullish
    closes = [100.0 + i for i in range(100)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    opens = [c - 0.2 for c in closes]
    
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000] * 100
    }, index=base)
    
    regime = classify_market_regime(df)
    assert regime["regime_trend"] == "trending_bullish"
    assert regime["is_trending"] is True
    assert regime["regime_volatility"] in ("high", "normal", "low")


def test_attribution_metrics(sample_trades_df):
    report = generate_attribution_report(sample_trades_df)
    assert report["total_trades"] == 10
    assert report["overall"]["win_rate"] == 0.6
    assert report["overall"]["expected_r"] == pytest.approx(0.2)
    
    # Check factor expectancy
    assert "htf_bias_alignment" in report["factors"]
    assert report["factors"]["htf_bias_alignment"]["count"] == 6
    assert report["factors"]["htf_bias_alignment"]["win_rate"] == pytest.approx(1.0)
    
    # Check MAE/MFE recommendations
    assert "stop_loss" in report["mae_mfe_recommendations"]
    assert "profit_taking" in report["mae_mfe_recommendations"]


def test_feature_importance(sample_trades_df):
    res = calculate_feature_importance(sample_trades_df)
    assert "importances" in res
    assert len(res["importances"]) > 0
    assert res["method"] in ("logistic_regression", "statistical_correlation")


def test_cross_validation_purged(sample_trades_df):
    train_df, test_df = purged_train_test_split(sample_trades_df, train_pct=0.7, purge_hours=2.0)
    assert len(train_df) == 7
    # Verify that test entries are after train exits + safety margin
    train_exits = pd.to_datetime(train_df["exit_time"])
    max_train_exit = train_exits.max()
    
    if not test_df.empty:
        test_entries = pd.to_datetime(test_df["entry_time"])
        for entry in test_entries:
            assert entry >= max_train_exit + pd.Timedelta(hours=2.0)
            
    # Sharpe ratio
    sharpe = calculate_sharpe_ratio(sample_trades_df["r_multiple"])
    assert isinstance(sharpe, float)


def test_calibration(sample_trades_df):
    report = generate_attribution_report(sample_trades_df)
    proposal = calibrate_confluence_weights(report)
    assert "proposed_weights" in proposal
    assert "changes" in proposal
    
    # Kelly
    kelly_cap = calculate_kelly_fraction(
        win_rate=0.6,
        avg_win_pnl=40.0,
        avg_loss_pnl=22.5,
        fraction=0.25
    )
    assert 0.005 <= kelly_cap <= 0.030


def test_decay_monitor(sample_trades_df):
    # Overall is positive, recent is negative
    decay_df = sample_trades_df.copy()
    # Modify the last 4 trades to be huge losses
    decay_df.loc[6:, "r_multiple"] = -2.0
    decay_df.loc[6:, "win"] = 0
    decay_df.loc[6:, "pnl"] = -100.0
    
    res = detect_edge_decay(decay_df, window_size=4)
    assert res["is_decaying"] is True
    assert "Edge decay detected!" in res["warning_message"]


def test_attribution_handles_missing_mae_mfe(sample_trades_df):
    df = sample_trades_df.copy()
    df.loc[0, "mae"] = None
    df.loc[1, "mfe"] = None
    report = generate_attribution_report(df)
    assert report["total_trades"] == 10
    assert "overall" in report
    assert isinstance(report["mae_mfe_recommendations"], dict)
