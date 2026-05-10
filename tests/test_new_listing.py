"""Tests for SSL context + new-listing partial yfinance schema (009819 fix)."""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
import ssl

import app  # type: ignore


# ──────────────────────── SSL context ────────────────────────
def test_ssl_context_uses_certifi():
    """SSL_CONTEXT should be a real SSLContext loaded with certifi CA bundle."""
    assert isinstance(app.SSL_CONTEXT, ssl.SSLContext)
    # Should have at least one CA loaded (certifi has thousands)
    assert len(app.SSL_CONTEXT.get_ca_certs()) > 100


def test_fetch_tw_realtime_uses_ssl_context():
    """fetch_tw_realtime_quote should pass SSL_CONTEXT to urlopen."""
    captured = {}

    def fake_urlopen(req, timeout, context=None):
        captured["context"] = context
        captured["timeout"] = timeout
        # Return a fake response that mimics urlopen's contextmanager
        class R:
            def read(self):
                import json as _j
                return _j.dumps({"msgArray": [{"z": "10.12", "y": "10.0"}]}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return R()

    with patch.object(app, "urlopen", side_effect=fake_urlopen):
        result = app.fetch_tw_realtime_quote("2883.TW")

    assert captured["context"] is app.SSL_CONTEXT, "should use module-level SSL_CONTEXT"
    assert captured["timeout"] == 8
    assert result["price"] == 10.12


# ──────────────────────── Partial yfinance schema ────────────────────────
def _make_history(days: int):
    """Build a fake yfinance-style DataFrame with N trading days."""
    dates = pd.date_range(end="2026-05-08", periods=days, freq="B", tz="America/New_York")
    closes = np.linspace(10, 10 + days * 0.1, days)
    return pd.DataFrame({
        "Open": closes - 0.05,
        "High": closes + 0.1,
        "Low": closes - 0.1,
        "Close": closes,
        "Volume": [1000] * days,
    }, index=dates)


def _patched_yf(history_df):
    """Make a fake yf.Ticker that returns history_df."""
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = history_df
    return mock_ticker


def test_yfinance_partial_for_new_listing_18_days():
    """18 trading days (like 009819 fresh IPO):
       price/change_1d/change_1m=N(>21)/rsi(>=14)/high52/low52 should exist;
       ma20 / ma60 / beta should be None instead of return {}."""
    h = _make_history(18)
    with patch.object(app.yf, "Ticker", return_value=_patched_yf(h)):
        result = app.fetch_yfinance_indicators("009819.TW")

    assert result, "18 days should NOT return empty dict (was the original bug)"
    assert result["price"] is not None
    assert result["change_1d"] is not None
    assert result["rsi"] is not None, "14 days enough for RSI"
    assert result["ma20"] is None, "<20 days no MA20"
    assert result["ma60"] is None, "<60 days no MA60"
    assert result["beta"] is None, "<20 days no beta"
    assert result["high52"] is not None
    assert result["low52"] is not None
    assert result["change_1m"] is None, "<22 days no change_1m"
    assert result["source"] == "yfinance"


def test_yfinance_partial_for_5_days():
    """Even with only 5 days we should get price/high/low; RSI/MA all None."""
    h = _make_history(5)
    with patch.object(app.yf, "Ticker", return_value=_patched_yf(h)):
        result = app.fetch_yfinance_indicators("NEW.TW")

    assert result["price"] is not None
    assert result["change_1d"] is not None
    assert result["rsi"] is None
    assert result["ma20"] is None
    assert result["ma60"] is None
    assert result["high52"] is not None
    assert result["low52"] is not None


def test_yfinance_full_schema_for_60_days():
    """60+ days should fill all indicators including ma60."""
    h = _make_history(120)
    with patch.object(app.yf, "Ticker", return_value=_patched_yf(h)):
        result = app.fetch_yfinance_indicators("OLD.TW")

    assert result["price"] is not None
    assert result["rsi"] is not None
    assert result["ma20"] is not None
    assert result["ma60"] is not None
    assert result["high52"] is not None


def test_yfinance_returns_empty_when_zero_history():
    """Truly delisted: 0 rows → still return {} so caller can fall back."""
    h = _make_history(0)
    with patch.object(app.yf, "Ticker", return_value=_patched_yf(h)):
        result = app.fetch_yfinance_indicators("DELISTED")
    assert result == {}


# ──────────────────────── Integration: fetch_indicators with new listing ────────────────────────
def test_009819_scenario_yfinance_partial_plus_twse_realtime():
    """Reproduce 009819 scenario: yfinance has 18 days (partial), TWSE has price.
       fetch_indicators should merge into a useful result."""
    h = _make_history(18)
    twse_payload = {
        "price": 10.15,
        "change_1d": 0.3,
        "change_1m": None, "rsi": None, "ma20": None, "ma60": None,
        "high52": 10.2, "low52": 10.05,
        "beta": None,
        "source": "twse_realtime",
    }
    with patch.object(app.yf, "Ticker", return_value=_patched_yf(h)), \
         patch.object(app, "fetch_tw_realtime_quote", return_value=twse_payload):
        result = app.fetch_indicators("009819.TW")

    # TWSE realtime price wins
    assert result["price"] == 10.15
    assert result["change_1d"] == 0.3
    # yfinance partial RSI survives
    assert result["rsi"] is not None, "RSI from yfinance partial should be present"
    # MA still None (18 < 20)
    assert result["ma20"] is None
    assert result["source"] == "twse_realtime+yfinance"
