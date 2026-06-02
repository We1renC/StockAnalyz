"""Tests for brokerage fee calculation, premium/discount, and annualized return."""
import app


def test_compute_fees_tw_buy_with_min_fee():
    cfg = {"tw_buy_fee_rate": 0.001425 * 0.6, "tw_sell_fee_rate": 0.001425 * 0.6,
           "tw_sell_tax_rate_stock": 0.003, "tw_sell_tax_rate_etf": 0.001, "tw_min_fee": 20}
    # 小單低於 min fee 應吃 min fee
    r = app._compute_fees(price=10, shares=100, currency="TWD", side="buy", fees_cfg=cfg, is_etf=False)
    assert r["fee"] == 20.0  # min fee kicks in
    assert r["tax"] == 0
    # 大單按比例
    r = app._compute_fees(price=500, shares=1000, currency="TWD", side="buy", fees_cfg=cfg, is_etf=False)
    assert round(r["fee"], 2) == round(500 * 1000 * 0.001425 * 0.6, 2)


def test_compute_fees_tw_sell_includes_tax():
    cfg = {"tw_sell_fee_rate": 0.001425 * 0.6, "tw_sell_tax_rate_stock": 0.003,
           "tw_sell_tax_rate_etf": 0.001, "tw_min_fee": 20}
    # 股票 0.3%
    r = app._compute_fees(price=100, shares=1000, currency="TWD", side="sell", fees_cfg=cfg, is_etf=False)
    assert round(r["tax"], 2) == round(100 * 1000 * 0.003, 2)
    # ETF 0.1%
    r = app._compute_fees(price=100, shares=1000, currency="TWD", side="sell", fees_cfg=cfg, is_etf=True)
    assert round(r["tax"], 2) == round(100 * 1000 * 0.001, 2)


def test_compute_fees_us_min_fee():
    cfg = {"us_fee_rate": 0.005, "us_min_fee": 39.9, "us_sec_fee_rate": 0.0000278}
    # 小單買進吃 min
    r = app._compute_fees(price=10, shares=5, currency="USD", side="buy", fees_cfg=cfg, is_etf=False)
    assert r["fee"] == 39.9
    assert r["tax"] == 0
    # 賣出多收 SEC 規費
    r = app._compute_fees(price=200, shares=100, currency="USD", side="sell", fees_cfg=cfg, is_etf=False)
    assert r["fee"] == max(200 * 100 * 0.005, 39.9)
    assert r["tax"] > 0


def test_annualized_return_positive():
    # 1 年 +10% → 大約 10%
    assert app._annualized_return(10.0, 365) == 10.0
    # 半年 +10% → ~21%
    annual = app._annualized_return(10.0, 182)
    assert annual is not None and 20.5 < annual < 22.0


def test_annualized_return_negative_clamped():
    # 完全虧光 → -100%
    assert app._annualized_return(-100.0, 365) == -100.0
    # 短於 7 天回 None（過短不計）
    assert app._annualized_return(5.0, 3) is None


def test_is_etf_symbol():
    assert app._is_etf_symbol("0050.TW") is True
    assert app._is_etf_symbol("00713.TW") is True
    assert app._is_etf_symbol("2330.TW") is False
    assert app._is_etf_symbol("AAPL") is False
    # quoteType override
    assert app._is_etf_symbol("SPY", quote_type="ETF") is True
    assert app._is_etf_symbol("AAPL", quote_type="EQUITY") is False


def test_brokerage_presets_loaded_in_settings():
    from llm_providers import BROKERAGE_PRESETS
    assert "default_60_discount" in BROKERAGE_PRESETS
    assert "fubon_proxy" in BROKERAGE_PRESETS
    assert "ibkr_tiered" in BROKERAGE_PRESETS
    assert "firstrade" in BROKERAGE_PRESETS
    # 第一證券免手續費
    assert BROKERAGE_PRESETS["firstrade"]["us_fee_rate"] == 0.0
