import pandas as pd
import pytest
import numpy as np

from crypto.adaptive_params import calculate_adaptive_params
from crypto.liquidations import detect_liquidation_clusters, confirm_liquidation_sweep
from crypto.cvd import detect_cvd_divergence, classify_order_flow
from crypto.cross_market import calculate_coinbase_premium, detect_cme_gaps, detect_cross_exchange_smt, align_altcoin_with_btc_bias
from smc_quant import build_smc_analysis, SMCConfig


@pytest.fixture
def base_crypto_df():
    # 65 bars to satisfy SMA60 and other swing filters
    base_time = pd.date_range("2026-06-01", periods=65, freq="h")
    closes = [100.0 + i * 0.1 for i in range(65)]
    # Create a swing high at index 40
    closes[40] = 115.0
    
    highs = [c + 0.5 for c in closes]
    highs[40] = 116.0
    
    lows = [c - 0.5 for c in closes]
    lows[40] = 114.0
    
    opens = [c - 0.1 for c in closes]
    
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000] * 65,
        "oi": [10000] * 65,
        "cvd": [5000] * 65,
        "funding_rate": [0.0001] * 65,
        "coinbase_premium": [0.02] * 65
    }, index=base_time)
    
    return df


def test_adaptive_params(base_crypto_df):
    params_btc = calculate_adaptive_params(base_crypto_df, "BTC/USDT")
    assert params_btc["asset_class"] == "major"
    assert params_btc["stop_atr_mult"] == 1.5
    assert 0.002 <= params_btc["range_percent_dyn"] <= 0.03
    
    params_alt = calculate_adaptive_params(base_crypto_df, "DOGE/USDT")
    assert params_alt["asset_class"] == "altcoin"
    assert params_alt["stop_atr_mult"] == 2.0


def test_liquidation_clusters(base_crypto_df):
    swings = [
        {"index": 40, "level": 116.0, "type": "high", "confirm_index": 45}
    ]
    clusters = detect_liquidation_clusters(base_crypto_df, swings)
    assert len(clusters) > 0
    assert clusters[0]["type"] == "BSL_LIQ"
    assert clusters[0]["level"] > 116.0


def test_confirm_liquidation_sweep():
    # Sweep low (long squeeze), OI drops by 3% (confirmed)
    ok, reason = confirm_liquidation_sweep(
        price=99.0,
        swing_level=100.0,
        direction=1,
        oi_change_pct=-0.03
    )
    assert ok is True
    assert "squeeze reversal" in reason

    # Sweep high (short squeeze), OI rises (not confirmed)
    ok2, reason2 = confirm_liquidation_sweep(
        price=101.0,
        swing_level=100.0,
        direction=-1,
        oi_change_pct=0.01
    )
    assert ok2 is False
    assert "insufficient OI drop" in reason2


def test_cvd_divergence():
    prices = pd.Series([10.0, 10.2, 10.4, 10.5, 10.3, 10.6, 10.8, 10.7, 10.9, 11.2, 11.0, 11.5])
    # CVD fails to follow the price to a higher high
    cvd = pd.Series([100, 105, 110, 112, 108, 115, 120, 118, 116, 114, 108, 110])
    
    div = detect_cvd_divergence(prices, cvd, lookback=5)
    assert div["bearish_cvd_divergence"] is True
    assert div["bullish_cvd_divergence"] is False
    
    # Classify flow
    flow = classify_order_flow(spot_cvd_change=150.0, perp_cvd_change=50.0)
    assert flow["spot_driven"] is True
    assert flow["source_type"] == "spot_genuine"


def test_cross_market():
    cb = pd.Series([100.1, 100.2], index=[0, 1])
    bn = pd.Series([100.0, 100.0], index=[0, 1])
    prem = calculate_coinbase_premium(cb, bn)
    assert prem.iloc[0] == pytest.approx(0.1)

    # CME Gaps
    base_time = pd.DatetimeIndex(["2026-06-05 16:00:00", "2026-06-06 17:00:00"]) # Friday to Saturday
    gap_df = pd.DataFrame({
        "open": [100.0, 105.0],
        "high": [101.0, 106.0],
        "low": [99.0, 104.0],
        "close": [100.0, 105.0]
    }, index=base_time)
    
    gaps = detect_cme_gaps(gap_df, threshold_pct=0.01)
    assert len(gaps) > 0
    assert gaps[0]["top"] == 105.0
    assert gaps[0]["bottom"] == 100.0

    # Cross exchange SMT
    binance = pd.DataFrame({"low": [10.0, 9.8], "high": [11.0, 11.0]})
    coinbase = pd.DataFrame({"low": [10.0, 10.1], "high": [11.0, 11.0]})
    smt = detect_cross_exchange_smt(binance, coinbase, lookback_bars=2)
    assert smt["bullish_exchange_smt"] is True

    # Alt align
    assert align_altcoin_with_btc_bias("SOL/USDT", "bullish", 1) is True
    assert align_altcoin_with_btc_bias("SOL/USDT", "bearish", 1) is False


def test_smc_quant_crypto_confluence(base_crypto_df):
    analysis = build_smc_analysis(
        base_crypto_df,
        symbol="BTC/USDT",
        timeframe="1h",
        config=SMCConfig(swing_length=2, internal_swing_length=2, entry_threshold=1)
    )
    assert analysis["market"] == "crypto"
    assert "signals" in analysis
    if analysis["signals"]:
        sig = analysis["signals"][0]
        # Verify factors are present and formatted
        factors = sig["factors"]
        factor_ids = [f["id"] for f in factors]
        assert "liquidation_cluster_sweep" in factor_ids
        assert "oi_squeeze_confirm" in factor_ids
        assert "cvd_divergence_confirm" in factor_ids
        assert "extreme_funding_rate" in factor_ids


def test_profile_cooldown_override():
    from smc_auto_workflow import profile_for_symbol
    profile = profile_for_symbol("BTC-USDT")
    assert profile.cooldown_minutes == 5

