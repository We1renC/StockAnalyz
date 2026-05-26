"""Offline tests for fetch_indicators 並行 yfinance + TWSE 邏輯.

關鍵測試目標：
1. 美股只走 yfinance（沒有 TWSE channel）
2. 台股 yfinance 與 TWSE 都成功時 → 取 yfinance 的 RSI/MA + TWSE 的即時 price
3. 台股 yfinance 失敗、TWSE 成功 → 仍可拿到 price 但 RSI=None（fallback only）
4. 台股 yfinance 成功、TWSE 失敗 → 完整 yfinance 指標（不會 crash）
5. 兩者都失敗 → 回空 dict（不 raise）
6. fetch_tw_realtime_quote 在網路錯誤時應回 {} 不 raise
7. _twse_channel 處理 .TW / .TWO / 美股 三種情境
"""
import pytest
from unittest.mock import patch
import json
from urllib.error import URLError

import app  # type: ignore  (resolved via conftest path injection)


# ──────────────────────── helpers ────────────────────────
YF_FULL_TW_2883 = {
    "price": 22.78,
    "change_1d": 0.5,
    "change_1m": 5.0,
    "rsi": 73.0,
    "ma20": 21.22,
    "ma60": 20.35,
    "high52": 22.9,
    "low52": 14.7,
    "beta": 0.55,
    "source": "yfinance",
}

TWSE_REALTIME_2883 = {
    "price": 22.95,
    "change_1d": 0.7,
    "change_1m": None,
    "rsi": None,
    "ma20": None,
    "ma60": None,
    "high52": 23.0,  # 當日高
    "low52": 22.5,   # 當日低
    "beta": None,
    "source": "twse_realtime",
}


# ──────────────────────── _twse_channel ────────────────────────
def test_twse_channel_for_tse():
    assert app._twse_channel("2883.TW") == "tse_2883.tw"

def test_twse_channel_for_otc():
    assert app._twse_channel("6469.TWO") == "otc_6469.tw"

def test_twse_channel_returns_none_for_us():
    assert app._twse_channel("NVDA") is None

def test_twse_channel_returns_none_for_unknown():
    assert app._twse_channel("UNKNOWN") is None


# ──────────────────────── _safe_float ────────────────────────
def test_safe_float_normal():
    assert app._safe_float("22.78") == 22.78

def test_safe_float_with_comma():
    assert app._safe_float("1,234.56") == 1234.56

def test_safe_float_empty():
    assert app._safe_float("") is None
    assert app._safe_float("-") is None
    assert app._safe_float(None) is None
    assert app._safe_float("---") is None

def test_safe_float_invalid():
    assert app._safe_float("abc") is None


# ──────────────────────── fetch_indicators 並行邏輯 ────────────────────────
def test_us_stock_uses_yfinance_only():
    """美股沒有 TWSE channel，應只走 yfinance"""
    with patch.object(app, "fetch_yfinance_indicators", return_value=YF_FULL_TW_2883), \
         patch.object(app, "fetch_tw_realtime_quote") as mock_tw:
        result = app.fetch_indicators("NVDA")
        assert result["source"] == "yfinance"
        assert result["rsi"] == 73.0
        mock_tw.assert_not_called()


def test_tw_stock_merges_yfinance_indicators_with_twse_realtime_price():
    """台股：yfinance 完整 + TWSE 即時 → 應該 RSI/MA 來自 yfinance、price 用 TWSE 覆蓋"""
    with patch.object(app, "fetch_yfinance_indicators", return_value=dict(YF_FULL_TW_2883)), \
         patch.object(app, "fetch_tw_realtime_quote", return_value=dict(TWSE_REALTIME_2883)):
        result = app.fetch_indicators("2883.TW")

    # 即時 price 優先
    assert result["price"] == 22.95, "TWSE 即時價應覆蓋 yfinance"
    assert result["change_1d"] == 0.7, "TWSE 日漲跌應覆蓋"

    # yfinance 歷史指標保留
    assert result["rsi"] == 73.0, "RSI 應來自 yfinance"
    assert result["ma20"] == 21.22, "MA20 應來自 yfinance"
    assert result["ma60"] == 20.35, "MA60 應來自 yfinance"
    assert result["beta"] == 0.55, "Beta 應來自 yfinance"

    # 52 週高低應保留 yfinance 的（TWSE 只給當日高低）
    assert result["high52"] == 22.9
    assert result["low52"] == 14.7

    # source 應標示融合來源
    assert result["source"] == "twse_realtime+yfinance"


def test_tw_stock_yfinance_fails_falls_back_to_twse_only():
    """yfinance 拿不到資料、TWSE OK → 回 TWSE 數據（RSI 等為 None）"""
    with patch.object(app, "fetch_yfinance_indicators", return_value={}), \
         patch.object(app, "fetch_official_tw_indicators", return_value={}), \
         patch.object(app, "fetch_tw_realtime_quote", return_value=dict(TWSE_REALTIME_2883)):
        result = app.fetch_indicators("009819.TW")

    assert result["price"] == 22.95
    assert result["change_1d"] == 0.7
    assert result["rsi"] is None
    assert result["ma20"] is None
    assert result["source"] == "twse_realtime"  # 沒有 yfinance 部分


def test_tw_stock_yfinance_fails_uses_official_daily_indicators_plus_realtime():
    """yfinance 失敗，但官方日線 OK → RSI/MA 來自官方日線，price 用即時價覆蓋"""
    official_daily = dict(YF_FULL_TW_2883)
    official_daily["source"] = "twse_daily"
    with patch.object(app, "fetch_yfinance_indicators", return_value={}), \
         patch.object(app, "fetch_official_tw_indicators", return_value=official_daily), \
         patch.object(app, "fetch_tw_realtime_quote", return_value=dict(TWSE_REALTIME_2883)):
        result = app.fetch_indicators("009819.TW")

    assert result["price"] == 22.95
    assert result["rsi"] == 73.0
    assert result["ma20"] == 21.22
    assert result["source"] == "twse_realtime+twse_daily"


def test_tw_stock_twse_fails_falls_back_to_yfinance():
    """yfinance OK、TWSE 失敗 → 應回完整 yfinance 結果不 crash"""
    with patch.object(app, "fetch_yfinance_indicators", return_value=dict(YF_FULL_TW_2883)), \
         patch.object(app, "fetch_tw_realtime_quote", return_value={}):
        result = app.fetch_indicators("2883.TW")

    # 該回 yfinance 全部結果
    assert result["price"] == 22.78
    assert result["rsi"] == 73.0
    assert result["source"] == "yfinance"


def test_tw_stock_both_fail_returns_empty():
    """yfinance 與 TWSE 都失敗 → 應回 {} 不 raise"""
    with patch.object(app, "fetch_yfinance_indicators", return_value={}), \
         patch.object(app, "fetch_tw_realtime_quote", return_value={}):
        result = app.fetch_indicators("9999.TW")
    assert result == {}


# ──────────────────────── fetch_tw_realtime_quote 韌性測試 ────────────────────────
def test_tw_realtime_handles_url_error():
    """網路錯誤時應回 {} 不 raise"""
    with patch.object(app, "urlopen", side_effect=URLError("network down")):
        result = app.fetch_tw_realtime_quote("2883.TW")
    assert result == {}


def test_tw_realtime_handles_timeout():
    with patch.object(app, "urlopen", side_effect=TimeoutError("slow")):
        result = app.fetch_tw_realtime_quote("2883.TW")
    assert result == {}


def test_tw_realtime_handles_invalid_json():
    """API 回非 JSON 時應回 {} 不 raise"""
    class FakeResp:
        def read(self):
            return b"not json at all"
        def __enter__(self): return self
        def __exit__(self, *a): pass
    with patch.object(app, "urlopen", return_value=FakeResp()):
        result = app.fetch_tw_realtime_quote("2883.TW")
    assert result == {}


def test_tw_realtime_handles_empty_msg_array():
    """API 回空 msgArray (盤後/週末) 應回 {}"""
    class FakeResp:
        def read(self):
            return json.dumps({"msgArray": []}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    with patch.object(app, "urlopen", return_value=FakeResp()):
        result = app.fetch_tw_realtime_quote("2883.TW")
    assert result == {}


def test_tw_realtime_parses_real_format():
    """模擬 TWSE mis 真實回傳格式"""
    fake_payload = {
        "msgArray": [{
            "z": "22.95",   # 當前成交價
            "y": "22.50",   # 昨收
            "h": "23.00",   # 當日最高
            "l": "22.40",   # 當日最低
            "pz": "22.95",  # 五檔成交
        }]
    }
    class FakeResp:
        def read(self):
            return json.dumps(fake_payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch.object(app, "urlopen", return_value=FakeResp()):
        result = app.fetch_tw_realtime_quote("2883.TW")

    assert result["price"] == 22.95
    assert result["change_1d"] == pytest.approx(2.0, abs=0.1)  # (22.95/22.50-1)*100 ≈ 2.0
    assert result["high52"] == 23.0
    assert result["low52"] == 22.4
    assert result["source"] == "twse_realtime"


def test_tw_realtime_skip_when_no_price():
    """價格欄都是 '-' 應回 {}"""
    fake_payload = {
        "msgArray": [{"z": "-", "y": "-", "pz": "-"}]
    }
    class FakeResp:
        def read(self):
            return json.dumps(fake_payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch.object(app, "urlopen", return_value=FakeResp()):
        result = app.fetch_tw_realtime_quote("2883.TW")
    assert result == {}


# ──────────────────────── store_price_cache merge 邏輯 ────────────────────────
def test_store_price_cache_preserves_existing_indicators():
    """新資料缺 RSI 時，merge 應保留舊快取的 RSI 值"""
    import sqlite3, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # 用真 SQLite 建測試資料庫
        original_db = app.DB
        app.DB = path
        app.init_db()
        conn = app.get_db()
        c = conn.cursor()

        # 第一次寫入：完整 yfinance 數據
        app.store_price_cache(c, "2883.TW", {
            "price": 22.78, "rsi": 73.0, "ma20": 21.22, "source": "yfinance"
        })
        conn.commit()

        # 第二次寫入：只有 TWSE 的 price (RSI=None 等於沒提供)
        app.store_price_cache(c, "2883.TW", {
            "price": 22.95, "change_1d": 0.7, "source": "twse_realtime"
        })
        conn.commit()

        row = c.execute("SELECT * FROM price_cache WHERE symbol=?", ("2883.TW",)).fetchone()
        conn.close()

        assert row["price"] == 22.95, "新 price 應覆蓋"
        assert row["change_1d"] == 0.7, "新 change_1d 應覆蓋"
        assert row["rsi"] == 73.0, "舊 RSI 應保留（merge 邏輯）"
        assert row["ma20"] == 21.22, "舊 MA20 應保留"
    finally:
        app.DB = original_db
        os.unlink(path)
