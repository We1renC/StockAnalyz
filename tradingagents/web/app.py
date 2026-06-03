#!/usr/bin/env python3
"""TradingAgents Portfolio Dashboard — FastAPI single-file app."""

import asyncio
import json
import math
import sqlite3
import ssl
import time
import warnings
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi

import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from llm_providers import (
    load_settings, save_settings, mask_key,
    run_workflow, AVAILABLE_MODELS, detect_cli_availability,
    call_llm, call_cli,
)
from technical_matrix import build_technical_matrix
from smc_quant import SMCConfig, build_smc_analysis
from smc_backtest import SMCBacktestConfig, run_smc_event_backtest

warnings.filterwarnings("ignore")

def sanitize_float_values(obj):
    """Recursively replace float('nan'), float('inf'), and -float('inf') with None in dictionaries and lists."""
    if isinstance(obj, dict):
        return {k: sanitize_float_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_float_values(v) for v in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    elif isinstance(obj, (np.floating, np.integer)):
        if np.isnan(obj):
            return None
    return obj

BASE = Path(__file__).parent
DB = BASE / "portfolio.db"
USD_TWD = 32.0  # 預估匯率 (簡化)
TWSE_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"

# SSL context for HTTPS calls (TWSE, etc.).
#
# 兩個常見問題在 Python 3.13 + macOS 環境：
#   1. 內建 CA 鏈不全 → 用 certifi 補
#   2. 某些公部門網站證書缺 Subject Key Identifier extension（如 mis.twse.com.tw），
#      Python 3.13 / OpenSSL 3.x 嚴格模式會擋下，需關掉 VERIFY_X509_STRICT。
#      仍保留 hostname 驗證與 CA 驗證，安全性影響極小。
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT

# ─────────────── Database ───────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        name TEXT,
        category TEXT,
        shares REAL NOT NULL,
        cost_price REAL NOT NULL,
        currency TEXT DEFAULT 'TWD',
        purchase_date TEXT,
        target_entry REAL,
        target_profit REAL,
        target_stop REAL
    );
    CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        name TEXT,
        category TEXT,
        currency TEXT DEFAULT 'TWD',
        target_entry REAL,
        target_add REAL,
        target_profit REAL,
        target_stop REAL,
        notes TEXT
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        level TEXT NOT NULL,
        type TEXT NOT NULL,
        message TEXT,
        price REAL,
        diagnosis TEXT,
        acknowledged INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS market_state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        ts TEXT,
        vix REAL,
        twii REAL,
        twii_ma60 REAL,
        spx REAL,
        spx_ma60 REAL,
        risk_level TEXT,
        warnings_count INTEGER
    );
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        date TEXT NOT NULL,
        zone TEXT NOT NULL,
        total_cost REAL,
        total_value REAL,
        total_pnl REAL,
        total_net_pnl REAL,
        position_count INTEGER,
        PRIMARY KEY (date, zone)
    );
    CREATE INDEX IF NOT EXISTS idx_snapshots_date ON portfolio_snapshots(date);
    CREATE TABLE IF NOT EXISTS portfolio_intraday_snapshots (
        ts TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        zone TEXT NOT NULL,
        total_cost REAL,
        total_value REAL,
        total_pnl REAL,
        total_net_pnl REAL,
        position_count INTEGER,
        PRIMARY KEY (ts, zone)
    );
    CREATE INDEX IF NOT EXISTS idx_intraday_snapshots_date_zone
        ON portfolio_intraday_snapshots(trade_date, zone, ts);
    CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        data TEXT,
        PRIMARY KEY (symbol, date)
    );
    CREATE INDEX IF NOT EXISTS idx_fundamentals_symbol ON fundamentals_snapshots(symbol, date);
    CREATE TABLE IF NOT EXISTS technical_matrix_snapshots (
        symbol TEXT NOT NULL,
        date TEXT NOT NULL,
        period TEXT NOT NULL,
        bias TEXT,
        net_score REAL,
        confidence REAL,
        risk_level TEXT,
        data TEXT,
        PRIMARY KEY (symbol, date, period)
    );
    CREATE INDEX IF NOT EXISTS idx_matrix_symbol ON technical_matrix_snapshots(symbol, date);
    CREATE TABLE IF NOT EXISTS financial_reports (
        symbol TEXT NOT NULL,
        period TEXT NOT NULL,
        period_type TEXT NOT NULL DEFAULT 'quarter',
        data TEXT,
        PRIMARY KEY (symbol, period, period_type)
    );
    CREATE INDEX IF NOT EXISTS idx_financials_symbol ON financial_reports(symbol, period);
    CREATE TABLE IF NOT EXISTS price_cache (
        symbol TEXT PRIMARY KEY,
        ts TEXT,
        price REAL,
        rsi REAL,
        ma20 REAL,
        ma60 REAL,
        high52 REAL,
        low52 REAL,
        change_1d REAL,
        change_1m REAL,
        beta REAL,
        nav REAL,
        pb REAL,
        quote_type TEXT,
        data TEXT
    );
    CREATE TABLE IF NOT EXISTS analysis_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        name TEXT,
        ts TEXT NOT NULL,
        mode TEXT,
        provider TEXT,
        model TEXT,
        elapsed REAL,
        decision_summary TEXT,
        sections TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_analysis_symbol_ts ON analysis_results(symbol, ts DESC);
    CREATE TABLE IF NOT EXISTS domain_research (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT NOT NULL,
        ts TEXT NOT NULL,
        frontier_stocks TEXT,
        leading_stocks TEXT,
        analyst_report TEXT,
        reviewer_report TEXT,
        obsidian_path TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_domain_research_ts ON domain_research(ts DESC);
    CREATE TABLE IF NOT EXISTS trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        name        TEXT DEFAULT '',
        action      TEXT NOT NULL CHECK(action IN ('buy','sell')),
        shares      REAL NOT NULL,
        price       REAL NOT NULL,
        fee         REAL DEFAULT 0,
        tax         REAL DEFAULT 0,
        trade_date  TEXT NOT NULL,
        settle_date TEXT,
        currency    TEXT DEFAULT 'TWD',
        notes       TEXT DEFAULT '',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
    CREATE INDEX IF NOT EXISTS idx_trades_date   ON trades(trade_date DESC);
    """)
    conn.commit()

    # Migration: positions target columns
    c = conn.cursor()
    for col in ("target_entry", "target_profit", "target_stop"):
        try:
            c.execute(f"ALTER TABLE positions ADD COLUMN {col} REAL")
            conn.commit()
        except Exception:
            pass

    # Migration: market_state extended indicators
    for col in ("sox", "sox_ma60", "ndx", "ndx_ma20", "tnx", "dxy", "hsntech",
                "twii_ma20", "twii_ma120", "spx_ma20", "spx_ma120"):
        try:
            c.execute(f"ALTER TABLE market_state ADD COLUMN {col} REAL")
            conn.commit()
        except Exception:
            pass

    # Migration: price_cache structured meta fields
    for col_def in ("nav REAL", "pb REAL", "quote_type TEXT"):
        try:
            c.execute(f"ALTER TABLE price_cache ADD COLUMN {col_def}")
            conn.commit()
        except Exception:
            pass

    # Migration: trades table (for pre-existing DBs)
    for col_def in (
        "name TEXT DEFAULT ''",
        "fee REAL DEFAULT 0",
        "tax REAL DEFAULT 0",
        "settle_date TEXT",
        "notes TEXT DEFAULT ''",
    ):
        try:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
            conn.commit()
        except Exception:
            pass

    conn.close()

# ─────────────── Price + Indicators ───────────────
def _safe_float(value):
    if value in (None, "", "-", "--", "---", "----", "null"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _normalize_purchase_date(value) -> Optional[str]:
    if value in (None, "", "-", "--"):
        return None
    text = str(value).strip().replace("/", "-")
    try:
        if "T" in text:
            return datetime.fromisoformat(text).date().isoformat()
        parts = text.split("-")
        if len(parts) == 3:
            y, m, d = (int(parts[0]), int(parts[1]), int(parts[2]))
            return date(y, m, d).isoformat()
    except Exception:
        return None
    return None


def _is_test_symbol(symbol: str, name: str = "", category: str = "") -> bool:
    symbol_upper = (symbol or "").strip().upper()
    name_text = (name or "").strip()
    category_text = (category or "").strip()
    return (
        symbol_upper.startswith("TEST")
        or "測試" in name_text
        or "測試" in category_text
    )

def _twse_channel(symbol: str) -> Optional[str]:
    if symbol.endswith(".TW"):
        return f"tse_{symbol[:-3]}.tw"
    if symbol.endswith(".TWO"):
        return f"otc_{symbol[:-4]}.tw"
    return None

def _tw_symbol_code(symbol: str) -> Optional[str]:
    if symbol.endswith(".TW"):
        return symbol[:-3]
    if symbol.endswith(".TWO"):
        return symbol[:-4]
    return None

def _month_starts(months: int) -> list[date]:
    today = date.today()
    starts = []
    y, m = today.year, today.month
    for _ in range(months):
        starts.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return starts

def _parse_tw_date(value: str) -> Optional[pd.Timestamp]:
    value = str(value).strip()
    if not value:
        return None
    try:
        parts = value.split("/")
        if len(parts) == 3 and len(parts[0]) <= 3:
            year = int(parts[0]) + 1911
            return pd.Timestamp(year=year, month=int(parts[1]), day=int(parts[2]))
        return pd.Timestamp(value)
    except Exception:
        return None

def fetch_tw_realtime_quote(symbol: str) -> dict:
    """Fallback quote source for Taiwan symbols via TWSE/TPEX realtime feed.

    Returns empty dict on any failure (network/timeout/parse error) so caller
    can gracefully fall back to other sources without raising.
    """
    channel = _twse_channel(symbol)
    if not channel:
        return {}

    query = urlencode({
        "ex_ch": channel,
        "json": "1",
        "delay": "0",
        "_": int(datetime.now().timestamp() * 1000),
    })
    req = Request(
        f"{TWSE_MIS_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        },
    )
    try:
        with urlopen(req, timeout=8, context=SSL_CONTEXT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {}

    msg_array = payload.get("msgArray") or []
    if not msg_array:
        return {}

    row = msg_array[0]
    price = _safe_float(row.get("z")) or _safe_float(row.get("pz")) or _safe_float(row.get("y"))
    prev_close = _safe_float(row.get("y"))
    high = _safe_float(row.get("h"))
    low = _safe_float(row.get("l"))
    if not price:
        return {}

    change_1d = None
    if prev_close and prev_close != 0:
        change_1d = (price / prev_close - 1) * 100

    return {
        "price": round(price, 2),
        "change_1d": round(change_1d, 2) if change_1d is not None else None,
        "change_1m": None,
        "rsi": None,
        "ma20": None,
        "ma60": None,
        "high52": round(high, 2) if high is not None else None,
        "low52": round(low, 2) if low is not None else None,
        "beta": None,
        "source": "twse_realtime",
    }

def _fetch_json(url: str, params: dict, timeout: int = 10) -> dict:
    req = Request(
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))

def fetch_twse_daily_history(symbol: str, months: int = 14) -> pd.DataFrame:
    """Fetch TWSE-listed daily OHLCV history from official monthly API."""
    code = _tw_symbol_code(symbol)
    if not code or not symbol.endswith(".TW"):
        return pd.DataFrame()

    rows = []
    for month_start in _month_starts(months):
        try:
            payload = _fetch_json(
                TWSE_STOCK_DAY_URL,
                {
                    "response": "json",
                    "date": month_start.strftime("%Y%m%d"),
                    "stockNo": code,
                },
            )
        except (URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue

        for row in payload.get("data") or []:
            if len(row) < 7:
                continue
            ts = _parse_tw_date(row[0])
            close = _safe_float(row[6])
            if ts is None or close is None:
                continue
            rows.append({
                "Date": ts,
                "Open": _safe_float(row[3]),
                "High": _safe_float(row[4]),
                "Low": _safe_float(row[5]),
                "Close": close,
                "Volume": _safe_float(row[1]) or 0,
            })

    return _rows_to_history(rows, "twse_daily")

def fetch_tpex_daily_history(symbol: str, months: int = 14) -> pd.DataFrame:
    """Fetch TPEx-listed daily OHLCV history from official monthly API."""
    code = _tw_symbol_code(symbol)
    if not code or not symbol.endswith(".TWO"):
        return pd.DataFrame()

    rows = []
    for month_start in _month_starts(months):
        date_str = month_start.strftime("%Y/%m/%d")
        try:
            payload = _fetch_json(
                TPEX_DAILY_URL,
                {"code": code, "date": date_str, "response": "json"},
            )
        except (URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue

        table_rows = payload.get("data") or []
        if not table_rows and payload.get("tables"):
            table_rows = (payload.get("tables") or [{}])[0].get("data") or []
        for row in table_rows:
            if isinstance(row, dict):
                vals = [
                    row.get("date") or row.get("日期"),
                    row.get("volume") or row.get("成交股數"),
                    row.get("open") or row.get("開盤"),
                    row.get("high") or row.get("最高"),
                    row.get("low") or row.get("最低"),
                    row.get("close") or row.get("收盤"),
                ]
            else:
                vals = row
            if len(vals) < 6:
                continue
            ts = _parse_tw_date(vals[0])
            close = _safe_float(vals[5])
            if ts is None or close is None:
                continue
            rows.append({
                "Date": ts,
                "Open": _safe_float(vals[2]),
                "High": _safe_float(vals[3]),
                "Low": _safe_float(vals[4]),
                "Close": close,
                "Volume": _safe_float(vals[1]) or 0,
            })

    return _rows_to_history(rows, "tpex_daily")

def _rows_to_history(rows: list[dict], source: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    h = pd.DataFrame(rows).dropna(subset=["Date", "Close"])
    h = h.drop_duplicates(subset=["Date"]).sort_values("Date")
    h = h.set_index("Date")
    h.attrs["source"] = source
    for col in ("Open", "High", "Low"):
        h[col] = h[col].fillna(h["Close"])
    h["Volume"] = h["Volume"].fillna(0)
    return h

def fetch_official_tw_daily_history(symbol: str, months: int = 14) -> pd.DataFrame:
    if symbol.endswith(".TW"):
        return fetch_twse_daily_history(symbol, months=months)
    if symbol.endswith(".TWO"):
        return fetch_tpex_daily_history(symbol, months=months)
    return pd.DataFrame()

def fetch_benchmark_close(symbol: str) -> pd.Series:
    """Best-effort benchmark history for beta calculation."""
    try:
        h = yf.Ticker(symbol).history(period="1y")
        if len(h) == 0:
            return pd.Series(dtype=float)
        close = h["Close"]
        close.index = close.index.tz_localize(None)
        return close
    except Exception:
        return pd.Series(dtype=float)


# Intermarket reference universe per market. Tickers chosen for free yfinance
# availability and design-doc relevance: index + DXY + 10Y yield + VIX, plus
# the two sector ETFs the design names for Risk-On/Risk-Off ratio analysis.
INTERMARKET_REFERENCES_US = {
    "spx": "^GSPC",
    "dxy": "DX-Y.NYB",
    "us2y": "^IRX",   # 13-week proxy; real 2Y not on yfinance
    "us10y": "^TNX",
    "us30y": "^TYX",
    "vix": "^VIX",
    # Full SPDR sector basket for rotation map
    "xlk": "XLK",   # tech
    "xlv": "XLV",   # healthcare (defensive)
    "xlf": "XLF",   # financials
    "xle": "XLE",   # energy
    "xli": "XLI",   # industrials
    "xlp": "XLP",   # consumer staples (defensive)
    "xlu": "XLU",   # utilities (defensive)
    "xlb": "XLB",   # materials
    "xly": "XLY",   # consumer discretionary (cyclical)
    "xlc": "XLC",   # communication services
    "xlre": "XLRE", # real estate
}

INTERMARKET_REFERENCES_TW = {
    "twii": "^TWII",
    "spx": "^GSPC",
    "dxy": "DX-Y.NYB",
    "us10y": "^TNX",
    "us30y": "^TYX",
    "vix": "^VIX",
}


_INTERMARKET_CACHE: dict[str, tuple[float, dict]] = {}
_INTERMARKET_TTL = 900  # 15 minutes — benchmarks change with the broader market, not per-symbol


def fetch_intermarket_benchmarks(symbol: str) -> dict[str, pd.Series]:
    """Pull a small basket of intermarket reference series.

    Returns {label: close-series}. Empty series for any failed download — the
    consumer must tolerate missing labels. Results are cached per-market (TW
    vs US) with a 15-minute TTL because they do not depend on the symbol.
    Internal yfinance pulls run in parallel to amortise latency.
    """
    from concurrent.futures import ThreadPoolExecutor

    market_key = "tw" if _twse_channel(symbol) else "us"
    entry = _INTERMARKET_CACHE.get(market_key)
    if entry and time.time() < entry[0]:
        return entry[1]
    refs = INTERMARKET_REFERENCES_TW if market_key == "tw" else INTERMARKET_REFERENCES_US

    def _pull(label_ticker: tuple[str, str]) -> tuple[str, Optional[pd.Series]]:
        label, ticker = label_ticker
        try:
            h = yf.Ticker(ticker).history(period="1y")
            if len(h) == 0:
                return label, None
            close = h["Close"]
            close.index = close.index.tz_localize(None)
            return label, close
        except Exception:
            return label, None

    out: dict[str, pd.Series] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(refs))) as ex:
        for label, close in ex.map(_pull, list(refs.items())):
            if close is not None:
                out[label] = close
    _INTERMARKET_CACHE[market_key] = (time.time() + _INTERMARKET_TTL, out)
    return out


def fetch_intraday_history(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """Best-effort intraday OHLCV pull for execution-timeframe MTF analysis.

    yfinance interval limits: 1m=7d, 5m/15m=60d, 1h=730d. Returns empty
    DataFrame on failure so callers can degrade gracefully.
    """
    try:
        h = yf.Ticker(symbol).history(period=period, interval=interval)
        if len(h) == 0:
            return pd.DataFrame()
        # tz_localize(None) on a tz-aware index preserves wall-clock values
        # without converting to UTC first (pandas 3 behaviour).  We must
        # tz_convert('UTC') first so that .timestamp() later yields the correct
        # Unix epoch regardless of the server's local timezone.
        if h.index.tz is not None:
            h.index = h.index.tz_convert('UTC').tz_localize(None)
        return h
    except Exception:
        return pd.DataFrame()


_FUNDAMENTALS_CACHE: dict[str, tuple[float, dict]] = {}
_FUNDAMENTALS_TTL = 3600  # valuation/financials change slowly; 1h cache is safe


def fetch_fundamentals(symbol: str) -> dict:
    """Pull valuation + financial-health metrics from yfinance.info.

    Returns a normalized dict (None for missing fields). Cached 1h. Empty
    dict on failure so the LLM context still builds with technicals only.
    """
    entry = _FUNDAMENTALS_CACHE.get(symbol)
    if entry and time.time() < entry[0]:
        return entry[1]
    out: dict = {}
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        _FUNDAMENTALS_CACHE[symbol] = (time.time() + _FUNDAMENTALS_TTL, {})
        return {}

    def _g(key):
        v = info.get(key)
        return v if isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else None

    out = {
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": _g("marketCap"),
        # 估值
        "trailing_pe": _g("trailingPE"),
        "forward_pe": _g("forwardPE"),
        "price_to_book": _g("priceToBook"),
        "peg_ratio": _g("pegRatio"),
        "trailing_eps": _g("trailingEps"),
        "forward_eps": _g("forwardEps"),
        # 成長
        "revenue_growth": _g("revenueGrowth"),
        "earnings_growth": _g("earningsGrowth"),
        # 獲利能力
        "gross_margins": _g("grossMargins"),
        "operating_margins": _g("operatingMargins"),
        "profit_margins": _g("profitMargins"),
        "return_on_equity": _g("returnOnEquity"),
        # 財務體質
        "debt_to_equity": _g("debtToEquity"),
        "free_cashflow": _g("freeCashflow"),
        "dividend_yield": _g("dividendYield"),
        # 賣方共識
        "target_mean_price": _g("targetMeanPrice"),
        "target_high_price": _g("targetHighPrice"),
        "target_low_price": _g("targetLowPrice"),
        "recommendation_key": info.get("recommendationKey"),
        "num_analysts": _g("numberOfAnalystOpinions"),
    }
    # ETF / 基金沒有損益表，改存基金等價資訊（類別、規模、報酬、配息、持股）
    quote_type = (info.get("quoteType") or "").upper()
    out["quote_type"] = quote_type
    if quote_type in ("ETF", "MUTUALFUND"):
        out["is_fund"] = True
        out["etf"] = {
            "category": info.get("category"),
            "fund_family": info.get("fundFamily"),
            "total_assets": _g("totalAssets"),
            "nav": _g("navPrice"),
            "yield": _g("yield"),
            "ytd_return": _g("ytdReturn"),
            "three_year_return": _g("threeYearAverageReturn"),
            "five_year_return": _g("fiveYearAverageReturn"),
            "beta_3y": _g("beta3Year"),
            "legal_type": info.get("legalType"),
        }
        # 前 5 大持股（best-effort）
        try:
            th = yf.Ticker(symbol).funds_data.top_holdings
            if th is not None and not th.empty:
                out["etf"]["top_holdings"] = [
                    {"symbol": str(idx), "name": str(row.get("Name", "")),
                     "weight": round(float(row.get("Holding Percent", 0)) * 100, 2)}
                    for idx, row in th.head(5).iterrows()
                ]
        except Exception:
            pass
    _FUNDAMENTALS_CACHE[symbol] = (time.time() + _FUNDAMENTALS_TTL, out)
    return out


_FINANCIALS_CACHE: dict[str, tuple[float, list]] = {}
_FINANCIALS_TTL = 21600  # quarterly reports change only a few times/year; 6h cache


def fetch_financial_history(symbol: str, max_quarters: int = 8) -> list[dict]:
    """Pull historical quarterly financial statements from yfinance.

    yfinance exposes the same Yahoo Finance backend the web pages render, so we
    use the structured API instead of scraping fragile HTML. Returns a list of
    period records (newest first) with revenue / net income / EPS / margins and
    YoY growth where two years of the same quarter are available.

    ETFs and funds have no income statement → returns []. Cached 6h.
    """
    entry = _FINANCIALS_CACHE.get(symbol)
    if entry and time.time() < entry[0]:
        return entry[1]

    records: list[dict] = []
    try:
        t = yf.Ticker(symbol)
        qi = t.quarterly_income_stmt
    except Exception:
        _FINANCIALS_CACHE[symbol] = (time.time() + _FINANCIALS_TTL, [])
        return []
    if qi is None or qi.empty:
        _FINANCIALS_CACHE[symbol] = (time.time() + _FINANCIALS_TTL, [])
        return []

    def _row(name):
        return qi.loc[name] if name in qi.index else None

    rev = _row("Total Revenue")
    ni = _row("Net Income")
    eps = _row("Diluted EPS") if "Diluted EPS" in qi.index else _row("Basic EPS")
    gp = _row("Gross Profit")
    oi = _row("Operating Income")

    def _val(series, col):
        if series is None:
            return None
        try:
            v = series.get(col)
            v = float(v)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    cols = list(qi.columns)[:max_quarters]
    # Map period -> revenue for YoY (same quarter previous year ≈ 4 columns back)
    all_cols = list(qi.columns)
    for idx, col in enumerate(cols):
        period = col.date().isoformat() if hasattr(col, "date") else str(col)
        revenue = _val(rev, col)
        net_income = _val(ni, col)
        gross_profit = _val(gp, col)
        rec = {
            "period": period,
            "revenue": revenue,
            "net_income": net_income,
            "eps": _val(eps, col),
            "gross_profit": gross_profit,
            "operating_income": _val(oi, col),
            "gross_margin": round(gross_profit / revenue * 100, 1) if (gross_profit and revenue) else None,
            "net_margin": round(net_income / revenue * 100, 1) if (net_income and revenue) else None,
        }
        # YoY: same quarter previous year is ~4 columns later in the index
        try:
            yoy_idx = all_cols.index(col) + 4
            if yoy_idx < len(all_cols):
                prev_col = all_cols[yoy_idx]
                prev_rev = _val(rev, prev_col)
                prev_eps = _val(eps, prev_col)
                if prev_rev and revenue:
                    rec["revenue_yoy"] = round((revenue / prev_rev - 1) * 100, 1)
                if prev_eps and rec["eps"] is not None and prev_eps != 0:
                    rec["eps_yoy"] = round((rec["eps"] / prev_eps - 1) * 100, 1)
        except (ValueError, IndexError):
            pass
        records.append(rec)

    _FINANCIALS_CACHE[symbol] = (time.time() + _FINANCIALS_TTL, records)
    return records


_TW_BREADTH_CACHE: dict[str, tuple[float, dict]] = {}
_TW_BREADTH_TTL = 3600  # breadth updates once per trading day


_EARNINGS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_EARNINGS_TTL = 3600  # earnings dates rarely shift within an hour


def fetch_earnings_events(symbol: str) -> list[dict]:
    """Return upcoming/recent earnings dates as event-calendar payloads.

    yfinance exposes them per ticker for both US listings (AAPL/MSFT etc.) and
    most TW listings (.TW / .TWO). Failure returns an empty list so the
    XVII dimension still falls back to its whipsaw heuristic. Cached for an
    hour so successive matrix builds skip the yfinance round-trip.
    """
    entry = _EARNINGS_CACHE.get(symbol)
    if entry and time.time() < entry[0]:
        return entry[1]
    try:
        cal = yf.Ticker(symbol).calendar or {}
    except Exception:
        _EARNINGS_CACHE[symbol] = (time.time() + _EARNINGS_TTL, [])
        return []
    earnings_dates = cal.get("Earnings Date") or []
    if not isinstance(earnings_dates, list):
        earnings_dates = [earnings_dates]
    events: list[dict] = []
    for d in earnings_dates:
        try:
            ts = pd.Timestamp(d).normalize()
        except Exception:
            continue
        events.append({
            "time": ts.isoformat(),
            "label": "Earnings",
            "source": "yfinance_calendar",
        })
    dividend_date = cal.get("Dividend Date")
    if dividend_date:
        try:
            events.append({
                "time": pd.Timestamp(dividend_date).normalize().isoformat(),
                "label": "Dividend",
                "source": "yfinance_calendar",
            })
        except Exception:
            pass
    _EARNINGS_CACHE[symbol] = (time.time() + _EARNINGS_TTL, events)
    return events


def _bs_gamma(spot: float, strike: float, iv: float, days_to_expiry: float, rate: float = 0.045) -> float:
    """Black-Scholes gamma (same for calls and puts).

    gamma = N'(d1) / (S * sigma * sqrt(T))
    where N'(d1) = (1/sqrt(2π)) * exp(-d1²/2)
    """
    if spot <= 0 or strike <= 0 or iv <= 0 or days_to_expiry <= 0:
        return 0.0
    T = days_to_expiry / 365.0
    sigma_sqrt_T = iv * math.sqrt(T)
    if sigma_sqrt_T == 0:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv ** 2) * T) / sigma_sqrt_T
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (spot * sigma_sqrt_T)


def fetch_us_options_profile(symbol: str) -> Optional[dict]:
    """Build an options/GEX payload from the nearest yfinance expiration.

    Returns the payload shape consumed by `_options_gex`:
        spot, gamma_flip, gamma_wall, max_pain, gamma_regime, expiration

    Returns None when the ticker has no listed options or the chain pull fails.
    Caller is responsible for restricting use to US tickers; TW listings have
    no public option chain via yfinance.
    """
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return None
        # Pick the nearest expiration with at least 7 days to live so gamma
        # exposure has signal; same-day expiries collapse to zero gamma.
        today = datetime.now().date()
        chosen = None
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            days = (exp_date - today).days
            if days >= 7:
                chosen = (exp_str, days)
                break
        if chosen is None:
            return None
        exp_str, days_to_expiry = chosen
        chain = ticker.option_chain(exp_str)
        calls = chain.calls
        puts = chain.puts
        if calls is None or puts is None or calls.empty or puts.empty:
            return None
        spot = _safe_float(ticker.fast_info.get("last_price")) if hasattr(ticker, "fast_info") else None
        if spot is None:
            hist = ticker.history(period="2d")
            spot = float(hist["Close"].iloc[-1]) if len(hist) else None
        if spot is None or spot <= 0:
            return None
        # Per-strike: dealer gamma exposure (calls positive, puts negative)
        # convention assumes market makers are short calls / long puts on net.
        contract_multiplier = 100  # standard equity option
        strikes: dict[float, dict] = {}
        for _, row in calls.iterrows():
            strike = _safe_float(row.get("strike"))
            iv = _safe_float(row.get("impliedVolatility"))
            oi = _safe_float(row.get("openInterest"))
            if not strike or iv is None or iv <= 0 or oi is None:
                continue
            g = _bs_gamma(spot, strike, iv, days_to_expiry)
            strikes.setdefault(strike, {"call_gex": 0.0, "put_gex": 0.0, "call_oi": 0, "put_oi": 0, "call_value": 0.0, "put_value": 0.0})
            strikes[strike]["call_gex"] += g * oi * contract_multiplier * spot
            strikes[strike]["call_oi"] += int(oi)
            strikes[strike]["call_value"] += max(spot - strike, 0) * oi * contract_multiplier
        for _, row in puts.iterrows():
            strike = _safe_float(row.get("strike"))
            iv = _safe_float(row.get("impliedVolatility"))
            oi = _safe_float(row.get("openInterest"))
            if not strike or iv is None or iv <= 0 or oi is None:
                continue
            g = _bs_gamma(spot, strike, iv, days_to_expiry)
            strikes.setdefault(strike, {"call_gex": 0.0, "put_gex": 0.0, "call_oi": 0, "put_oi": 0, "call_value": 0.0, "put_value": 0.0})
            strikes[strike]["put_gex"] -= g * oi * contract_multiplier * spot
            strikes[strike]["put_oi"] += int(oi)
            strikes[strike]["put_value"] += max(strike - spot, 0) * oi * contract_multiplier
        if not strikes:
            return None
        sorted_strikes = sorted(strikes.keys())
        net_gex_by_strike = [(k, strikes[k]["call_gex"] + strikes[k]["put_gex"]) for k in sorted_strikes]
        # Gamma flip: highest strike where cumulative dealer gamma crosses zero
        cumulative = 0.0
        flip_strike = None
        for strike, gex in net_gex_by_strike:
            cumulative += gex
            if flip_strike is None and cumulative >= 0:
                flip_strike = strike
        # Gamma wall: strike with the largest absolute net GEX
        gamma_wall = max(net_gex_by_strike, key=lambda x: abs(x[1]))[0] if net_gex_by_strike else None
        # Max pain: strike that minimises (call intrinsic + put intrinsic) total
        pain_by_strike = {
            k: strikes[k]["call_value"] + strikes[k]["put_value"]
            for k in sorted_strikes
        }
        max_pain = min(pain_by_strike, key=pain_by_strike.get) if pain_by_strike else None
        total_net = sum(g for _, g in net_gex_by_strike)
        regime = "positive" if total_net >= 0 else "negative"

        # IV skew: 25-delta put IV minus 25-delta call IV. Use moneyness as
        # a proxy: 25Δ put ≈ 0.90 × spot, 25Δ call ≈ 1.10 × spot.
        def _closest_iv(df, target_strike: float) -> Optional[float]:
            if df is None or df.empty:
                return None
            df2 = df.copy()
            df2["_dist"] = (df2["strike"] - target_strike).abs()
            row = df2.sort_values("_dist").iloc[0]
            return _safe_float(row.get("impliedVolatility"))

        skew_25d = None
        put_25d_iv = _closest_iv(puts, spot * 0.90)
        call_25d_iv = _closest_iv(calls, spot * 1.10)
        if put_25d_iv is not None and call_25d_iv is not None:
            skew_25d = round(put_25d_iv - call_25d_iv, 4)

        # IV term structure: front-month ATM IV vs next-month ATM IV
        front_atm_iv = _closest_iv(calls, spot)
        term_structure_diff = None
        far_exp = None
        for exp_far in expirations:
            try:
                far_date = datetime.strptime(exp_far, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (far_date - today).days >= days_to_expiry + 14:
                far_exp = exp_far
                break
        if far_exp:
            try:
                far_chain = ticker.option_chain(far_exp)
                far_atm_iv = _closest_iv(far_chain.calls, spot)
                if front_atm_iv is not None and far_atm_iv is not None:
                    term_structure_diff = round(far_atm_iv - front_atm_iv, 4)
            except Exception:
                pass

        return {
            "spot": round(spot, 2),
            "expiration": exp_str,
            "days_to_expiry": days_to_expiry,
            "gamma_flip": round(flip_strike, 2) if flip_strike else None,
            "gamma_wall": round(gamma_wall, 2) if gamma_wall else None,
            "max_pain": round(max_pain, 2) if max_pain else None,
            "gamma_regime": regime,
            "net_gex_total": round(total_net, 2),
            "skew_25_delta": skew_25d,
            "atm_iv_front": round(front_atm_iv, 4) if front_atm_iv else None,
            "iv_term_structure_diff": term_structure_diff,
            "far_expiration": far_exp,
            "source": "yfinance_option_chain",
        }
    except Exception:
        return None


def fetch_tw_order_book_snapshot(symbol: str) -> Optional[dict]:
    """Pull the latest 5-level bid/ask snapshot for a TW listing.

    Returns an `order_book` payload (bids/asks as price+size dicts) that the
    XIV dimension consumes, or None on any failure.
    """
    channel = _twse_channel(symbol)
    if not channel:
        return None
    query = urlencode({
        "ex_ch": channel,
        "json": "1",
        "delay": "0",
        "_": int(datetime.now().timestamp() * 1000),
    })
    req = Request(
        f"{TWSE_MIS_URL}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        },
    )
    try:
        with urlopen(req, timeout=8, context=SSL_CONTEXT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    msg_array = payload.get("msgArray") or []
    if not msg_array:
        return None
    row = msg_array[0]

    def _parse_levels(price_field: str, size_field: str) -> list[dict]:
        prices = [p for p in (row.get(price_field) or "").split("_") if p and p != "-"]
        sizes = [s for s in (row.get(size_field) or "").split("_") if s and s != "-"]
        out = []
        for p, s in zip(prices, sizes):
            price = _safe_float(p)
            size = _safe_float(s)
            if price is None or size is None:
                continue
            out.append({"price": price, "size": size})
        return out

    bids = _parse_levels("b", "g")
    asks = _parse_levels("a", "f")
    if not bids and not asks:
        return None
    return {
        "bids": bids,
        "asks": asks,
        "as_of_ts": row.get("tlong") or row.get("t"),
        "source": "twse_5level",
    }


def fetch_tw_breadth() -> Optional[dict]:
    """Pull today's TWSE advance/decline counts from the public MI_INDEX feed.

    Returns {advancing, declining, unchanged, untraded} for use as a breadth
    payload, or None on any failure. Cached for an hour because the upstream
    value changes only at the daily close.
    """
    cache_key = "twse"
    entry = _TW_BREADTH_CACHE.get(cache_key)
    if entry and time.time() < entry[0]:
        return entry[1]
    today = datetime.now().strftime("%Y%m%d")
    url = (
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
        f"?date={today}&type=MS&response=json"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=8, context=SSL_CONTEXT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    counts = {"advancing": None, "declining": None, "unchanged": None, "untraded": None}
    # TWSE wraps the breadth table under `tables` (new API) or `data*` (legacy).
    tables = payload.get("tables") or []
    for table in tables:
        title = (table.get("title") or "").replace(" ", "")
        if "漲跌證券數" not in title and "漲跌統計" not in title:
            continue
        for row in table.get("data") or []:
            if not row:
                continue
            label = (row[0] or "").replace(" ", "")
            try:
                # Row format: [類別, 整體上漲, 整體下跌, 整體持平, 整體未成交]
                if "整體" in label and "市場" in label:
                    counts["advancing"] = _safe_float(str(row[1]).replace(",", ""))
                    counts["declining"] = _safe_float(str(row[2]).replace(",", ""))
                    counts["unchanged"] = _safe_float(str(row[3]).replace(",", ""))
            except Exception:
                continue
    if counts["advancing"] is None or counts["declining"] is None:
        # Legacy schema fallback
        for key in ("data1", "data2", "data3", "data"):
            table = payload.get(key)
            if not isinstance(table, list):
                continue
            for row in table:
                if not row or "整體" not in str(row[0] or ""):
                    continue
                try:
                    counts["advancing"] = _safe_float(str(row[1]).replace(",", ""))
                    counts["declining"] = _safe_float(str(row[2]).replace(",", ""))
                    counts["unchanged"] = _safe_float(str(row[3]).replace(",", ""))
                except Exception:
                    continue
    if counts["advancing"] is None or counts["declining"] is None:
        return None
    breadth = {
        "advancing": int(counts["advancing"]),
        "declining": int(counts["declining"]),
        "unchanged": int(counts["unchanged"] or 0),
        "as_of_date": today,
        "source": "twse_mi_index",
    }
    _TW_BREADTH_CACHE[cache_key] = (time.time() + _TW_BREADTH_TTL, breadth)
    return breadth

def _normalize_history_index(h: pd.DataFrame) -> pd.DataFrame:
    if len(h) == 0:
        return h
    h = h.copy()
    h.index = pd.to_datetime(h.index).tz_localize(None)
    return h

def fetch_history(symbol: str, period: str = "1y") -> tuple[pd.DataFrame, str]:
    """Fetch OHLCV history with official Taiwan daily data as fallback."""
    try:
        h = yf.Ticker(symbol).history(period=period)
        if len(h) > 0:
            h = _normalize_history_index(h)
            h.attrs["source"] = "yfinance"
            return h, "yfinance"
    except Exception:
        pass

    if _twse_channel(symbol):
        months_by_period = {"1mo": 2, "3mo": 4, "6mo": 8, "1y": 14, "2y": 26}
        months = months_by_period.get(period, 14)
        h = fetch_official_tw_daily_history(symbol, months=months)
        if len(h) > 0:
            return h, h.attrs.get("source", "official_tw_daily")
    return pd.DataFrame(), ""


def _fetch_daily_history_from_date(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    if start_date > end_date:
        return pd.DataFrame()
    try:
        h = yf.Ticker(symbol).history(
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            interval="1d",
        )
        if len(h) > 0:
            return _normalize_history_index(h)
    except Exception:
        pass

    if _twse_channel(symbol):
        months = max(2, int(math.ceil(((end_date - start_date).days + 31) / 30)))
        h = fetch_official_tw_daily_history(symbol, months=months)
        if len(h) > 0:
            return h[h.index.date >= start_date]
    return pd.DataFrame()


def _chart_period_config(period: str) -> dict[str, str]:
    configs = {
        "1m": {"period": "5d", "interval": "1m"},
        "5m": {"period": "5d", "interval": "5m"},
        "15m": {"period": "10d", "interval": "15m"},
        "30m": {"period": "20d", "interval": "30m"},
        "1h": {"period": "30d", "interval": "1h"},
        "4h": {"period": "60d", "interval": "1h"},
        "1d": {"period": "1d", "interval": "15m"},
        "5d": {"period": "5d", "interval": "30m"},
        "1mo": {"period": "1mo", "interval": "1d"},
        "3mo": {"period": "3mo", "interval": "1d"},
        "6mo": {"period": "6mo", "interval": "1d"},
        "1y": {"period": "1y", "interval": "1d"},
        "2y": {"period": "2y", "interval": "1d"},
    }
    return configs.get(period, configs["6mo"])

def _indicators_from_history(h: pd.DataFrame, bench_close=None, source: str = "") -> dict:
    """Compute partial indicator schema from any OHLCV history DataFrame.

    回 partial schema：每個 indicator 都有對應的最低資料量需求，缺的就回 None：
      - price / change_1d:  ≥ 1 筆
      - change_1m:          ≥ 22 筆
      - rsi:                ≥ 14 筆
      - ma20 / high52 / low52: ≥ 20 筆
      - ma60:               ≥ 60 筆
      - beta:               ≥ 20 筆 + bench_close ≥ 20 筆

    新上市股（如 009819 上市才 18 天）以前會被 len < 20 卡住整個 return {}，
    現在改成只缺什麼回什麼，至少 price 與 RSI 仍可用。
    """
    if len(h) < 1:
        return {}
    h = h.dropna(subset=["Close", "Open", "High", "Low"])
    if len(h) < 1:
        return {}
    c = h["Close"]
    n = len(c)

    price = float(c.iloc[-1])
    prev_d = float(c.iloc[-2]) if n > 1 else price
    change_1d = round((price / prev_d - 1) * 100, 2) if n > 1 else None
    change_1m = round((price / float(c.iloc[-22]) - 1) * 100, 2) if n > 21 else None

    ma20 = round(float(c.rolling(20).mean().iloc[-1]), 2) if n >= 20 else None
    ma60 = round(float(c.rolling(60).mean().iloc[-1]), 2) if n >= 60 else None

    if n >= 14:
        d = c.diff()
        gain = d.clip(lower=0).rolling(14).mean()
        loss = (-d.clip(upper=0)).rolling(14).mean()
        rsi_val = (100 - 100 / (1 + gain / loss)).iloc[-1]
        rsi = round(float(rsi_val), 1) if not pd.isna(rsi_val) else None
    else:
        rsi = None

    if n >= 20:
        high52 = round(float(c.iloc[-252:].max()), 2)
        low52 = round(float(c.iloc[-252:].min()), 2)
    else:
        high52 = round(float(c.max()), 2)
        low52 = round(float(c.min()), 2)

    beta = None
    if bench_close is not None and len(bench_close) > 20 and n >= 20:
        al = pd.concat([c.rename("s"), bench_close.rename("m")], axis=1).dropna()
        if len(al) > 20:
            rs = al["s"].pct_change().dropna()
            rm = al["m"].pct_change().dropna()
            cv = np.cov(rs, rm)
            beta_val = cv[0, 1] / cv[1, 1]
            beta = round(float(beta_val), 2) if not np.isnan(beta_val) else None

    return sanitize_float_values({
        "price": round(price, 2),
        "change_1d": change_1d,
        "change_1m": change_1m,
        "rsi": rsi,
        "ma20": ma20,
        "ma60": ma60,
        "high52": high52,
        "low52": low52,
        "beta": beta,
        "source": source or h.attrs.get("source") or "history",
    })

_YF_HISTORY_CACHE: dict[tuple[str, str], tuple[float, "pd.DataFrame"]] = {}
_YF_HISTORY_TTL = 60  # daily bars only refresh intraday by minutes; 60s is safe
_YF_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_YF_INFO_TTL = 30  # realtime price updates roughly every 30s on Yahoo's feed


def _cached_yf_history(symbol: str, period: str = "1y") -> "pd.DataFrame":
    key = (symbol, period)
    entry = _YF_HISTORY_CACHE.get(key)
    if entry and time.time() < entry[0]:
        return entry[1]
    try:
        h = yf.Ticker(symbol).history(period=period)
    except Exception:
        return pd.DataFrame()
    _YF_HISTORY_CACHE[key] = (time.time() + _YF_HISTORY_TTL, h)
    return h


def fetch_yfinance_indicators(symbol: str, bench_close=None) -> dict:
    """Full indicator source backed by Yahoo Finance history."""
    try:
        h = _cached_yf_history(symbol, period="1y")
        if len(h) < 1:
            return {}
        h = _normalize_history_index(h)
        return _indicators_from_history(h, bench_close=bench_close, source="yfinance")
    except Exception:
        return {}

def fetch_official_tw_indicators(symbol: str, bench_close=None) -> dict:
    h = fetch_official_tw_daily_history(symbol, months=14)
    if len(h) == 0:
        return {}
    result = _indicators_from_history(h, bench_close=bench_close, source=h.attrs.get("source", "official_tw_daily"))
    result["source"] = h.attrs.get("source", "official_tw_daily")
    return result

def fetch_indicators(symbol: str, bench_close=None) -> dict:
    """Market-aware quote fetcher.

    Strategy: 永遠取 yfinance 的歷史指標（RSI/MA/Beta/52週高低），
    台股額外用 TWSE/TPEX 即時 quote 覆蓋 price/change_1d 提升即時性。
    這樣可避免「全走 TWSE 導致技術指標凍結」的問題。
    Implementation note: the three potential network calls
    (yfinance history, yfinance .info, TWSE realtime) all hit different
    services so we issue them concurrently and assemble the result after
    everything returns. TW symbols skip yfinance.info entirely because the
    TWSE realtime quote already provides price/change_1d at lower latency.
    """
    from concurrent.futures import ThreadPoolExecutor

    is_tw = bool(_twse_channel(symbol))

    def _yf_history_job():
        return fetch_yfinance_indicators(symbol, bench_close)

    # TW ETF code heuristic: 4-digit codes starting with "00" before the dot
    # (0050, 0056, 00xx series). Used to opt-in to the slower yf.info call
    # only for TW ETFs where we genuinely need navPrice for 折溢價 display.
    _tw_part = symbol.split(".")[0] if symbol else ""
    is_tw_etf_like = is_tw and _tw_part.startswith("00") and len(_tw_part) <= 6
    want_info = (not is_tw) or is_tw_etf_like

    def _yf_info_job():
        return _get_yf_quote_info(symbol, want_info=want_info)

    def _tw_realtime_job():
        return fetch_tw_realtime_quote(symbol) if is_tw else None

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_hist = ex.submit(_yf_history_job)
        f_info = ex.submit(_yf_info_job)
        f_tw = ex.submit(_tw_realtime_job)
        # Defensive copy: fetch_yfinance_indicators / mocks may share a dict
        # across calls and downstream paths mutate price/source on it.
        yf_ind = dict(f_hist.result() or {})
        info_payload = f_info.result() or {}
        yf_realtime = info_payload.get("realtime") if isinstance(info_payload, dict) else info_payload
        official = f_tw.result()

    if not yf_ind and is_tw:
        yf_ind = dict(fetch_official_tw_indicators(symbol, bench_close) or {})

    if yf_ind and yf_realtime is not None:
        yf_ind["_yf_realtime"] = yf_realtime
    # NAV + quote_type 是 ETF 折溢價計算需要的；只要 yfinance.info 給了就保留
    if yf_ind and isinstance(info_payload, dict):
        if info_payload.get("nav") is not None:
            yf_ind["nav"] = info_payload["nav"]
        if info_payload.get("quote_type"):
            yf_ind["quote_type"] = info_payload["quote_type"]

    if is_tw:
        if official:
            if not yf_ind:
                # yfinance 失敗：直接回 TWSE 完整 schema（含 rsi=None 等）
                return official
            # 兩邊都有：取較可能即時的價格
            twse_price = official.get("price")
            yf_price = yf_ind.get("_yf_realtime") or yf_ind.get("price")
            twse_change = official.get("change_1d") or 0

            # TWSE 在盤中若 change_1d==0 且價格與 yfinance 昨收相同，
            # 代表 TWSE 還沒更新到盤中即時價 → 優先用 yfinance
            if twse_price and yf_price and abs(twse_change) > 0.001:
                # TWSE 有即時漲跌 → 用 TWSE
                yf_ind["price"] = twse_price
                yf_ind["change_1d"] = official["change_1d"]
            elif yf_price:
                # TWSE 疑似未更新 → 保留 yfinance 的值
                yf_ind["price"] = yf_price

            # high/low 只在 yfinance 沒有時用 TWSE 的當日高低備援
            for key in ("high52", "low52"):
                if yf_ind.get(key) is None and official.get(key) is not None:
                    yf_ind[key] = official[key]
            history_source = yf_ind.get("source") or "history"
            yf_ind["source"] = f"twse_realtime+{history_source}"
            yf_ind.pop("_yf_realtime", None)
            return yf_ind

    # US (or any non-TW) path: yfinance daily history's last bar is yesterday's
    # close until Yahoo backfills the intraday partial bar. yf.Ticker.info has
    # the genuine realtime price — if we got it, overlay both price and change_1d
    # so positions PnL actually tracks the live market.
    if not is_tw and yf_ind and yf_realtime is not None:
        prev_close = None
        # change_1d in yf_ind was computed against yfinance daily prev close;
        # we can recover that prev close from price / (1 + change_1d/100).
        prior_price = yf_ind.get("price")
        prior_change = yf_ind.get("change_1d")
        if prior_price and prior_change is not None:
            try:
                prev_close = prior_price / (1 + prior_change / 100.0)
            except (ZeroDivisionError, TypeError):
                prev_close = None
        yf_ind["price"] = round(float(yf_realtime), 4)
        if prev_close and prev_close > 0:
            yf_ind["change_1d"] = round((yf_realtime / prev_close - 1) * 100, 2)
        history_source = yf_ind.get("source") or "history"
        if "yf_realtime" not in history_source:
            yf_ind["source"] = f"yf_realtime+{history_source}"

    yf_ind.pop("_yf_realtime", None)
    return yf_ind


def _get_yf_quote_info(symbol: str, want_info: bool = True) -> dict:
    if not want_info:
        return {"realtime": None, "nav": None, "quote_type": None, "pb": None}
    entry = _YF_INFO_CACHE.get(symbol)
    if entry and time.time() < entry[0]:
        return entry[1]
    out = {"realtime": None, "nav": None, "quote_type": None, "pb": None}
    try:
        info = yf.Ticker(symbol).info or {}
        rmp = info.get("regularMarketPrice") or info.get("currentPrice")
        if rmp and rmp > 0:
            out["realtime"] = float(rmp)
        nav = info.get("navPrice")
        if nav and nav > 0:
            out["nav"] = float(nav)
        out["quote_type"] = info.get("quoteType")
        pb = info.get("priceToBook")
        if pb is not None:
            out["pb"] = _safe_float(pb)
    except Exception:
        pass
    _YF_INFO_CACHE[symbol] = (time.time() + _YF_INFO_TTL, out)
    return out


def _hydrate_price_cache_meta(row: dict) -> tuple[Optional[float], Optional[str], Optional[float]]:
    nav = row.get("nav")
    quote_type = row.get("quote_type")
    pb = _safe_float(row.get("pb"))
    raw = row.get("price_cache_data")
    if raw:
        try:
            blob = json.loads(raw)
            if nav is None:
                nav = blob.get("nav")
            if quote_type is None:
                quote_type = blob.get("quote_type")
            if pb is None:
                pb = _safe_float(blob.get("pb"))
        except (TypeError, ValueError):
            pass
    return nav, quote_type, pb


def _load_active_portfolio_entities(conn) -> tuple[list[dict], list[dict]]:
    positions = [
        dict(row) for row in conn.execute("SELECT * FROM positions").fetchall()
        if not _is_test_symbol(row["symbol"], row["name"], row["category"])
    ]
    watchlist = [
        dict(row) for row in conn.execute("SELECT * FROM watchlist").fetchall()
        if not _is_test_symbol(row["symbol"], row["name"], row["category"])
    ]
    return positions, watchlist


def _collect_symbol_benchmarks(positions: list[dict], watchlist: list[dict], twii, spx) -> dict[str, dict]:
    all_symbols: dict[str, dict] = {}
    for d in positions:
        all_symbols[d["symbol"]] = {"bench": twii if ".TW" in d["symbol"] else spx}
    for d in watchlist:
        all_symbols.setdefault(d["symbol"], {"bench": twii if ".TW" in d["symbol"] else spx})
    return all_symbols


def _fetch_indicator_batch(symbol_map: dict[str, dict], use_benchmark: bool = True, max_workers: int = 8) -> dict[str, dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(symbol, bench):
        return symbol, fetch_indicators(symbol, bench if use_benchmark else None)

    indicators: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_fetch_one, sym, info.get("bench")): sym
            for sym, info in symbol_map.items()
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                fetched_sym, ind = future.result()
                if ind and "price" in ind:
                    indicators[fetched_sym] = ind
            except Exception as e:
                print(f"  [WARN] {sym} fetch failed: {e}")
    return indicators

def store_price_cache(c, symbol: str, ind: dict):
    """Persist fresh quote data while keeping older indicator fields when absent."""
    ind = sanitize_float_values(ind)
    existing = c.execute("SELECT * FROM price_cache WHERE symbol=?", (symbol,)).fetchone()
    existing_data = json.loads(existing["data"] or "{}") if existing and existing["data"] else {}
    if existing:
        for key in ("nav", "pb", "quote_type"):
            if existing[key] is not None and key not in existing_data:
                existing_data[key] = existing[key]
    merged = dict(existing_data)
    for key, value in ind.items():
        if value is not None:
            merged[key] = value
    merged["source"] = ind.get("source", merged.get("source"))
    merged = sanitize_float_values(merged)

    c.execute(
        """INSERT OR REPLACE INTO price_cache
           (symbol, ts, price, rsi, ma20, ma60, high52, low52, change_1d, change_1m, beta, nav, pb, quote_type, data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            symbol,
            datetime.now().isoformat(timespec="seconds"),
            merged.get("price"),
            merged.get("rsi"),
            merged.get("ma20"),
            merged.get("ma60"),
            merged.get("high52"),
            merged.get("low52"),
            merged.get("change_1d"),
            merged.get("change_1m"),
            merged.get("beta"),
            merged.get("nav"),
            merged.get("pb"),
            merged.get("quote_type"),
            json.dumps(merged),
        ),
    )

def get_market_state():
    """Pull VIX, TWII, SPX, SOX, NDX, TNX, DXY, HSNTECH."""
    def _last(series):
        return float(series.iloc[-1]) if len(series) > 0 else None

    def _ma(series, n):
        return float(series.rolling(n).mean().iloc[-1]) if len(series) >= n else _last(series)

    def _fetch(ticker, period="6mo"):
        try:
            return yf.Ticker(ticker).history(period=period)["Close"].dropna()
        except Exception:
            return None

    try:
        # 7 yfinance calls were serial (~3s total). Parallelise — they target
        # distinct tickers so there is no upstream contention.
        from concurrent.futures import ThreadPoolExecutor
        tasks = [
            ("vix", "^VIX", "1mo"),
            ("twii", "^TWII", "6mo"),
            ("spx", "^GSPC", "6mo"),
            ("sox", "^SOX", "6mo"),
            ("ndx", "^NDX", "3mo"),
            ("tnx", "^TNX", "1mo"),
            ("dxy", "DX-Y.NYB", "3mo"),
        ]
        results: dict = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            for label, series in ex.map(lambda t: (t[0], _fetch(t[1], t[2])), tasks):
                results[label] = series
        vix_s, twii_s, spx_s, sox_s, ndx_s, tnx_s, dxy_s = (
            results["vix"], results["twii"], results["spx"], results["sox"],
            results["ndx"], results["tnx"], results["dxy"],
        )

        vix_val    = _last(vix_s)     if vix_s  is not None else None
        twii_val   = _last(twii_s)    if twii_s is not None else None
        twii_ma20  = _ma(twii_s, 20)  if twii_s is not None else None
        twii_ma60  = _ma(twii_s, 60)  if twii_s is not None else None
        twii_ma120 = _ma(twii_s, 120) if twii_s is not None else None
        spx_val    = _last(spx_s)     if spx_s  is not None else None
        spx_ma20   = _ma(spx_s, 20)   if spx_s  is not None else None
        spx_ma60   = _ma(spx_s, 60)   if spx_s  is not None else None
        spx_ma120  = _ma(spx_s, 120)  if spx_s  is not None else None
        sox_val    = _last(sox_s)     if sox_s  is not None else None
        sox_ma60   = _ma(sox_s, 60)   if sox_s  is not None else None
        ndx_val    = _last(ndx_s)     if ndx_s  is not None else None
        ndx_ma20   = _ma(ndx_s, 20)   if ndx_s  is not None else None
        tnx_val    = _last(tnx_s)     if tnx_s  is not None else None
        dxy_val    = _last(dxy_s)     if dxy_s  is not None else None

        warnings = 0
        if vix_val   and vix_val > 25:                       warnings += 1
        if twii_val  and twii_ma60  and twii_val < twii_ma60: warnings += 1
        if spx_val   and spx_ma60   and spx_val < spx_ma60:   warnings += 1
        if sox_val   and sox_ma60   and sox_val < sox_ma60:    warnings += 1  # semiconductor stress

        level = "danger" if warnings >= 3 else "warning" if warnings >= 1 else "safe"

        def _r(v, d=2): return round(v, d) if v is not None else None

        return {
            "ts":           datetime.now().isoformat(timespec="seconds"),
            "vix":          _r(vix_val),
            "twii":         _r(twii_val, 0),
            "twii_ma20":    _r(twii_ma20, 0),
            "twii_ma60":    _r(twii_ma60, 0),
            "twii_ma120":   _r(twii_ma120, 0),
            "spx":          _r(spx_val),
            "spx_ma20":     _r(spx_ma20),
            "spx_ma60":     _r(spx_ma60),
            "spx_ma120":    _r(spx_ma120),
            "sox":          _r(sox_val, 0),
            "sox_ma60":     _r(sox_ma60, 0),
            "ndx":          _r(ndx_val, 0),
            "ndx_ma20":     _r(ndx_ma20, 0),
            "tnx":          _r(tnx_val),    # 10Y yield %
            "dxy":          _r(dxy_val),    # USD index
            "risk_level":   level,
            "warnings_count": warnings,
        }
    except Exception as e:
        return {"error": str(e)}

# ─────────────── Alert Engine ───────────────
def evaluate_alerts(symbol: str, name: str, ind: dict, position=None, watch=None):
    """Return list of alert dicts."""
    alerts = []
    price = ind.get("price")
    rsi = ind.get("rsi")
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")
    if not price:
        return alerts

    # ── Watchlist 觸發 ──
    if watch:
        target_entry = watch.get("target_entry")
        target_profit = watch.get("target_profit")
        if target_entry and price <= target_entry * 1.02:
            alerts.append({
                "level": "info",
                "type": "ENTRY_TRIGGER",
                "message": f"{name} 已到進場區 {target_entry:.2f}（現價 {price}）",
                "price": price,
            })
        if target_profit and price >= target_profit * 0.98:
            alerts.append({
                "level": "info",
                "type": "PROFIT_TARGET",
                "message": f"{name} 已達停利目標 {target_profit:.2f}（現價 {price}）",
                "price": price,
            })

    # ── Position 觸發 ──
    if position:
        cost = position.get("cost_price")
        if cost:
            pnl_pct = (price/cost - 1) * 100
            if pnl_pct <= -10:
                alerts.append({
                    "level": "danger",
                    "type": "STOP_LOSS",
                    "message": f"{name} 虧損 {pnl_pct:.1f}% 觸及 -10% 停損線",
                    "price": price,
                })
            elif pnl_pct <= -7:
                alerts.append({
                    "level": "warning",
                    "type": "LOSS_WARN",
                    "message": f"{name} 虧損 {pnl_pct:.1f}% 接近停損線",
                    "price": price,
                })
            elif pnl_pct >= 30:
                alerts.append({
                    "level": "info",
                    "type": "PROFIT_30",
                    "message": f"{name} 獲利 {pnl_pct:.1f}%，可考慮分批停利",
                    "price": price,
                })

    # ── 技術面通用 ──
    if rsi and rsi >= 80:
        alerts.append({
            "level": "warning",
            "type": "RSI_OVERBOUGHT",
            "message": f"{name} RSI {rsi:.1f} 超買，注意回檔風險",
            "price": price,
        })
    if rsi and rsi <= 25:
        alerts.append({
            "level": "info",
            "type": "RSI_OVERSOLD",
            "message": f"{name} RSI {rsi:.1f} 超賣，可能反彈",
            "price": price,
        })

    if ma20 and price < ma20 * 0.97:
        alerts.append({
            "level": "warning",
            "type": "BELOW_MA20",
            "message": f"{name} 跌破 MA20 ({ma20:.2f}) 約 3%，趨勢轉弱",
            "price": price,
        })

    return alerts

def diagnose(symbol: str, name: str, ind: dict, market: dict, position=None) -> str:
    """規則式自動診斷，給出操作建議。"""
    price = ind.get("price")
    rsi = ind.get("rsi")
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")
    high52 = ind.get("high52")
    market_level = market.get("risk_level", "safe")
    diag = []

    # 大盤狀態
    if market_level == "danger":
        diag.append("[大盤危險] 3+ 紅燈，任何進場都需減半。")
    elif market_level == "warning":
        diag.append("[大盤警戒] 建議分批進場。")

    # 個股技術
    if not price:
        return "資料不足"

    if rsi:
        if rsi >= 80:
            diag.append(f"RSI {rsi} 嚴重超買 → 不追高，等回到 60 以下。")
        elif rsi >= 70:
            diag.append(f"RSI {rsi} 偏高 → 短線回調風險。")
        elif rsi <= 30:
            diag.append(f"RSI {rsi} 超賣 → 反彈機會大。")
        elif 40 <= rsi <= 60:
            diag.append(f"RSI {rsi} 健康 → 適合進場。")

    if ma20 and ma60:
        if price > ma20 > ma60:
            diag.append("多頭排列：價格 > MA20 > MA60，趨勢向上。")
        elif price < ma20 < ma60:
            diag.append("空頭排列：建議先觀望。")

    if high52 and price >= high52 * 0.98:
        diag.append(f"已達52週新高 {high52} 附近 → 突破有效則加碼，失敗則減碼。")

    if position:
        cost = position.get("cost_price")
        if cost and price:
            pnl_pct = (price/cost - 1) * 100
            if pnl_pct < -10:
                diag.append(f"已虧損 {pnl_pct:.1f}%，建議停損出場。")
            elif pnl_pct > 30:
                diag.append(f"已獲利 {pnl_pct:.1f}%，建議分批停利 30~50%。")

    return " | ".join(diag) if diag else "暫無重大訊號，繼續觀察。"

def insert_alerts(c, symbol: str, name: str, ind: dict, market: dict, position=None, watch=None) -> int:
    """Evaluate alerts, apply daily de-duplication, and insert new rows."""
    created = 0
    today_start = f"{datetime.now().date().isoformat()}T00:00:00"
    alerts = evaluate_alerts(symbol, name, ind, position=position, watch=watch)
    for a in alerts:
        existing = c.execute(
            "SELECT 1 FROM alerts WHERE symbol=? AND type=? AND ts>=? LIMIT 1",
            (symbol, a["type"], today_start),
        ).fetchone()
        if existing:
            continue

        diag_text = diagnose(symbol, name, ind, market, position=position)
        ts_now = datetime.now().isoformat(timespec="seconds")
        c.execute(
            "INSERT INTO alerts (ts, symbol, level, type, message, price, diagnosis) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts_now, symbol, a["level"], a["type"], a["message"], a["price"], diag_text),
        )
        # Async Obsidian write (best-effort, non-blocking)
        try:
            vault = _get_vault()
            if vault:
                _obsidian_write_alert(vault, {
                    "ts": ts_now,
                    "symbol": symbol,
                    "level": a["level"],
                    "type": a["type"],
                    "message": a["message"],
                    "price": a.get("price"),
                    "diagnosis": diag_text,
                    "acknowledged": 0,
                })
        except Exception:
            pass
        created += 1
    return created

# ─────────────── Background Monitor ───────────────
# Serialize monitor_loop and api_refresh: if both fire concurrently they
# spawn 40+ yfinance threads against the same process, which makes Yahoo
# silently drop a chunk of requests (the US history calls were the visible
# casualty — manual refresh appeared to do nothing because those fetches
# returned empty dicts).
_refresh_lock = asyncio.Lock()


async def monitor_loop():
    """Background task: refresh prices + evaluate alerts every 5 minutes."""
    while True:
        try:
            async with _refresh_lock:
                t0 = datetime.now()
                print(f"[{t0:%H:%M:%S}] Monitor cycle started...")
                cycle = _run_refresh_cycle_sync(use_benchmark=True, indicator_workers=8)
                elapsed = (datetime.now() - t0).total_seconds()
                print(
                    f"[{datetime.now():%H:%M:%S}] Monitor cycle done. "
                    f"({elapsed:.1f}s, {len(cycle['indicators'])}/{len(cycle['symbol_map'])} symbols)"
                )
        except Exception as e:
            print(f"Monitor error: {e}")

        # Same yfinance FD leak workaround as in api_refresh — drop stranded
        # tkr-tz SQLite handles before sleeping.
        import gc
        gc.collect()
        await asyncio.sleep(300)  # 5 minutes

# ─────────────── FastAPI lifecycle ───────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(monitor_loop())
    yield
    task.cancel()

app = FastAPI(title="TradingAgents Dashboard", lifespan=lifespan)

# ─────────────── Routes ───────────────
@app.get("/", response_class=HTMLResponse)
def home():
    html_path = BASE / "templates" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

@app.get("/api/market")
def api_market():
    conn = get_db()
    row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {"error": "no data yet"}


MARKET_INTRADAY_CONFIG = {
    "twii": {
        "symbol": "^TWII",
        "label": "台股加權",
        "timezone": "Asia/Taipei",
        "open": (9, 0),
        "close": (13, 30),
    },
    "spx": {
        "symbol": "^GSPC",
        "label": "S&P 500",
        "timezone": "America/New_York",
        "open": (9, 30),
        "close": (16, 0),
    },
}


def _market_intraday_session(key: str, interval: str = "5m") -> dict:
    cfg = MARKET_INTRADAY_CONFIG.get(key)
    if not cfg:
        return {"error": "unknown market"}
    allowed_intervals = {"1m", "2m", "5m", "15m", "30m", "60m"}
    if interval not in allowed_intervals:
        interval = "5m"

    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    open_dt = now.replace(hour=cfg["open"][0], minute=cfg["open"][1], second=0, microsecond=0)
    close_dt = now.replace(hour=cfg["close"][0], minute=cfg["close"][1], second=0, microsecond=0)
    preopen_dt = open_dt - timedelta(minutes=10)
    is_business_day = now.weekday() < 5
    prefer_today = is_business_day and now >= preopen_dt
    is_preopen = prefer_today and now < open_dt

    h = yf.Ticker(cfg["symbol"]).history(period="5d", interval=interval)
    h = h.dropna(subset=["Close"])
    if len(h) == 0:
        return {"error": "no intraday data"}

    if h.index.tz is None:
        h.index = h.index.tz_localize("UTC").tz_convert(tz)
    else:
        h.index = h.index.tz_convert(tz)

    available_dates = sorted(set(h.index.date))
    today = now.date()
    selected_date = today if prefer_today else available_dates[-1]
    if selected_date not in available_dates and not is_preopen:
        selected_date = available_dates[-1]

    session = h[h.index.date == selected_date]
    points = [
        {"time": int(ts.timestamp()), "value": round(float(row["Close"]), 2)}
        for ts, row in session.iterrows()
    ]
    first = points[0]["value"] if points else None
    last = points[-1]["value"] if points else None
    change_pct = round((last / first - 1) * 100, 2) if first and last else None
    status = "preopen" if is_preopen and selected_date == today and not points else "intraday"
    if selected_date != today:
        status = "last_session"
    elif now > close_dt:
        status = "closed"

    return sanitize_float_values({
        "key": key,
        "symbol": cfg["symbol"],
        "label": cfg["label"],
        "interval": interval,
        "timezone": cfg["timezone"],
        "session_date": selected_date.isoformat(),
        "status": status,
        "points": points,
        "start": first,
        "last": last,
        "change_pct": change_pct,
    })


@app.get("/api/market/intraday")
def api_market_intraday(interval: str = "5m"):
    """大盤當日分時曲線；非營業日回最後交易日，開盤前 10 分鐘準備當日。"""
    return {
        "twii": _market_intraday_session("twii", interval=interval),
        "spx": _market_intraday_session("spx", interval=interval),
    }

def _recommend_watch_levels(ind: dict, market: dict | None = None) -> Optional[dict]:
    """根據技術指標推算觀察清單的建議目標價位。

    回傳 {target_entry, target_add, target_profit, target_stop}；資料不足回 None。
    保守邏輯：
      - entry 取 MA20 與 現價*0.97 較低者（等小回再進）；大盤危險時再下修 2%
      - add 為 entry 再下 5%
      - profit 若 52 週高至少高於 entry 10%，取 high52，否則 entry*1.25
      - stop 取 MA60 與 entry*0.90 中較大者（確保低於 entry），避免過深
    """
    price = ind.get("price")
    if not price:
        return None
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")
    high52 = ind.get("high52")
    market_risk = (market or {}).get("risk_level", "safe")

    entry_candidates = [v for v in (ma20, price * 0.97) if v]
    entry = min(entry_candidates) if entry_candidates else price * 0.97
    if market_risk == "danger":
        entry *= 0.98  # 大盤危險再退一步
    entry = round(entry, 2)

    add = round(entry * 0.95, 2)

    if high52 and high52 > entry * 1.10:
        profit = round(high52, 2)
    else:
        profit = round(entry * 1.25, 2)

    stop_candidates = [v for v in (ma60, entry * 0.90) if v and v < entry]
    stop = round(max(stop_candidates), 2) if stop_candidates else round(entry * 0.90, 2)

    return {
        "target_entry": entry,
        "target_add": add,
        "target_profit": profit,
        "target_stop": stop,
    }


def _recommend_position(d: dict, market: dict | None) -> dict:
    """根據持倉指標 + 大盤狀態產生操作建議。

    回傳：{action, urgency, color, reason, stop_loss, take_profit}
    """
    cur = d.get("current_price") or d["cost_price"]
    cost = d["cost_price"]
    pnl_pct = (cur/cost - 1) * 100
    rsi = d.get("rsi") or 50
    beta = d.get("beta") or 1
    ma20 = d.get("ma20")
    high52 = d.get("high52")
    day = d.get("change_1d") or 0
    near_high = (cur >= high52 * 0.98) if high52 else False
    market_risk = (market or {}).get("risk_level", "safe")

    # ── 規則優先序：危險 → 警告 → 機會 → 中性 ──
    # 1. 任何虧損 + RSI 超買 + 高 β + 今日續跌 → 立即停損
    if pnl_pct < 0 and rsi >= 75 and beta >= 1.0 and day < 0:
        loss_severity = "嚴重虧損" if pnl_pct <= -5 else "虧損"
        return {
            "action": "立即停損 50%",
            "urgency": "danger",
            "color": "bg-red-700",
            "reason": f"{loss_severity} {pnl_pct:.1f}% + RSI {rsi:.0f} 超買 + β {beta:.2f} + 今日續跌 → 災難組合",
            "stop_loss": round(cur * 0.97, 2),
        }
    # 1b. 大幅虧損且 RSI 還超買
    if pnl_pct <= -3 and rsi >= 75 and beta >= 1.0:
        return {
            "action": "立即停損 50%",
            "urgency": "danger",
            "color": "bg-red-700",
            "reason": f"虧損 {pnl_pct:.1f}% + RSI {rsi:.0f} 超買 + β {beta:.2f} 高波動",
            "stop_loss": round(cur * 0.97, 2),
        }
    # 2. 觸及機械式停損 -10%
    if pnl_pct <= -10:
        return {
            "action": "立即停損出場",
            "urgency": "danger",
            "color": "bg-red-700",
            "reason": f"虧損 {pnl_pct:.1f}% 觸及 -10% 紅線",
            "stop_loss": cur,
        }
    # 3. 達高位 RSI 嚴重超買 + 高 β → 減碼鎖利
    if rsi >= 77 and beta >= 1.2 and near_high:
        return {
            "action": "賣出 50% 鎖利",
            "urgency": "warning",
            "color": "bg-yellow-600",
            "reason": f"RSI {rsi:.0f} 超買 + β {beta:.2f} 下行風險高 + 已達 52週高",
            "stop_loss": round(ma20, 2) if ma20 else None,
        }
    # 4. 跌破 -7% 預警
    if pnl_pct <= -7:
        return {
            "action": "減碼 50% 觀察",
            "urgency": "warning",
            "color": "bg-orange-600",
            "reason": f"虧損 {pnl_pct:.1f}% 接近停損線",
            "stop_loss": round(cur * 0.97, 2),
        }
    # 5. 大盤危險 + 任何持倉 RSI 過高
    if market_risk == "danger" and rsi >= 70:
        return {
            "action": "減碼 30%",
            "urgency": "warning",
            "color": "bg-orange-600",
            "reason": f"大盤危險 + RSI {rsi:.0f} 偏高",
            "stop_loss": round(ma20, 2) if ma20 else None,
        }
    # 6. 獲利 ≥ 20% 達停利
    if pnl_pct >= 20 and rsi >= 70:
        return {
            "action": "停利 30~50%",
            "urgency": "info",
            "color": "bg-emerald-700",
            "reason": f"獲利 {pnl_pct:+.1f}% + RSI {rsi:.0f} 高位 → 分批了結",
            "take_profit": cur,
        }
    # 7. 強勢創高 + 放量 + 低 β → 加碼
    if near_high and beta < 0.7 and 60 <= rsi <= 75:
        return {
            "action": "可加碼",
            "urgency": "info",
            "color": "bg-blue-700",
            "reason": f"創 52週高 + β {beta:.2f} 低波動 + RSI {rsi:.0f} 健康",
            "stop_loss": round(ma20, 2) if ma20 else None,
        }
    # 8. RSI 超賣 + 已虧損 → 觀望
    if rsi <= 30 and pnl_pct < 0:
        return {
            "action": "觀察反彈",
            "urgency": "neutral",
            "color": "bg-gray-600",
            "reason": f"RSI {rsi:.0f} 超賣 + 虧損 {pnl_pct:.1f}%，等反彈再決定",
            "stop_loss": round(cur * 0.95, 2),
        }
    # 9. 預設：持有
    return {
        "action": "持有",
        "urgency": "neutral",
        "color": "bg-gray-600",
        "reason": f"RSI {rsi:.0f} / β {beta:.2f} / 損益 {pnl_pct:+.1f}% 無強訊號",
        "stop_loss": round(ma20, 2) if ma20 else None,
    }

def _run_backtest_for_position(position: dict, months: int = 6) -> dict:
    symbol = position["symbol"]
    shares = float(position["shares"])
    cost_price = float(position["cost_price"])
    period = "2y" if months > 12 else "1y"
    h, source = fetch_history(symbol, period=period)
    if len(h) < 2:
        return {
            "symbol": symbol,
            "name": position.get("name") or symbol,
            "error": "no history",
        }

    cutoff = pd.Timestamp(datetime.now() - timedelta(days=months * 31))
    h = h[h.index >= cutoff]
    if len(h) < 2:
        return {
            "symbol": symbol,
            "name": position.get("name") or symbol,
            "error": "not enough history",
        }

    close = h["Close"].astype(float)
    first = float(close.iloc[0])
    last = float(close.iloc[-1])
    current_value = last * shares
    buy_hold_value = first * shares
    cost_value = cost_price * shares
    if position.get("currency") == "USD":
        current_value *= USD_TWD
        buy_hold_value *= USD_TWD
        cost_value *= USD_TWD

    daily = close.pct_change().dropna()
    cumulative = close / first - 1
    peak = cumulative.cummax()
    drawdown = cumulative - peak
    volatility = float(daily.std() * math.sqrt(252) * 100) if len(daily) else 0

    return {
        "symbol": symbol,
        "name": position.get("name") or symbol,
        "source": source,
        "days": int(len(h)),
        "first_date": h.index[0].date().isoformat(),
        "last_date": h.index[-1].date().isoformat(),
        "start_price": round(first, 2),
        "end_price": round(last, 2),
        "period_return_pct": round((last / first - 1) * 100, 2),
        "position_pnl": round(current_value - cost_value, 0),
        "buy_hold_pnl": round(current_value - buy_hold_value, 0),
        "max_drawdown_pct": round(float(drawdown.min() * 100), 2),
        "volatility_pct": round(volatility, 2),
    }

@app.get("/api/backtest")
def api_backtest(months: int = 6):
    months = max(1, min(months, 24))
    conn = get_db()
    rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.close()
    positions = [
        dict(r) for r in rows
        if not _is_test_symbol(r["symbol"], r["name"], r["category"])
    ]
    items = [_run_backtest_for_position(p, months=months) for p in positions]
    valid = [x for x in items if not x.get("error")]

    def _finite(v) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return f if math.isfinite(f) else 0.0

    total_position_pnl = sum(_finite(x.get("position_pnl")) for x in valid)
    total_buy_hold_pnl = sum(_finite(x.get("buy_hold_pnl")) for x in valid)
    returns = [_finite(x.get("period_return_pct")) for x in valid]
    avg_return = (sum(returns) / len(returns)) if returns else 0.0
    return sanitize_float_values({
        "months": months,
        "items": items,
        "summary": {
            "valid_count": len(valid),
            "total_position_pnl": round(total_position_pnl, 0),
            "total_buy_hold_pnl": round(total_buy_hold_pnl, 0),
            "avg_return_pct": round(avg_return, 2),
        },
    })


def _compute_fees(price: float, shares: float, currency: str, side: str, fees_cfg: dict, is_etf: bool) -> dict:
    """Calculate brokerage fees + taxes for a single side (buy/sell).

    Returns {fee, tax, total} in the symbol's native currency.
    """
    notional = float(price) * float(shares)
    if currency == "USD":
        rate = float(fees_cfg.get("us_fee_rate", 0.005) or 0)
        min_fee = float(fees_cfg.get("us_min_fee", 0) or 0)
        sec_rate = float(fees_cfg.get("us_sec_fee_rate", 0) or 0)
        fee = max(notional * rate, min_fee)
        # SEC 規費只在賣出時收取
        tax = notional * sec_rate if side == "sell" else 0.0
    else:  # TWD
        if side == "buy":
            rate = float(fees_cfg.get("tw_buy_fee_rate", 0.001425 * 0.6) or 0)
        else:
            rate = float(fees_cfg.get("tw_sell_fee_rate", 0.001425 * 0.6) or 0)
        min_fee = float(fees_cfg.get("tw_min_fee", 20) or 0)
        fee = max(notional * rate, min_fee) if notional > 0 else 0.0
        if side == "sell":
            tax_rate = float(fees_cfg.get("tw_sell_tax_rate_etf" if is_etf else "tw_sell_tax_rate_stock", 0.003) or 0)
            tax = notional * tax_rate
        else:
            tax = 0.0
    return {"fee": round(fee, 4), "tax": round(tax, 4), "total": round(fee + tax, 4)}


def _annualized_return(total_return_pct: float, days_held: float) -> Optional[float]:
    """Convert total return % over `days_held` to annualized return %.

    Formula: (1 + total)^(365 / days) - 1
    Returns None when days_held < 7 (too short to annualize meaningfully).
    """
    if days_held is None or days_held < 7:
        return None
    try:
        growth = 1.0 + total_return_pct / 100.0
        if growth <= 0:
            return -100.0  # full loss
        return round((growth ** (365.0 / days_held) - 1) * 100, 2)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def _is_etf_symbol(symbol: str, quote_type: Optional[str] = None) -> bool:
    """Best-effort ETF detection for fee/tax rate selection."""
    if quote_type and str(quote_type).upper() == "ETF":
        return True
    base = symbol.split(".")[0]
    if base.startswith("00") and 4 <= len(base) <= 6:
        return True  # TW ETF code pattern
    return False


def _compute_portfolio_snapshot_for_zone(positions: list[dict], fees_cfg: dict) -> dict:
    """Aggregate one zone's positions into snapshot fields (TWD-denominated).

    positions: rows from api_portfolio's enriched output (must include
    current_price, cost_price, shares, currency, net_pnl already in TWD).
    """
    cost = value = net_pnl = 0.0
    for d in positions:
        cur = d.get("current_price") or d["cost_price"]
        rate = USD_TWD if d.get("currency") == "USD" else 1.0
        cost += d["cost_price"] * d["shares"] * rate
        value += cur * d["shares"] * rate
        net_pnl += float(d.get("net_pnl") or 0)
    return {
        "total_cost": round(cost, 0),
        "total_value": round(value, 0),
        "total_pnl": round(value - cost, 0),
        "total_net_pnl": round(net_pnl, 0),
        "position_count": len(positions),
    }


def _record_intraday_portfolio_snapshot(
    conn,
    tw_positions: list[dict],
    us_positions: list[dict],
    fees_cfg: dict,
    now_dt: Optional[datetime] = None,
) -> None:
    now_dt = now_dt or datetime.now()
    bucket_minute = (now_dt.minute // 10) * 10
    bucket_ts = now_dt.replace(minute=bucket_minute, second=0, microsecond=0).isoformat(timespec="minutes")
    trade_date = now_dt.date().isoformat()
    for zone, plist in (("tw", tw_positions), ("us", us_positions)):
        snap = _compute_portfolio_snapshot_for_zone(plist, fees_cfg)
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_intraday_snapshots
               (ts, trade_date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bucket_ts,
                trade_date,
                zone,
                snap["total_cost"],
                snap["total_value"],
                snap["total_pnl"],
                snap["total_net_pnl"],
                snap["position_count"],
            ),
        )


def _backfill_portfolio_snapshots(conn) -> None:
    rows = conn.execute(
        """SELECT p.*, pc.price as current_price, pc.nav, pc.pb, pc.quote_type, pc.data AS price_cache_data
           FROM positions p
           LEFT JOIN price_cache pc ON p.symbol = pc.symbol"""
    ).fetchall()
    positions = [dict(r) for r in rows if not _is_test_symbol(r["symbol"], r["name"], r["category"])]
    if not positions:
        return

    fees_cfg = (load_settings().get("brokerage_fees") or {})
    vault = _get_vault()
    market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
    market = dict(market_row) if market_row else None
    sqlite_dirty = False
    enriched_positions = []
    earliest_purchase = None
    for row in positions:
        enriched, changed = _enrich_position_for_portfolio(conn, row, market, fees_cfg, vault)
        sqlite_dirty = sqlite_dirty or changed
        enriched_positions.append(enriched)
        p_date = _normalize_purchase_date(enriched.get("purchase_date"))
        if p_date:
            dt = datetime.fromisoformat(p_date).date()
            earliest_purchase = dt if earliest_purchase is None else min(earliest_purchase, dt)
    if sqlite_dirty:
        conn.commit()
    if earliest_purchase is None:
        return

    yesterday = date.today() - timedelta(days=1)
    if earliest_purchase > yesterday:
        return

    existing_rows = conn.execute(
        "SELECT date, zone FROM portfolio_snapshots WHERE date BETWEEN ? AND ?",
        (earliest_purchase.isoformat(), yesterday.isoformat()),
    ).fetchall()
    existing_pairs = {(row["date"], row["zone"]) for row in existing_rows}

    zone_daily_values = {"tw": {}, "us": {}}
    for pos in enriched_positions:
        purchase_date_str = _normalize_purchase_date(pos.get("purchase_date"))
        if not purchase_date_str:
            continue
        purchase_dt = datetime.fromisoformat(purchase_date_str).date()
        if purchase_dt > yesterday:
            continue
        hist = _fetch_daily_history_from_date(pos["symbol"], purchase_dt, yesterday)
        if len(hist) == 0 or "Close" not in hist:
            continue
        zone = "tw" if (pos.get("currency") == "TWD" or pos["symbol"].endswith(".TW") or pos["symbol"].endswith(".TWO")) else "us"
        currency = pos.get("currency") or "TWD"
        rate = USD_TWD if currency == "USD" else 1.0
        is_etf = bool(pos.get("is_etf"))
        cost_total = pos["cost_price"] * pos["shares"] * rate
        buy_fees = _compute_fees(pos["cost_price"], pos["shares"], currency, "buy", fees_cfg, is_etf)
        for ts, hist_row in hist.iterrows():
            snap_date = ts.date().isoformat()
            if (snap_date, zone) in existing_pairs:
                continue
            close_price = _safe_float(hist_row.get("Close"))
            if close_price is None:
                continue
            sell_fees = _compute_fees(close_price, pos["shares"], currency, "sell", fees_cfg, is_etf)
            market_value = close_price * pos["shares"] * rate
            gross_native = (close_price - pos["cost_price"]) * pos["shares"]
            net_native = gross_native - buy_fees["total"] - sell_fees["total"]
            payload = zone_daily_values[zone].setdefault(
                snap_date,
                {"total_cost": 0.0, "total_value": 0.0, "total_net_pnl": 0.0, "position_count": 0},
            )
            payload["total_cost"] += cost_total
            payload["total_value"] += market_value
            payload["total_net_pnl"] += net_native * rate
            payload["position_count"] += 1

    inserts = []
    for zone, date_map in zone_daily_values.items():
        for snap_date, payload in sorted(date_map.items()):
            total_cost = round(payload["total_cost"], 0)
            total_value = round(payload["total_value"], 0)
            inserts.append(
                (
                    snap_date,
                    zone,
                    total_cost,
                    total_value,
                    round(total_value - total_cost, 0),
                    round(payload["total_net_pnl"], 0),
                    payload["position_count"],
                )
            )
    if inserts:
        conn.executemany(
            """INSERT OR IGNORE INTO portfolio_snapshots
               (date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            inserts,
        )
        conn.commit()


def _record_portfolio_snapshot(conn=None) -> None:
    """Persist today's per-zone aggregate to portfolio_snapshots.

    Idempotent on (date, zone) — same-day repeated calls overwrite the day's
    value with the latest one, but past days remain untouched even when a
    user later edits a position's cost / shares / purchase_date.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        rows = conn.execute("""
            SELECT p.*, pc.price as current_price, pc.nav, pc.pb, pc.quote_type, pc.data AS price_cache_data
            FROM positions p
            LEFT JOIN price_cache pc ON p.symbol = pc.symbol
        """).fetchall()
        positions = [dict(r) for r in rows if not _is_test_symbol(r["symbol"], r["name"], r["category"])]
        if not positions:
            return
        fees_cfg = (load_settings().get("brokerage_fees") or {})
        vault = _get_vault()
        market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
        market = dict(market_row) if market_row else None
        sqlite_dirty = False
        enriched_positions = []
        for row in positions:
            enriched, changed = _enrich_position_for_portfolio(conn, row, market, fees_cfg, vault)
            sqlite_dirty = sqlite_dirty or changed
            enriched_positions.append(enriched)
        if sqlite_dirty:
            conn.commit()

        today = date.today().isoformat()
        tw_positions, us_positions = _split_positions_by_zone(enriched_positions)
        for zone, plist in (("tw", tw_positions), ("us", us_positions)):
            snap = _compute_portfolio_snapshot_for_zone(plist, fees_cfg)
            conn.execute(
                """INSERT OR REPLACE INTO portfolio_snapshots
                   (date, zone, total_cost, total_value, total_pnl, total_net_pnl, position_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (today, zone, snap["total_cost"], snap["total_value"],
                 snap["total_pnl"], snap["total_net_pnl"], snap["position_count"]),
            )
        _record_intraday_portfolio_snapshot(conn, tw_positions, us_positions, fees_cfg)
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def _split_positions_by_zone(positions: list[dict]) -> tuple[list[dict], list[dict]]:
    tw_positions = [
        p for p in positions
        if (p.get("currency") == "TWD" or p["symbol"].endswith(".TW") or p["symbol"].endswith(".TWO"))
    ]
    us_positions = [p for p in positions if p not in tw_positions]
    return tw_positions, us_positions


def _enrich_position_for_portfolio(
    conn,
    row: dict,
    market: Optional[dict],
    fees_cfg: dict,
    vault: Optional[Path],
) -> tuple[dict, bool]:
    d = dict(row)
    sqlite_dirty = False

    if vault and (
        not d.get("purchase_date")
        or not d.get("name")
        or not d.get("category")
        or not d.get("currency")
    ):
        obsidian_pos = _obsidian_position_fallback(vault, d["symbol"])
        if obsidian_pos:
            for field in ("purchase_date", "name", "category", "currency"):
                if not d.get(field) and obsidian_pos.get(field) not in (None, ""):
                    d[field] = obsidian_pos[field]
                    conn.execute(f"UPDATE positions SET {field}=? WHERE id=?", (obsidian_pos[field], d["id"]))
                    sqlite_dirty = True

    d, meta_dirty = _enrich_quote_meta_for_symbol(conn, d)
    sqlite_dirty = sqlite_dirty or meta_dirty
    cur = d.get("current_price") or d["cost_price"]
    cost_price = d["cost_price"]
    shares = d["shares"]
    currency = d["currency"] or "TWD"
    nav = d.get("nav")
    quote_type = d.get("quote_type")
    pb = d.get("pb")
    is_etf = d.get("is_etf", _is_etf_symbol(d["symbol"], quote_type))

    cost_total = cost_price * shares
    val_total = cur * shares
    buy_fees = _compute_fees(cost_price, shares, currency, "buy", fees_cfg, is_etf)
    sell_fees = _compute_fees(cur, shares, currency, "sell", fees_cfg, is_etf)
    gross_pnl_native = val_total - cost_total
    net_pnl_native = gross_pnl_native - buy_fees["total"] - sell_fees["total"]

    rate = USD_TWD if currency == "USD" else 1.0
    cost_total_twd = cost_total * rate
    val_total_twd = val_total * rate
    net_pnl_twd = net_pnl_native * rate

    annualized = None
    annualized_status = "missing_purchase_date"
    purchase_date_str = _normalize_purchase_date(d.get("purchase_date"))
    d["purchase_date"] = purchase_date_str
    if purchase_date_str:
        try:
            purchase_date = datetime.fromisoformat(purchase_date_str).date()
            days_held = (date.today() - purchase_date).days
            if days_held >= 0 and cost_price:
                d["days_held"] = days_held
                total_return_pct = (cur / cost_price - 1) * 100
                annualized = _annualized_return(total_return_pct, days_held)
                annualized_status = "ok" if annualized is not None else "too_short"
        except (ValueError, TypeError):
            pass

    d["pnl"] = round(val_total_twd - cost_total_twd, 0)
    d["pnl_pct"] = round((cur / cost_price - 1) * 100, 2) if cost_price else None
    d["net_pnl"] = round(net_pnl_twd, 0)
    d["net_pnl_pct"] = round(net_pnl_native / cost_total * 100, 2) if cost_total else None
    d["fees"] = {
        "buy_fee": buy_fees["fee"],
        "sell_fee": sell_fees["fee"],
        "sell_tax": sell_fees["tax"],
        "total_native": round(buy_fees["total"] + sell_fees["total"], 2),
        "total_twd": round((buy_fees["total"] + sell_fees["total"]) * rate, 0),
        "currency": currency,
    }
    d["annualized_return_pct"] = annualized
    d["annualized_status"] = annualized_status
    d["market_value"] = round(val_total_twd, 0)
    d["cost_total"] = round(cost_total_twd, 0)
    d["recommendation"] = _recommend_position(d, market)
    return d, sqlite_dirty


def _enrich_quote_meta_for_symbol(conn, row: dict) -> tuple[dict, bool]:
    d = dict(row)
    sqlite_dirty = False
    nav, quote_type, pb = _hydrate_price_cache_meta(d)
    cur = d.get("current_price") or d.get("cost_price")
    is_etf = _is_etf_symbol(d["symbol"], quote_type)

    if is_etf and (nav is None or quote_type is None or pb is None):
        info_payload = _get_yf_quote_info(d["symbol"], want_info=True)
        changed = {}
        if nav is None and info_payload.get("nav") is not None:
            nav = info_payload["nav"]
            changed["nav"] = nav
        if not quote_type and info_payload.get("quote_type"):
            quote_type = info_payload["quote_type"]
            changed["quote_type"] = quote_type
        if pb is None and info_payload.get("pb") is not None:
            pb = info_payload["pb"]
            changed["pb"] = pb
        if changed:
            if info_payload.get("realtime") is not None and not d.get("current_price"):
                d["current_price"] = info_payload["realtime"]
                cur = d["current_price"]
                changed["price"] = d["current_price"]
            store_price_cache(conn.cursor(), d["symbol"], changed)
            sqlite_dirty = True
    elif not is_etf and pb is None:
        info_payload = _get_yf_quote_info(d["symbol"], want_info=True)
        changed = {}
        if info_payload.get("pb") is not None:
            pb = info_payload["pb"]
            changed["pb"] = pb
        if info_payload.get("quote_type") and not quote_type:
            quote_type = info_payload["quote_type"]
            changed["quote_type"] = quote_type
        if changed:
            if info_payload.get("realtime") is not None and not d.get("current_price"):
                d["current_price"] = info_payload["realtime"]
                cur = d["current_price"]
                changed["price"] = d["current_price"]
            store_price_cache(conn.cursor(), d["symbol"], changed)
            sqlite_dirty = True

    premium_pct = None
    premium_status = "not_etf"
    if nav and nav > 0 and cur:
        premium_pct = round((cur / nav - 1) * 100, 2)
        premium_status = "ok"
    elif is_etf:
        premium_status = "missing_nav" if not nav else "missing_price"

    d["current_price"] = cur
    d["nav"] = nav
    d["pb"] = round(pb, 2) if pb is not None else None
    d["quote_type"] = quote_type
    d["is_etf"] = is_etf
    d["premium_pct"] = premium_pct
    d["premium_status"] = premium_status
    return d, sqlite_dirty


def _persist_market_state(conn, market: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO market_state
           (id, ts, vix,
            twii, twii_ma20, twii_ma60, twii_ma120,
            spx,  spx_ma20,  spx_ma60,  spx_ma120,
            sox, sox_ma60, ndx, ndx_ma20, tnx, dxy,
            risk_level, warnings_count)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (market.get("ts"), market.get("vix"),
         market.get("twii"), market.get("twii_ma20"), market.get("twii_ma60"), market.get("twii_ma120"),
         market.get("spx"),  market.get("spx_ma20"),  market.get("spx_ma60"),  market.get("spx_ma120"),
         market.get("sox"), market.get("sox_ma60"),
         market.get("ndx"), market.get("ndx_ma20"),
         market.get("tnx"), market.get("dxy"),
         market.get("risk_level"), market.get("warnings_count"))
    )


def _apply_indicator_updates_and_alerts(conn, positions: list[dict], watchlist: list[dict], indicators: dict[str, dict], market: dict) -> int:
    c = conn.cursor()
    alerts_created = 0
    for d in positions:
        ind = indicators.get(d["symbol"])
        if ind:
            store_price_cache(c, d["symbol"], ind)
            created = insert_alerts(c, d["symbol"], d["name"], ind, market, position=d)
            alerts_created += created
            if created:
                print(f"  [ALERT] {d['symbol']}: {created} new alert(s)")

    for d in watchlist:
        ind = indicators.get(d["symbol"])
        if ind:
            store_price_cache(c, d["symbol"], ind)
            created = insert_alerts(c, d["symbol"], d["name"], ind, market, watch=d)
            alerts_created += created
            if created:
                print(f"  [ALERT] {d['symbol']}: {created} new alert(s)")
    return alerts_created


def _run_refresh_cycle_sync(*, use_benchmark: bool, indicator_workers: int) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_twii = ex.submit(fetch_benchmark_close, "^TWII")
        f_spx = ex.submit(fetch_benchmark_close, "^GSPC")
        f_market = ex.submit(get_market_state)
        twii = f_twii.result()
        spx = f_spx.result()
        market = f_market.result()

    conn = get_db()
    positions, watchlist = _load_active_portfolio_entities(conn)
    symbol_map = _collect_symbol_benchmarks(positions, watchlist, twii, spx)
    indicators = _fetch_indicator_batch(symbol_map, use_benchmark=use_benchmark, max_workers=indicator_workers)
    _persist_market_state(conn, market)
    alerts_created = _apply_indicator_updates_and_alerts(conn, positions, watchlist, indicators, market)
    conn.commit()
    try:
        _record_portfolio_snapshot(conn)
    except Exception as e:
        print(f"  [WARN] snapshot failed: {e}")
    vault = _get_vault()
    if alerts_created and vault:
        _obsidian_post_write_sync(vault, kinds=("alerts",))
    conn.close()
    return {
        "market": market,
        "positions": positions,
        "watchlist": watchlist,
        "symbol_map": symbol_map,
        "indicators": indicators,
        "alerts_created": alerts_created,
    }


@app.get("/api/portfolio")
def api_portfolio():
    conn = get_db()
    rows = conn.execute("""
        SELECT p.*, pc.price as current_price, pc.rsi, pc.change_1d, pc.beta, pc.ma20, pc.high52,
               pc.nav, pc.pb, pc.quote_type,
               pc.data AS price_cache_data
        FROM positions p
        LEFT JOIN price_cache pc ON p.symbol = pc.symbol
    """).fetchall()
    market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
    market = dict(market_row) if market_row else None
    settings = load_settings()
    fees_cfg = settings.get("brokerage_fees") or {}
    vault = _get_vault()

    out = []
    total_cost = total_value = total_net_pnl = 0.0
    sqlite_dirty = False
    for r in rows:
        enriched, changed = _enrich_position_for_portfolio(conn, dict(r), market, fees_cfg, vault)
        sqlite_dirty = sqlite_dirty or changed
        total_cost += enriched["cost_total"]
        total_value += enriched["market_value"]
        total_net_pnl += enriched["net_pnl"]
        out.append(enriched)

    if sqlite_dirty:
        conn.commit()
    conn.close()

    return sanitize_float_values({
        "positions": out,
        "summary": {
            "total_cost": round(total_cost, 0),
            "total_value": round(total_value, 0),
            "total_pnl": round(total_value - total_cost, 0),
            "total_pnl_pct": round((total_value / total_cost - 1) * 100, 2) if total_cost > 0 else 0,
            "total_net_pnl": round(total_net_pnl, 0),
            "total_net_pnl_pct": round(total_net_pnl / total_cost * 100, 2) if total_cost > 0 else 0,
        },
        "brokerage": {
            "tw_broker": fees_cfg.get("tw_broker"),
            "us_broker": fees_cfg.get("us_broker"),
        },
    })


@app.get("/api/portfolio/trend")
def api_portfolio_trend():
    """Daily portfolio equity curve from immutable snapshots.

    歷史線取自 portfolio_snapshots（monitor / refresh 每次都會 INSERT OR
    REPLACE 當日值；過去日是凍結的，**使用者後續修改持倉、刪除持倉、
    改 cost_price 都不會回溯影響已存的歷史水位**）。

    今日的最後一個點仍然用即時 price_cache 計算，這樣 UI 在盤中也能看到
    當下的資產值動態變化。
    """
    conn = get_db()
    try:
        _backfill_portfolio_snapshots(conn)
    except Exception:
        pass
    snap_rows = conn.execute(
        """SELECT date, zone, total_cost, total_value, total_pnl, total_net_pnl
           FROM portfolio_snapshots
           ORDER BY date ASC"""
    ).fetchall()
    today_str = date.today().isoformat()
    intraday_rows = conn.execute(
        """SELECT ts, trade_date, zone, total_cost, total_value, total_pnl, total_net_pnl
           FROM portfolio_intraday_snapshots
           WHERE trade_date = ?
           ORDER BY ts ASC""",
        (today_str,),
    ).fetchall()
    conn.close()

    def _zone_scale(zone: str) -> float:
        return USD_TWD if zone == "us" else 1.0

    def _round_zone_amount(value, zone: str):
        scale = _zone_scale(zone)
        return round((value or 0) / scale, 2 if zone == "us" else 0)

    trend_tw: list[dict] = []
    trend_us: list[dict] = []
    last_seen = {"tw": None, "us": None}
    for r in snap_rows:
        d = dict(r)
        if d["date"] == today_str:
            # 今日值由 live snapshot 取代（下面補）
            continue
        try:
            ts = int(datetime.fromisoformat(d["date"]).timestamp())
        except ValueError:
            continue
        point = {
            "time": ts,
            "date": d["date"],
            "total_value": _round_zone_amount(d["total_value"], d["zone"]),
            "total_cost": _round_zone_amount(d["total_cost"], d["zone"]),
            "total_pnl": _round_zone_amount(d["total_pnl"], d["zone"]),
            "total_net_pnl": _round_zone_amount(d["total_net_pnl"], d["zone"]),
        }
        if d["zone"] == "tw":
            trend_tw.append(point)
            last_seen["tw"] = point
        elif d["zone"] == "us":
            trend_us.append(point)
            last_seen["us"] = point

    intraday_by_zone = {"tw": trend_tw, "us": trend_us}
    intraday_seen = {"tw": False, "us": False}
    for r in intraday_rows:
        d = dict(r)
        try:
            ts = int(datetime.fromisoformat(d["ts"]).timestamp())
        except ValueError:
            continue
        point = {
            "time": ts,
            "date": d["trade_date"],
            "total_value": _round_zone_amount(d["total_value"], d["zone"]),
            "total_cost": _round_zone_amount(d["total_cost"], d["zone"]),
            "total_pnl": _round_zone_amount(d["total_pnl"], d["zone"]),
            "total_net_pnl": _round_zone_amount(d["total_net_pnl"], d["zone"]),
            "live": True,
        }
        if d["zone"] in intraday_by_zone:
            intraday_by_zone[d["zone"]].append(point)
            intraday_seen[d["zone"]] = True

    # Append today's live snapshot (in-memory; doesn't write to DB so a manual
    # browse doesn't persist a partial-day value).
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT p.*, pc.price as current_price, pc.nav, pc.pb, pc.quote_type, pc.data AS price_cache_data
               FROM positions p
               LEFT JOIN price_cache pc ON p.symbol = pc.symbol"""
        ).fetchall()
        market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
        market = dict(market_row) if market_row else None
        positions = [dict(r) for r in rows if not _is_test_symbol(r["symbol"], r["name"], r["category"])]
        if positions:
            fees_cfg = (load_settings().get("brokerage_fees") or {})
            vault = _get_vault()
            sqlite_dirty = False
            enriched_positions = []
            for row in positions:
                enriched, changed = _enrich_position_for_portfolio(conn, row, market, fees_cfg, vault)
                sqlite_dirty = sqlite_dirty or changed
                enriched_positions.append(enriched)
            if sqlite_dirty:
                conn.commit()
            tw_positions, us_positions = _split_positions_by_zone(enriched_positions)
            now_ts = int(datetime.now().timestamp())
            for zone, plist, arr in (("tw", tw_positions, trend_tw), ("us", us_positions, trend_us)):
                if intraday_seen.get(zone):
                    continue
                snap = _compute_portfolio_snapshot_for_zone(plist, fees_cfg)
                if snap["position_count"] == 0:
                    continue
                point = {
                    "time": now_ts,
                    "date": today_str,
                    "total_value": _round_zone_amount(snap["total_value"], zone),
                    "total_cost": _round_zone_amount(snap["total_cost"], zone),
                    "total_pnl": _round_zone_amount(snap["total_pnl"], zone),
                    "total_net_pnl": _round_zone_amount(snap["total_net_pnl"], zone),
                    "live": True,
                }
                arr.append(point)
        conn.close()
    except Exception:
        pass

    return sanitize_float_values({"tw": trend_tw, "us": trend_us})


@app.get("/api/watchlist")
def api_watchlist():
    conn = get_db()
    watchlist_rows = conn.execute("""
        SELECT w.*, pc.price as current_price, pc.rsi, pc.change_1d, pc.ma20, pc.ma60, pc.beta,
               pc.nav, pc.pb, pc.quote_type, pc.data AS price_cache_data
        FROM watchlist w
        LEFT JOIN price_cache pc ON w.symbol = pc.symbol
    """).fetchall()
    positions_rows = conn.execute("""
        SELECT p.id, p.symbol, p.name, p.category, p.currency, p.target_entry, p.target_profit, p.target_stop,
               pc.price as current_price, pc.rsi, pc.change_1d, pc.ma20, pc.ma60, pc.beta,
               pc.nav, pc.pb, pc.quote_type, pc.data AS price_cache_data
        FROM positions p
        LEFT JOIN price_cache pc ON p.symbol = pc.symbol
    """).fetchall()

    out_map = {}
    # 1. 放入觀察清單的資料
    for r in watchlist_rows:
        d = dict(r)
        d["is_position"] = False
        d["is_watchlist"] = True
        out_map[d["symbol"]] = d

    # 2. 合併持倉的資料
    for r in positions_rows:
        d = dict(r)
        sym = d["symbol"]
        if sym in out_map:
            out_map[sym]["is_position"] = True
            # 如果持倉有設定點位，且觀察清單沒有，用持倉點位補充
            if d.get("target_entry") and not out_map[sym].get("target_entry"):
                out_map[sym]["target_entry"] = d["target_entry"]
            if d.get("target_profit") and not out_map[sym].get("target_profit"):
                out_map[sym]["target_profit"] = d["target_profit"]
            if d.get("target_stop") and not out_map[sym].get("target_stop"):
                out_map[sym]["target_stop"] = d["target_stop"]
        else:
            d["is_position"] = True
            d["is_watchlist"] = False
            d["id"] = None
            out_map[sym] = d

    out = []
    sqlite_dirty = False
    for d in out_map.values():
        d, meta_dirty = _enrich_quote_meta_for_symbol(conn, d)
        sqlite_dirty = sqlite_dirty or meta_dirty
        cur = d.get("current_price")
        rsi = d.get("rsi") or 50

        # 計算優先度分數 (0~100，高=優先)
        priority = 0
        if cur and d.get("target_entry"):
            d["distance_to_entry"] = round((cur/d["target_entry"]-1)*100, 1)
            dist = d["distance_to_entry"]

            if cur <= d["target_entry"] * 1.02:
                d["status"] = "可進場"
                d["status_class"] = "text-emerald-400"
                priority = 100
                # RSI 健康加分
                if 40 <= rsi <= 60:
                    priority += 10
            elif cur <= d["target_entry"] * 1.05:
                d["status"] = "接近進場"
                d["status_class"] = "text-yellow-400"
                priority = 85 - abs(dist) * 2
            elif cur <= d["target_entry"] * 1.08:
                d["status"] = "接近進場"
                d["status_class"] = "text-yellow-400"
                priority = 70 - abs(dist) * 1.5
            elif rsi >= 80:
                d["status"] = "嚴重超買"
                d["status_class"] = "text-red-500"
                priority = 5
            elif rsi >= 75:
                d["status"] = "超買"
                d["status_class"] = "text-red-400"
                priority = 15
            elif rsi <= 25:
                d["status"] = "超賣反彈"
                d["status_class"] = "text-cyan-400"
                priority = 60
            elif rsi <= 35:
                d["status"] = "偏弱觀察"
                d["status_class"] = "text-blue-400"
                priority = 45
            else:
                d["status"] = "等待"
                d["status_class"] = "text-orange-400"
                priority = max(20, 50 - abs(dist))
        elif d.get("is_position"):
            d["status"] = "已持倉"
            d["status_class"] = "text-blue-400"
            priority = 10
        else:
            d["status"] = "資料中"
            d["status_class"] = "text-gray-500"
            priority = 0

        d["priority"] = round(priority, 1)
        out.append(d)

    if sqlite_dirty:
        conn.commit()
    conn.close()

    # 按優先度降序，再按類別
    out.sort(key=lambda x: (-x["priority"], x.get("category") or ""))
    return sanitize_float_values({"watchlist": out})

# Alert type 顯示資訊：中文標籤、tailwind 配色、優先排序權重 (大=越優先)
# color 是 tailwind class，前端直接套用 → 不同 type 用不同色塊辨識，不靠 icon
ALERT_TYPE_META = {
    "STOP_LOSS":       {"label": "停損",     "color": "bg-red-700",    "priority": 100},
    "ENTRY_TRIGGER":   {"label": "進場",     "color": "bg-emerald-700","priority": 80},
    "PROFIT_TARGET":   {"label": "停利",     "color": "bg-yellow-600", "priority": 75},
    "LOSS_WARN":       {"label": "虧損預警", "color": "bg-orange-700", "priority": 70},
    "BELOW_MA20":      {"label": "破均線",   "color": "bg-rose-700",   "priority": 60},
    "RSI_OVERBOUGHT":  {"label": "超買",     "color": "bg-pink-700",   "priority": 50},
    "PROFIT_30":       {"label": "獲利達標", "color": "bg-green-700",  "priority": 45},
    "RSI_OVERSOLD":    {"label": "超賣",     "color": "bg-cyan-700",   "priority": 40},
}

# Emoji + 特殊符號 regex（涵蓋常見表情、符號、箭頭、幾何圖形等）
import re
_EMOJI_RE = re.compile(
    "["                                  # noqa: RUF001
    "\U0001F300-\U0001F5FF"              # symbols & pictographs
    "\U0001F600-\U0001F64F"              # emoticons
    "\U0001F680-\U0001F6FF"              # transport
    "\U0001F700-\U0001F77F"              # alchemical
    "\U0001F780-\U0001F7FF"              # geometric extended
    "\U0001F800-\U0001F8FF"              # arrows-c
    "\U0001F900-\U0001F9FF"              # supplemental symbols
    "\U0001FA00-\U0001FA6F"              # chess
    "\U0001FA70-\U0001FAFF"              # extended-a
    "\U00002600-\U000026FF"              # misc symbols (☀⚡⚠ etc)
    "\U00002700-\U000027BF"              # dingbats (✓✗✅❌ etc)
    "\U0001F1E0-\U0001F1FF"              # flags
    "⌀-⏿"                      # misc technical (⌚⌛)
    "■-◿"                      # geometric (■▲▼ etc)
    "←-⇿"                      # arrows (→←↑↓)
    "︀-️"                      # variation selectors
    "]+",
    flags=re.UNICODE,
)

def _strip_emoji(text):
    if not text:
        return text
    # 先去 emoji，再去多餘空白
    cleaned = _EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _enrich_alert(row: dict) -> dict:
    """Add label/color/sort_priority + strip emoji from message/diagnosis."""
    meta = ALERT_TYPE_META.get(row.get("type"), {"label": row.get("type") or "其他", "color": "bg-gray-700", "priority": 0})
    row["type_label"] = meta["label"]
    row["type_color"] = meta["color"]
    row["sort_priority"] = meta["priority"]
    # 清掉舊資料殘留的 emoji（新警報已不會產生，這層做雙保險）
    row["message"] = _strip_emoji(row.get("message"))
    row["diagnosis"] = _strip_emoji(row.get("diagnosis"))
    return row


@app.get("/api/alerts")
def api_alerts(limit: int = 50):
    conn = get_db()
    rows = conn.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {
        "alerts": [_enrich_alert(dict(r)) for r in rows],
        "type_meta": ALERT_TYPE_META,
    }

class AckRequest(BaseModel):
    id: int

@app.post("/api/alerts/ack")
def api_ack_alert(req: AckRequest):
    conn = get_db()
    row = conn.execute("SELECT * FROM alerts WHERE id=?", (req.id,)).fetchone()
    conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (req.id,))
    conn.commit()
    vault = _get_vault()
    if vault and row:
        updated = dict(row)
        updated["acknowledged"] = 1
        _obsidian_write_alert(vault, updated)
        _obsidian_post_write_sync(vault, kinds=("alerts",))
    conn.close()
    return {"ok": True}

@app.post("/api/refresh")
async def api_refresh():
    """Manually refresh prices and evaluate alerts immediately (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time
    t0 = _time.time()

    # Acquire the shared refresh lock so we never race monitor_loop. Without
    # this two refresh cycles compete for the yfinance connection pool, which
    # makes Yahoo silently drop a fraction of the requests (the US history
    # calls in particular came back empty, so manual refresh appeared to do
    # nothing for US positions).
    async with _refresh_lock:
        return await asyncio.to_thread(_run_api_refresh_sync, t0)


def _run_api_refresh_sync(t0: float) -> dict:
    """Synchronous body of the manual refresh, held under _refresh_lock."""
    import time as _time
    import gc

    cycle = _run_refresh_cycle_sync(use_benchmark=False, indicator_workers=16)
    # yfinance >= 0.2 leaks SQLite FDs from its per-Ticker timezone cache
    # (~10 stranded `tkr-tz` FDs per refresh). The connections only release
    # when the Ticker objects are garbage-collected; a forced full GC after
    # each refresh keeps the process from hitting ulimit -n.
    gc.collect()
    elapsed = round(_time.time() - t0, 1)
    return {
        "refreshed": len(cycle["indicators"]),
        "alerts_created": cycle["alerts_created"],
        "market": cycle["market"],
        "elapsed_seconds": elapsed,
    }

@app.get("/api/history/{symbol}")
def api_history(symbol: str, period: str = "6mo"):
    """歷史 OHLC 數據 (給 K 線圖使用)。"""
    try:
        cfg = _chart_period_config(period)
        interval = cfg["interval"]
        
        # 決定拉取較大的歷史區間，以提供 MA20 / MA60 足夠的 warm-up 緩衝
        calc_periods = {
            "1m": "5d",
            "5m": "5d",
            "15m": "10d",
            "30m": "20d",
            "1h": "30d",
            "4h": "60d",
            "1d": "5d",
            "5d": "1mo",
            "1mo": "6mo",
            "3mo": "1y",
            "6mo": "1y",
            "1y": "2y",
            "2y": "5y",
        }
        calc_period = calc_periods.get(period, "1y")

        if interval == "1d":
            h, source = fetch_history(symbol, period=calc_period)
        else:
            h = fetch_intraday_history(symbol, period=calc_period, interval=interval)
            source = "yfinance_intraday"
            
        h = h.dropna(subset=["Open", "High", "Low", "Close"])
        if len(h) == 0:
            return {"error": "no data"}
            
        # 統一將 index 轉為 naive UTC datetime
        # Must tz_convert('UTC') before tz_localize(None): pandas 3 preserves
        # wall-clock values (e.g. 09:00 Asia/Taipei) without converting, which
        # makes .timestamp() return wrong Unix values on non-UTC machines.
        h = h.copy()
        if h.index.tz is not None:
            h.index = h.index.tz_convert('UTC').tz_localize(None)
        
        # 在完整歷史上計算滾動平均線
        close = h["Close"]
        ma20_series = close.rolling(20).mean()
        ma60_series = close.rolling(60).mean()
        
        # 依據資料最後一個時間點，向前裁切出用戶實際請求的週期區間
        import datetime
        latest_ts = h.index[-1]
        period_durations = {
            "1m": datetime.timedelta(hours=2),
            "5m": datetime.timedelta(hours=10),
            "15m": datetime.timedelta(hours=30),
            "30m": datetime.timedelta(hours=60),
            "1h": datetime.timedelta(days=5),
            "4h": datetime.timedelta(days=20),
            "1d": datetime.timedelta(days=1),
            "5d": datetime.timedelta(days=5),
            "1mo": datetime.timedelta(days=31),
            "3mo": datetime.timedelta(days=92),
            "6mo": datetime.timedelta(days=183),
            "1y": datetime.timedelta(days=366),
            "2y": datetime.timedelta(days=731),
        }
        duration = period_durations.get(period, datetime.timedelta(days=183))
        cutoff_date = latest_ts - duration
        
        h_sliced = h[h.index >= cutoff_date]
        if len(h_sliced) == 0:
            h_sliced = h
            
        candles = []
        volumes = []
        for ts, row in h_sliced.iterrows():
            t = int(ts.timestamp())
            candles.append({
                "time": t,
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
            })
            volumes.append({
                "time": t,
                "value": int(row["Volume"]),
                "color": "rgba(239,68,68,0.6)" if row["Close"] >= row["Open"] else "rgba(16,185,129,0.6)",
            })
            
        ma20 = []
        ma60 = []
        sliced_timestamps = set(h_sliced.index)
        
        for ts, val in ma20_series.dropna().items():
            if ts in sliced_timestamps:
                ma20.append({"time": int(ts.timestamp()), "value": round(float(val), 2)})
        for ts, val in ma60_series.dropna().items():
            if ts in sliced_timestamps:
                ma60.append({"time": int(ts.timestamp()), "value": round(float(val), 2)})
                
        return sanitize_float_values({
            "symbol": symbol,
            "period": period,
            "interval": interval,
            "source": source,
            "candles": candles,
            "volumes": volumes,
            "ma20": ma20,
            "ma60": ma60,
        })
    except Exception as e:
        return {"error": str(e)}


# In-process TTL cache for technical matrix payloads. fetch_history +
# fetch_benchmark_close are the slow part; reusing for 5 minutes mirrors the
# monitor loop cadence so UI re-opens during a single cycle are near-instant.
_TECH_MATRIX_CACHE: dict[tuple, tuple[float, dict]] = {}
_TECH_MATRIX_TTL_SECONDS = 300


def _tech_matrix_cache_get(key: tuple) -> Optional[dict]:
    entry = _TECH_MATRIX_CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if time.time() >= expires_at:
        _TECH_MATRIX_CACHE.pop(key, None)
        return None
    return payload


def _tech_matrix_cache_set(key: tuple, payload: dict) -> None:
    _TECH_MATRIX_CACHE[key] = (time.time() + _TECH_MATRIX_TTL_SECONDS, payload)


@app.get("/api/technical-matrix/{symbol}")
def api_technical_matrix(symbol: str, period: str = "1y", include_history_markers: bool = False, bypass_cache: bool = False):
    """17-dimensional technical matrix with chart markers.

    Pass include_history_markers=true to backfill high-value markers across
    every bar in the requested period (engulfing, hammer, pin bar, sweep,
    trap, BOS/ChoCh, RSI divergence, ±2σ/±3σ, vol spike/absorb). Off by
    default to keep the chart uncluttered.
    """
    try:
        matrix = _build_technical_matrix_payload(
            symbol, period, use_cache=not bypass_cache, include_history_markers=include_history_markers
        )
        return sanitize_float_values(matrix)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/smc-analysis/{symbol}")
def api_smc_analysis(
    symbol: str,
    period: str = "6mo",
    swing_length: int = 5,
    internal_swing_length: int = 3,
    close_break: bool = True,
):
    """Quantified Smart Money Concept analysis for one symbol.

    This exposes deterministic SMC concepts and markers so UI, reports, and
    future backtests can share the same output contract.
    """
    try:
        cfg = _chart_period_config(period)
        calc_period = cfg["period"]
        interval = cfg.get("interval", "1d")
        if interval == "1d":
            h, source = fetch_history(symbol, period=calc_period)
        else:
            h = fetch_intraday_history(symbol, period=calc_period, interval=interval)
            source = "yfinance_intraday"
        if h is None or len(h) == 0:
            raise HTTPException(404, "No price history")
        smc_cfg = SMCConfig(
            swing_length=max(2, min(int(swing_length), 50)),
            internal_swing_length=max(2, min(int(internal_swing_length), 20)),
            close_break=bool(close_break),
        )
        payload = build_smc_analysis(h, symbol=symbol, timeframe=period, config=smc_cfg)
        payload["source"] = source
        payload["period"] = period
        return sanitize_float_values(payload)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/smc-backtest/{symbol}")
def api_smc_backtest(
    symbol: str,
    period: str = "1y",
    swing_length: int = 5,
    internal_swing_length: int = 3,
    min_bars: int = 60,
    max_hold_bars: int = 20,
    entry_threshold: int = 8,
    account_equity: float = 100_000,
    risk_pct: float = 0.01,
    require_qualified: bool = True,
):
    """Lookahead-proof SMC event backtest for one symbol."""
    try:
        cfg = _chart_period_config(period)
        calc_period = cfg["period"]
        interval = cfg.get("interval", "1d")
        if interval == "1d":
            h, source = fetch_history(symbol, period=calc_period)
        else:
            h = fetch_intraday_history(symbol, period=calc_period, interval=interval)
            source = "yfinance_intraday"
        if h is None or len(h) == 0:
            raise HTTPException(404, "No price history")
        smc_cfg = SMCConfig(
            swing_length=max(2, min(int(swing_length), 50)),
            internal_swing_length=max(2, min(int(internal_swing_length), 20)),
            entry_threshold=max(1, min(int(entry_threshold), 20)),
        )
        bt_cfg = SMCBacktestConfig(
            min_bars=max(20, min(int(min_bars), 300)),
            max_hold_bars=max(1, min(int(max_hold_bars), 120)),
            account_equity=max(float(account_equity), 0),
            risk_pct=max(0, min(float(risk_pct), 0.05)),
            require_qualified=bool(require_qualified),
        )
        payload = run_smc_event_backtest(h, symbol=symbol, timeframe=period, smc_config=smc_cfg, backtest_config=bt_cfg)
        payload["source"] = source
        payload["period"] = period
        return sanitize_float_values(payload)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/research/backfill/{symbol}")
def api_backfill_research(symbol: str, lookback_days: int = 180, step_days: int = 5):
    """回填單一標的的 17D 歷史偏向（用歷史 OHLCV 逐日重算）。"""
    try:
        result = _backfill_technical_matrix_history(symbol, lookback_days=lookback_days, step_days=step_days)
        return {"ok": True, "symbol": symbol, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/research/store-all")
def api_store_all_research():
    """批次：把觀察清單 + 持倉所有標的的財報（ETF 存基金概況）+ 基本面 +
    17D 矩陣落地到 SQL 與 Obsidian。"""
    conn = get_db()
    syms = []
    seen = set()
    for tbl in ("positions", "watchlist"):
        for r in conn.execute(f"SELECT symbol, name, category FROM {tbl}").fetchall():
            if r["symbol"] in seen or _is_test_symbol(r["symbol"], r["name"], r["category"]):
                continue
            seen.add(r["symbol"])
            syms.append(r["symbol"])
    conn.close()

    out = {}
    for sym in syms:
        try:
            fundamentals = fetch_fundamentals(sym)
            financials = fetch_financial_history(sym)
            matrix = None
            try:
                matrix = _build_technical_matrix_payload(sym, "6mo")
            except Exception:
                matrix = None
            _persist_symbol_research(sym, fundamentals, matrix, financials)
            out[sym] = {
                "type": "fund" if fundamentals.get("is_fund") else "stock",
                "quarters": len(financials),
                "matrix": bool(matrix and not matrix.get("error")),
            }
        except Exception as e:
            out[sym] = {"error": str(e)}
        finally:
            import gc
            gc.collect()
    return {"ok": True, "symbols": len(syms), "detail": out}


@app.post("/api/research/backfill-all")
def api_backfill_all(lookback_days: int = 180, step_days: int = 5):
    """回填所有持倉 + 觀察清單標的的 17D 歷史。"""
    conn = get_db()
    syms = set()
    for tbl in ("positions", "watchlist"):
        for r in conn.execute(f"SELECT symbol, name, category FROM {tbl}").fetchall():
            if not _is_test_symbol(r["symbol"], r["name"], r["category"]):
                syms.add(r["symbol"])
    conn.close()
    out = {}
    for sym in sorted(syms):
        try:
            out[sym] = _backfill_technical_matrix_history(sym, lookback_days=lookback_days, step_days=step_days)
        except Exception as e:
            out[sym] = {"errors": 1, "reason": str(e)}
    total_filled = sum(v.get("filled", 0) for v in out.values())
    return {"ok": True, "symbols": len(syms), "total_filled": total_filled, "detail": out}


@app.post("/api/technical-matrix/{symbol}/snapshot")
def api_technical_matrix_snapshot(symbol: str, period: str = "1y", include_history_markers: bool = False):
    """Build and save a 17-dimensional technical matrix snapshot to Obsidian."""
    vault = _get_vault()
    if not vault:
        raise HTTPException(400, "obsidian_vault_path 未設定或路徑不存在")
    try:
        # Snapshot writes a fresh matrix to disk; bypass cache for accuracy.
        matrix = _build_technical_matrix_payload(symbol, period, use_cache=False, include_history_markers=include_history_markers)
        note_path = _obsidian_write_technical_matrix(vault, matrix)
        return sanitize_float_values({
            "ok": True,
            "symbol": symbol,
            "obsidian_path": str(note_path),
            "matrix": matrix,
        })
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


def _build_technical_matrix_payload(
    symbol: str,
    period: str,
    *,
    use_cache: bool = True,
    include_history_markers: bool = False,
) -> dict:
    key = (symbol, period, include_history_markers)
    if use_cache:
        cached = _tech_matrix_cache_get(key)
        if cached is not None:
            return cached
    cfg = _chart_period_config(period)
    interval = cfg["interval"]
    yf_period = cfg["period"]
    context_period = "2y" if period == "2y" else "1y"
    is_tw = bool(_twse_channel(symbol))

    # Network-bound fetches all run in parallel. They are independent and the
    # vast majority of total latency is I/O — sequential calls were 5–10s, the
    # parallel fan-out collapses that to the slowest single call (~1s).
    from concurrent.futures import ThreadPoolExecutor

    def _primary():
        if interval == "1d":
            return ("primary", fetch_history(symbol, period=yf_period))
        return ("primary", (fetch_intraday_history(symbol, period=yf_period, interval=interval), "yfinance_intraday"))

    def _context():
        # Skip when primary fetch is already covering the same daily-period.
        # We still issue the call when the primary is intraday (it has no
        # multi-year span). When yf_period and context_period match we just
        # alias the primary result later.
        if interval == "1d" and yf_period == context_period:
            return ("context", None)
        return ("context", fetch_history(symbol, period=context_period))

    jobs: dict = {
        "primary": _primary,
        "context": _context,
        "benchmarks": lambda: ("benchmarks", fetch_intermarket_benchmarks(symbol)),
        "intraday_1h": lambda: ("intraday_1h", fetch_intraday_history(symbol, period="30d", interval="1h")),
        "intraday_15m": lambda: ("intraday_15m", fetch_intraday_history(symbol, period="14d", interval="15m")),
        "intraday_5m": lambda: ("intraday_5m", fetch_intraday_history(symbol, period="1d", interval="5m")),
        "events": lambda: ("events", fetch_earnings_events(symbol)),
    }
    # Market-specific feeds: skip the irrelevant side to avoid wasted network.
    if is_tw:
        jobs["breadth"] = lambda: ("breadth", fetch_tw_breadth())
        jobs["order_book"] = lambda: ("order_book", fetch_tw_order_book_snapshot(symbol))
    else:
        jobs["options_profile"] = lambda: ("options_profile", fetch_us_options_profile(symbol))

    results: dict = {}
    with ThreadPoolExecutor(max_workers=min(10, len(jobs))) as ex:
        for label, value in ex.map(lambda fn: fn(), jobs.values()):
            results[label] = value

    primary = results.get("primary")
    if isinstance(primary, tuple) and len(primary) == 2:
        h, source = primary
    else:
        h, source = pd.DataFrame(), ""
    if len(h) == 0:
        raise ValueError("no data")
    ctx_result = results.get("context")
    if ctx_result is None:
        context_h, context_source = h, source
    else:
        context_h, context_source = ctx_result
        if len(context_h) == 0:
            context_h, context_source = h, source

    benchmarks = results.get("benchmarks") or {}
    primary_label = "twii" if is_tw else "spx"
    benchmark = benchmarks.get(primary_label, pd.Series(dtype=float))
    intraday_1h = results.get("intraday_1h")
    intraday_15m = results.get("intraday_15m")
    intraday_5m_today = results.get("intraday_5m")
    breadth = results.get("breadth")
    order_book = results.get("order_book")
    options_profile = results.get("options_profile")
    events = results.get("events") or []
    payload = build_technical_matrix(
        symbol,
        h,
        context_history=context_h,
        benchmark_close=benchmark,
        benchmarks=benchmarks,
        intraday_1h=intraday_1h,
        intraday_15m=intraday_15m,
        intraday_5m=intraday_5m_today,
        breadth=breadth,
        order_book=order_book,
        options_profile=options_profile,
        events=events,
        include_history_markers=include_history_markers,
        source=source or "history",
    )
    payload["analysis_context"]["context_source"] = context_source or source or "history"
    payload["analysis_context"]["requested_period"] = period
    payload["analysis_context"]["requested_interval"] = interval
    _tech_matrix_cache_set(key, payload)
    return payload


@app.get("/api/intraday/{symbol}")
def api_intraday(symbol: str, interval: str = "5m"):
    """當日分時收盤價，供觀察列表 hover 小圖使用。"""
    allowed_intervals = {"1m", "2m", "5m", "15m", "30m", "60m"}
    if interval not in allowed_intervals:
        interval = "5m"
    try:
        h = yf.Ticker(symbol).history(period="1d", interval=interval)
        h = h.dropna(subset=["Close"])
        if len(h) == 0:
            return {"error": "no intraday data"}
        if h.index.tz is not None:
            h.index = h.index.tz_convert('UTC').tz_localize(None)
        points = [
            {"time": int(ts.timestamp()), "value": round(float(row["Close"]), 2)}
            for ts, row in h.iterrows()
        ]
        first = points[0]["value"]
        last = points[-1]["value"]
        change_pct = round((last / first - 1) * 100, 2) if first else None
        return sanitize_float_values({
            "symbol": symbol,
            "interval": interval,
            "points": points,
            "start": first,
            "last": last,
            "change_pct": change_pct,
        })
    except Exception as e:
        return {"error": str(e)}

# ─────────────── Position CRUD ───────────────
class PositionCreate(BaseModel):
    symbol: str
    name: Optional[str] = ""
    category: Optional[str] = ""
    shares: float
    cost_price: float
    currency: str = "TWD"
    purchase_date: Optional[str] = None
    target_entry: Optional[float] = None
    target_profit: Optional[float] = None
    target_stop: Optional[float] = None

@app.post("/api/positions")
def api_add_position(p: PositionCreate):
    conn = get_db()
    p_date = _normalize_purchase_date(p.purchase_date) or date.today().isoformat()
    conn.execute(
        "INSERT INTO positions (symbol, name, category, shares, cost_price, currency, purchase_date, target_entry, target_profit, target_stop) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p.symbol, p.name, p.category, p.shares, p.cost_price, p.currency, p_date, p.target_entry, p.target_profit, p.target_stop)
    )
    conn.commit()
    # 新增持倉的部分視為買入交易記錄
    try:
        from datetime import datetime
        created_at = datetime.utcnow().isoformat() + "Z"
        fee, tax = _estimate_trade_fees("buy", p.shares, p.cost_price, p.currency)
        conn.execute(
            """INSERT INTO trades
               (symbol, name, action, shares, price, fee, tax, trade_date, settle_date, currency, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.symbol.upper(), p.name or "", "buy", p.shares, p.cost_price, fee, tax,
             p_date, None, p.currency, "新增持倉自動導入", created_at)
        )
        conn.commit()
    except Exception as e:
        print(f"  [WARN] auto trade record failed: {e}")
    # 立即抓一次該標的的報價並寫入 price_cache，這樣 UI 在新增後第一次
    # 重新整理就能看到正確的現價 / PnL，不必等下一輪 monitor_loop。
    try:
        ind = fetch_indicators(p.symbol, None)
        if ind and "price" in ind:
            c = conn.cursor()
            store_price_cache(c, p.symbol, ind)
            conn.commit()
    except Exception as e:
        print(f"  [WARN] initial fetch for {p.symbol} failed: {e}")
    # 寫一個 snapshot，避免「新增後到 monitor 跑之前資產線是空的」的視覺感
    try:
        _record_portfolio_snapshot(conn)
    except Exception:
        pass
    vault = _get_vault()
    if vault:
        _obsidian_write_position_snapshot(vault, conn, p.symbol)
        _obsidian_post_write_sync(vault, kinds=("positions",))
    conn.close()
    return {"ok": True}

@app.delete("/api/positions/{pid}")
def api_del_position(pid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    conn.execute("DELETE FROM positions WHERE id=?", (pid,))
    conn.commit()
    vault = _get_vault()
    if vault:
        if row:
            note = vault / "Portfolio" / "Positions" / f"{_safe_obsidian_name(row['symbol'])}.md"
            if note.exists():
                note.unlink()
        all_pos = [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
        _obsidian_write_portfolio_index(vault, all_pos)
        _obsidian_post_write_sync(vault, kinds=("positions",))
    conn.close()
    return {"ok": True}

class PositionUpdate(BaseModel):
    shares: Optional[float] = None
    cost_price: Optional[float] = None
    name: Optional[str] = None
    category: Optional[str] = None
    currency: Optional[str] = None
    purchase_date: Optional[str] = None
    target_entry: Optional[float] = None
    target_profit: Optional[float] = None
    target_stop: Optional[float] = None

@app.put("/api/positions/{pid}")
def api_update_position(pid: int, p: PositionUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Position not found")
    updates = []
    params = []
    
    set_fields = p.model_dump(exclude_unset=True)
    if "purchase_date" in set_fields:
        set_fields["purchase_date"] = _normalize_purchase_date(set_fields.get("purchase_date"))
    for field in ("shares", "cost_price", "name", "category", "currency", "purchase_date", "target_entry", "target_profit", "target_stop"):
        if field in set_fields:
            updates.append(f"{field}=?")
            params.append(set_fields[field])
            
    if updates:
        params.append(pid)
        conn.execute(f"UPDATE positions SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
    vault = _get_vault()
    if vault and updates:
        _obsidian_write_position_snapshot(vault, conn, existing["symbol"])
        _obsidian_post_write_sync(vault, kinds=("positions",))
    conn.close()
    return {"ok": True}

class WatchCreate(BaseModel):
    symbol: str
    name: Optional[str] = ""
    category: Optional[str] = ""
    currency: str = "TWD"
    target_entry: Optional[float] = None
    target_add: Optional[float] = None
    target_profit: Optional[float] = None
    target_stop: Optional[float] = None
    notes: Optional[str] = ""

@app.post("/api/watchlist")
def api_add_watch(w: WatchCreate):
    conn = get_db()
    conn.execute(
        """INSERT INTO watchlist (symbol, name, category, currency, target_entry, target_add, target_profit, target_stop, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (w.symbol, w.name, w.category, w.currency, w.target_entry, w.target_add, w.target_profit, w.target_stop, w.notes)
    )
    conn.commit()
    vault = _get_vault()
    if vault:
        _obsidian_write_watchlist_snapshot(vault, conn, w.symbol)
        _obsidian_post_write_sync(vault, kinds=("watchlist",))
    conn.close()
    return {"ok": True}

class WatchUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    currency: Optional[str] = None
    target_entry: Optional[float] = None
    target_add: Optional[float] = None
    target_profit: Optional[float] = None
    target_stop: Optional[float] = None
    notes: Optional[str] = None


@app.put("/api/watchlist/{wid}")
def api_update_watch(wid: int, w: WatchUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM watchlist WHERE id=?", (wid,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Watchlist item not found")
    updates = []
    params = []
    set_fields = w.model_dump(exclude_unset=True)
    for field in ("name", "category", "currency", "target_entry", "target_add",
                  "target_profit", "target_stop", "notes"):
        if field in set_fields:
            updates.append(f"{field}=?")
            params.append(set_fields[field])
    if updates:
        params.append(wid)
        conn.execute(f"UPDATE watchlist SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
    vault = _get_vault()
    if vault and updates:
        _obsidian_write_watchlist_snapshot(vault, conn, existing["symbol"])
        _obsidian_post_write_sync(vault, kinds=("watchlist",))
    conn.close()
    return {"ok": True}


@app.delete("/api/watchlist/{wid}")
def api_del_watch(wid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM watchlist WHERE id=?", (wid,)).fetchone()
    conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    conn.commit()
    vault = _get_vault()
    if vault:
        if row:
            note = vault / "Watchlist" / f"{_safe_obsidian_name(row['symbol'])}.md"
            if note.exists():
                note.unlink()
        all_wl = [dict(r) for r in conn.execute("SELECT * FROM watchlist").fetchall()]
        _obsidian_write_watchlist_index(vault, all_wl)
        _obsidian_post_write_sync(vault, kinds=("watchlist",))
    conn.close()
    return {"ok": True}


# ─────────────── Trade Records CRUD ───────────────

def _estimate_trade_fees(action: str, shares: float, price: float, currency: str) -> tuple[float, float]:
    """Estimate brokerage fee and transaction tax for TW stocks.

    TW buy:  fee = shares * price * 0.001425  (capped at min 20 TWD)
    TW sell: fee = shares * price * 0.001425  + tax = shares * price * 0.003
    US / other: fee = 0 (user can enter manually)
    """
    if currency != "TWD":
        return 0.0, 0.0
    gross = shares * price
    fee = round(max(20.0, gross * 0.001425), 0)
    tax = round(gross * 0.003, 0) if action == "sell" else 0.0
    return fee, tax


def _compute_fifo_pnl(symbol: str, conn) -> dict:
    """FIFO realized P&L for a single symbol from all trades.

    Returns dict with keys:
      realized_pnl, realized_pnl_pct, total_bought, total_sold,
      remaining_shares, avg_cost, win_trades, loss_trades,
      total_trades, holding_days (avg of closed lots), best_pnl, worst_pnl
    """
    rows = conn.execute(
        "SELECT action, shares, price, fee, tax, trade_date FROM trades "
        "WHERE symbol=? ORDER BY trade_date ASC, id ASC",
        (symbol,),
    ).fetchall()

    buy_queue: list[dict] = []   # {"shares": float, "price": float, "date": str}
    realized_pnl = 0.0
    realized_cost = 0.0
    total_bought = 0.0
    total_sold = 0.0
    win, loss = 0, 0
    best_pnl: Optional[float] = None
    worst_pnl: Optional[float] = None
    holding_days_list: list[float] = []

    for r in rows:
        action, shares, price, fee, tax, trade_date = (
            r["action"], r["shares"], r["price"], r["fee"] or 0.0, r["tax"] or 0.0, r["trade_date"]
        )
        if action == "buy":
            buy_queue.append({"shares": shares, "price": price + (fee / shares if shares else 0), "date": trade_date})
            total_bought += shares
        elif action == "sell":
            total_sold += shares
            sell_gross = shares * price - fee - tax
            remaining_sell = shares
            sell_cost = 0.0
            while remaining_sell > 1e-9 and buy_queue:
                lot = buy_queue[0]
                use = min(lot["shares"], remaining_sell)
                sell_cost += use * lot["price"]
                # holding days for this lot
                try:
                    from datetime import date as _date
                    d1 = _date.fromisoformat(lot["date"])
                    d2 = _date.fromisoformat(trade_date)
                    holding_days_list.append((d2 - d1).days)
                except Exception:
                    pass
                lot["shares"] -= use
                remaining_sell -= use
                if lot["shares"] < 1e-9:
                    buy_queue.pop(0)
            pnl = sell_gross - sell_cost
            realized_pnl += pnl
            realized_cost += sell_cost
            if pnl >= 0:
                win += 1
            else:
                loss += 1
            if best_pnl is None or pnl > best_pnl:
                best_pnl = pnl
            if worst_pnl is None or pnl < worst_pnl:
                worst_pnl = pnl

    remaining_shares = sum(l["shares"] for l in buy_queue)
    avg_cost = (
        sum(l["shares"] * l["price"] for l in buy_queue) / remaining_shares
        if remaining_shares > 1e-9 else 0.0
    )
    total_trades = win + loss
    return {
        "realized_pnl": round(realized_pnl, 2),
        "realized_pnl_pct": round(realized_pnl / realized_cost * 100, 2) if realized_cost > 0 else 0.0,
        "realized_cost": round(realized_cost, 2),
        "total_bought": total_bought,
        "total_sold": total_sold,
        "remaining_shares": round(remaining_shares, 4),
        "avg_cost": round(avg_cost, 4),
        "win_trades": win,
        "loss_trades": loss,
        "total_closed_trades": total_trades,
        "win_rate": round(win / total_trades * 100, 1) if total_trades > 0 else None,
        "avg_holding_days": round(sum(holding_days_list) / len(holding_days_list), 1) if holding_days_list else None,
        "best_pnl": round(best_pnl, 2) if best_pnl is not None else None,
        "worst_pnl": round(worst_pnl, 2) if worst_pnl is not None else None,
    }


class TradeCreate(BaseModel):
    symbol: str
    name: Optional[str] = ""
    action: str          # "buy" | "sell"
    shares: float
    price: float
    fee: Optional[float] = None   # None = auto-estimate for TW stocks
    tax: Optional[float] = None   # None = auto-estimate for TW stocks
    trade_date: Optional[str] = None
    settle_date: Optional[str] = None
    currency: str = "TWD"
    notes: Optional[str] = ""
    auto_fee: bool = True          # if True and fee/tax are None, auto-estimate


class TradeUpdate(BaseModel):
    name: Optional[str] = None
    action: Optional[str] = None
    shares: Optional[float] = None
    price: Optional[float] = None
    fee: Optional[float] = None
    tax: Optional[float] = None
    trade_date: Optional[str] = None
    settle_date: Optional[str] = None
    currency: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/trades")
def api_get_trades(
    symbol: Optional[str] = None,
    action: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 500,
):
    conn = get_db()
    query = "SELECT * FROM trades WHERE 1=1"
    params: list = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    if action:
        query += " AND action = ?"
        params.append(action)
    if from_date:
        query += " AND trade_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND trade_date <= ?"
        params.append(to_date)
    query += " ORDER BY trade_date DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()
    return sanitize_float_values({"trades": rows, "count": len(rows)})


@app.post("/api/trades")
def api_add_trade(t: TradeCreate):
    if t.action not in ("buy", "sell"):
        raise HTTPException(400, "action must be 'buy' or 'sell'")
    if t.shares <= 0:
        raise HTTPException(400, "shares must be positive")
    if t.price <= 0:
        raise HTTPException(400, "price must be positive")

    trade_date = _normalize_purchase_date(t.trade_date) or date.today().isoformat()
    symbol = t.symbol.strip().upper()

    fee = t.fee
    tax = t.tax
    if t.auto_fee and fee is None and tax is None:
        fee, tax = _estimate_trade_fees(t.action, t.shares, t.price, t.currency)
    fee = fee or 0.0
    tax = tax or 0.0

    created_at = datetime.utcnow().isoformat() + "Z"
    conn = get_db()

    # 以持股頁面中的成本價格為準：若賣出時交易紀錄無足夠的買入額度，自動以持倉成本補登買入明細
    if t.action == "sell":
        try:
            buy_shares_res = conn.execute(
                "SELECT SUM(shares) FROM trades WHERE symbol=? AND action='buy'", (symbol,)
            ).fetchone()
            sell_shares_res = conn.execute(
                "SELECT SUM(shares) FROM trades WHERE symbol=? AND action='sell'", (symbol,)
            ).fetchone()
            total_buy = buy_shares_res[0] or 0.0
            total_sell = sell_shares_res[0] or 0.0
            new_total_sell = total_sell + t.shares

            if total_buy < new_total_sell:
                deficit = new_total_sell - total_buy
                pos = conn.execute(
                    "SELECT cost_price, purchase_date, name, currency FROM positions WHERE symbol=?", (symbol,)
                ).fetchone()
                if pos:
                    pos_cost = pos["cost_price"]
                    pos_date = pos["purchase_date"] or date.today().isoformat()
                    pos_name = pos["name"] or t.name or ""
                    pos_curr = pos["currency"] or t.currency
                    auto_fee, _ = _estimate_trade_fees("buy", deficit, pos_cost, pos_curr)

                    conn.execute(
                        """INSERT INTO trades
                           (symbol, name, action, shares, price, fee, tax, trade_date, settle_date, currency, notes, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (symbol, pos_name, "buy", deficit, pos_cost, auto_fee, 0.0,
                         pos_date, None, pos_curr, "自動補登初始持倉成本", created_at)
                    )
                    conn.commit()
        except Exception as ex:
            print(f"  [WARN] Failed to auto-backfill buy trade: {ex}")

    conn.execute(
        """INSERT INTO trades
           (symbol, name, action, shares, price, fee, tax, trade_date, settle_date, currency, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, t.name or "", t.action, t.shares, t.price, fee, tax,
         trade_date, t.settle_date, t.currency, t.notes or "", created_at),
    )
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"ok": True, "id": tid, "fee": fee, "tax": tax}


@app.put("/api/trades/{tid}")
def api_update_trade(tid: int, t: TradeUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Trade not found")
    set_fields = t.model_dump(exclude_unset=True)
    if "trade_date" in set_fields:
        set_fields["trade_date"] = _normalize_purchase_date(set_fields["trade_date"]) or existing["trade_date"]
    updates = [f"{k}=?" for k in set_fields]
    params = list(set_fields.values()) + [tid]
    if updates:
        conn.execute(f"UPDATE trades SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/trades/{tid}")
def api_del_trade(tid: int):
    conn = get_db()
    conn.execute("DELETE FROM trades WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/trades/summary")
def api_trades_summary():
    """已實現損益彙總（全部股票，FIFO）"""
    conn = get_db()
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM trades ORDER BY symbol"
    ).fetchall()]
    result = []
    total_realized = 0.0
    for sym in symbols:
        fifo = _compute_fifo_pnl(sym, conn)
        # grab display name
        name_row = conn.execute(
            "SELECT name FROM trades WHERE symbol=? AND name != '' ORDER BY id DESC LIMIT 1", (sym,)
        ).fetchone()
        name = name_row[0] if name_row else sym
        # currency — assume homogeneous per symbol
        cur_row = conn.execute("SELECT currency FROM trades WHERE symbol=? LIMIT 1", (sym,)).fetchone()
        currency = cur_row[0] if cur_row else "TWD"
        row_data = {"symbol": sym, "name": name, "currency": currency, **fifo}
        result.append(row_data)
        total_realized += fifo["realized_pnl"]
    conn.close()
    win_symbols = [r for r in result if r["realized_pnl"] > 0]
    loss_symbols = [r for r in result if r["realized_pnl"] < 0]
    return sanitize_float_values({
        "by_symbol": result,
        "total_realized_pnl": round(total_realized, 2),
        "win_symbols": len(win_symbols),
        "loss_symbols": len(loss_symbols),
        "symbol_count": len(result),
    })


@app.get("/api/trades/{symbol}/timeline")
def api_trades_timeline(symbol: str):
    """單一股票交易時間線 + FIFO 損益"""
    symbol = symbol.upper()
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE symbol=? ORDER BY trade_date ASC, id ASC", (symbol,)
    ).fetchall()]
    fifo = _compute_fifo_pnl(symbol, conn)
    conn.close()
    return sanitize_float_values({"symbol": symbol, "trades": rows, "fifo_summary": fifo})


# ─────────────── 警報歷史篩選 ───────────────

@app.get("/api/alerts/search")
def api_alerts_search(
    symbol: Optional[str] = None,
    level: Optional[str] = None,
    type: Optional[str] = None,
    days: int = 7,
    limit: int = 200,
):
    conn = get_db()
    query = "SELECT * FROM alerts WHERE date(ts) >= date('now', ?) "
    params = [f"-{days} days"]
    if symbol:
        query += " AND symbol LIKE ?"
        params.append(f"%{symbol}%")
    if level:
        query += " AND level = ?"
        params.append(level)
    if type:
        query += " AND type = ?"
        params.append(type)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {
        "alerts": [_enrich_alert(dict(r)) for r in rows],
        "type_meta": ALERT_TYPE_META,
    }

@app.delete("/api/alerts/clear")
def api_clear_alerts(days: Optional[int] = None, symbol: Optional[str] = None, market: Optional[str] = None):
    """清除警報。可依 symbol、market、days 篩選；都未提供則全清。"""
    conn = get_db()
    to_delete = []
    if symbol:
        to_delete = [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE symbol=?", (symbol,)).fetchall()]
    elif market == "tw":
        to_delete = [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE symbol LIKE '%.TW'").fetchall()]
    elif market == "us":
        to_delete = [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE symbol NOT LIKE '%.TW'").fetchall()]
    elif days is None:
        to_delete = [dict(r) for r in conn.execute("SELECT * FROM alerts").fetchall()]
    else:
        to_delete = [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE date(ts) < date('now', ?)", (f"-{days} days",)).fetchall()]
    if symbol:
        conn.execute("DELETE FROM alerts WHERE symbol=?", (symbol,))
    elif market == "tw":
        conn.execute("DELETE FROM alerts WHERE symbol LIKE '%.TW'")
    elif market == "us":
        conn.execute("DELETE FROM alerts WHERE symbol NOT LIKE '%.TW'")
    elif days is None:
        conn.execute("DELETE FROM alerts")
    else:
        conn.execute("DELETE FROM alerts WHERE date(ts) < date('now', ?)", (f"-{days} days",))
    conn.commit()
    vault = _get_vault()
    if vault:
        for alert in to_delete:
            _obsidian_delete_alert(vault, alert)
        if to_delete:
            _obsidian_post_write_sync(vault, kinds=("alerts",))
    conn.close()
    return {"ok": True}

# ─────────────── 新增的批次點位分析與調整 API ───────────────
from typing import List

class BatchUpdateItem(BaseModel):
    symbol: str
    entry: Optional[float] = None
    profit: Optional[float] = None
    stop: Optional[float] = None

class BatchUpdateRequests(BaseModel):
    items: List[BatchUpdateItem]

@app.delete("/api/alerts/{aid}")
def api_delete_alert(aid: int):
    """刪除單筆警報。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    conn.execute("DELETE FROM alerts WHERE id=?", (aid,))
    conn.commit()
    vault = _get_vault()
    if vault and row:
        _obsidian_delete_alert(vault, dict(row))
        _obsidian_post_write_sync(vault, kinds=("alerts",))
    conn.close()
    return {"ok": True}

@app.get("/api/batch-suggest-levels")
def api_batch_suggest_levels():
    """針對所有列出的個股（持倉與觀察清單）進行 AI 批量點位分析與建議。"""
    def event_generator():
        try:
            yield "data: " + json.dumps({"status": "progress", "percent": 10, "message": "正在讀取持股與觀察清單資料..."}) + "\n\n"
            conn = get_db()
            watchlist_rows = conn.execute("SELECT symbol, name, target_entry, target_stop, target_profit FROM watchlist").fetchall()
            positions_rows = conn.execute("SELECT symbol, name, target_entry, target_stop, target_profit FROM positions").fetchall()
            market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
            conn.close()

            # 合併所有獨特的個股代號
            symbols_map = {}
            for r in watchlist_rows:
                row_dict = dict(r)
                symbols_map[row_dict["symbol"]] = {
                    "name": row_dict["name"],
                    "target_entry": row_dict["target_entry"],
                    "target_stop": row_dict["target_stop"],
                    "target_profit": row_dict["target_profit"]
                }
            for r in positions_rows:
                row_dict = dict(r)
                if row_dict["symbol"] not in symbols_map:
                    symbols_map[row_dict["symbol"]] = {
                        "name": row_dict["name"],
                        "target_entry": row_dict["target_entry"],
                        "target_stop": row_dict["target_stop"],
                        "target_profit": row_dict["target_profit"]
                    }

            if not symbols_map:
                yield "data: " + json.dumps({"status": "done", "percent": 100, "message": "分析完成", "suggestions": {}}) + "\n\n"
                return

            yield "data: " + json.dumps({"status": "progress", "percent": 30, "message": f"已載入 {len(symbols_map)} 檔個股，正在讀取技術指標快取..."}) + "\n\n"

            # 批次查詢最新價格快取
            conn = get_db()
            placeholders = ",".join("?" * len(symbols_map))
            cache_rows = conn.execute(
                f"SELECT symbol, price, rsi, change_1d, change_1m, beta, ma20, ma60, high52, low52, data FROM price_cache WHERE symbol IN ({placeholders})",
                list(symbols_map.keys())
            ).fetchall()
            conn.close()

            cache_map = {r["symbol"]: dict(r) for r in cache_rows}

            # 構築大盤與個股的上下文數據
            context_lines = []
            if market_row:
                m = dict(market_row)
                context_lines.append(f"大盤指標: VIX {m.get('vix')}, 風險等級 {m.get('risk_level')}, 警訊數 {m.get('warnings_count')}/3")

            for symbol, info in symbols_map.items():
                cache = cache_map.get(symbol)
                if not cache:
                    continue
                ind = json.loads(cache.get("data") or "{}")
                line = [
                    f"標的: {symbol} ({info['name']})",
                    f"現價: {cache.get('price') or ind.get('price')}, RSI: {cache.get('rsi') or ind.get('rsi')}, β: {cache.get('beta') or ind.get('beta')}",
                    f"MA20: {cache.get('ma20') or ind.get('ma20')}, MA60: {cache.get('ma60') or ind.get('ma60')}",
                    f"52週高/低: {cache.get('high52') or ind.get('high52')} / {cache.get('low52') or ind.get('low52')}",
                    f"今日漲跌: {cache.get('change_1d') or ind.get('change_1d')}%, 1月漲跌: {cache.get('change_1m') or ind.get('change_1m')}%"
                ]
                if info['target_entry'] or info['target_profit'] or info['target_stop']:
                    line.append(f"目前設定 -> 進場: {info.get('target_entry') or '未設定'}, 停利: {info.get('target_profit') or '未設定'}, 停損: {info.get('target_stop') or '未設定'}")
                context_lines.append("\n".join(line))
                context_lines.append("---")

            context_str = "\n".join(context_lines)

            settings = load_settings()
            keys = settings["api_keys"]
            roles = settings["roles"]
            analyst = roles["analyst"]

            yield "data: " + json.dumps({"status": "progress", "percent": 50, "message": f"正在使用 {analyst['provider']} ({analyst['model']}) 進行 AI 智能點位分析與建議規劃..."}) + "\n\n"

            prompt = f"""你是專業金融分析師與資深投資經理。請針對以下所有股票的最新數據與指標，分析並給出適合的推薦點位：
1. 進場價位 (entry)
2. 停損價位 (stop)
3. 停利價位 (profit)
並且給出 1-2 句話的操作策略理由 (reason)。

[股票即時數據與指標]
{context_str}

請務必嚴格以下列 JSON 格式回傳，不要有任何其他 markdown 標記或包裹字元（例如 ```json 或 ``` 都不需要，直接輸出純 JSON 字符串）：
{{
  "2330.TW": {{
    "entry": 800.0,
    "profit": 920.0,
    "stop": 760.0,
    "reason": "多頭趨勢，RSI 中性，建議拉回至 MA20 附近分批進場，跌破 760 停損。"
  }}
}}
如果某些數值不適用或無法建議，請給予合適的預估數字。
"""
            raw_output = call_llm(
                analyst["provider"], analyst["model"],
                prompt,
                keys.get(analyst["provider"], ""),
                mode=analyst.get("mode", "api"),
            )

            yield "data: " + json.dumps({"status": "progress", "percent": 90, "message": "正在解析 AI 建議並整理輸出結果..."}) + "\n\n"

            # 解析 JSON
            clean_json = raw_output.strip()
            if clean_json.startswith("```"):
                lines = clean_json.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].strip() == "```":
                    lines = lines[:-1]
                clean_json = "\n".join(lines).strip()

            suggestions = json.loads(clean_json)

            # 整合原始名稱、現價與建議數據
            response_data = {}
            for symbol, info in symbols_map.items():
                cache = cache_map.get(symbol)
                current_price = cache.get("price") if cache else None

                sug = suggestions.get(symbol, {})
                response_data[symbol] = {
                    "name": info["name"],
                    "current_price": current_price,
                    "entry": sug.get("entry") or info["target_entry"],
                    "profit": sug.get("profit") or info["target_profit"],
                    "stop": sug.get("stop") or info["target_stop"],
                    "reason": sug.get("reason") or "暫無建議"
                }

            yield "data: " + json.dumps({"status": "done", "percent": 100, "message": "分析完成", "suggestions": response_data}) + "\n\n"

        except Exception as e:
            yield "data: " + json.dumps({"status": "error", "error": f"LLM 批量分析失敗: {e}"}) + "\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/batch-update-levels")
def api_batch_update_levels(req: BatchUpdateRequests):
    """批量套用微調後的進場、停損與停利點位。"""
    conn = get_db()
    updated_watch_symbols = set()
    updated_position_symbols = set()
    for item in req.items:
        # 更新觀察清單中的點位
        watch_row = conn.execute("SELECT id FROM watchlist WHERE symbol=?", (item.symbol,)).fetchone()
        if watch_row:
            updates = []
            params = []
            if item.entry is not None:
                updates.append("target_entry=?")
                params.append(item.entry)
            if item.profit is not None:
                updates.append("target_profit=?")
                params.append(item.profit)
            if item.stop is not None:
                updates.append("target_stop=?")
                params.append(item.stop)
            if updates:
                params.append(watch_row["id"])
                conn.execute(f"UPDATE watchlist SET {', '.join(updates)} WHERE id=?", params)
                updated_watch_symbols.add(item.symbol)

        # 更新持倉中的點位
        pos_row = conn.execute("SELECT id FROM positions WHERE symbol=?", (item.symbol,)).fetchone()
        if pos_row:
            updates = []
            params = []
            if item.entry is not None:
                updates.append("target_entry=?")
                params.append(item.entry)
            if item.profit is not None:
                updates.append("target_profit=?")
                params.append(item.profit)
            if item.stop is not None:
                updates.append("target_stop=?")
                params.append(item.stop)
            if updates:
                params.append(pos_row["id"])
                conn.execute(f"UPDATE positions SET {', '.join(updates)} WHERE id=?", params)
                updated_position_symbols.add(item.symbol)
                
    conn.commit()
    vault = _get_vault()
    if vault:
        for symbol in sorted(updated_watch_symbols):
            _obsidian_write_watchlist_snapshot(vault, conn, symbol)
        for symbol in sorted(updated_position_symbols):
            _obsidian_write_position_snapshot(vault, conn, symbol)
        if updated_watch_symbols or updated_position_symbols:
            _obsidian_post_write_sync(
                vault,
                kinds=tuple(
                    kind
                    for kind, enabled in (
                        ("watchlist", bool(updated_watch_symbols)),
                        ("positions", bool(updated_position_symbols)),
                    )
                    if enabled
                ),
            )
    conn.close()
    return {"ok": True}

# ─────────────── 設定 API (LLM Keys + Roles) ───────────────
@app.get("/api/settings")
def api_get_settings():
    """回傳設定，API key 用 mask 格式。"""
    from llm_providers import BROKERAGE_PRESETS
    s = load_settings()
    return {
        "api_keys_masked": {k: mask_key(v) for k, v in s["api_keys"].items()},
        "api_keys_set": {k: bool(v) for k, v in s["api_keys"].items()},
        "roles": s["roles"],
        "available_models": AVAILABLE_MODELS,
        "cli_status": detect_cli_availability(),
        "obsidian_vault_path": s.get("obsidian_vault_path", ""),
        "brokerage_fees": s.get("brokerage_fees", {}),
        "brokerage_presets": BROKERAGE_PRESETS,
    }

class SettingsUpdate(BaseModel):
    api_keys: Optional[dict] = None           # {anthropic, openai, google} - 空字串 = 不更新
    roles: Optional[dict] = None              # {analyst: {provider, model}, reviewer: {...}}
    obsidian_vault_path: Optional[str] = None # Obsidian vault 根目錄，None = 不更新
    brokerage_fees: Optional[dict] = None     # 手續費覆寫 - 可帶 tw_broker / us_broker 切 preset
    tw_broker_preset: Optional[str] = None    # 一鍵套用 preset
    us_broker_preset: Optional[str] = None


@app.post("/api/settings")
def api_save_settings(req: SettingsUpdate):
    from llm_providers import BROKERAGE_PRESETS
    s = load_settings()
    if req.api_keys:
        for provider, key in req.api_keys.items():
            if key and provider in s["api_keys"]:
                # 只更新非空 key（避免 mask 顯示覆蓋實際 key）
                if not key.startswith("***") and "..." not in key:
                    s["api_keys"][provider] = key
    if req.roles:
        for role_name, cfg in req.roles.items():
            if role_name in s["roles"]:
                s["roles"][role_name].update(cfg)
    if req.obsidian_vault_path is not None:
        s["obsidian_vault_path"] = req.obsidian_vault_path
    fees = dict(s.get("brokerage_fees") or {})
    if req.tw_broker_preset and req.tw_broker_preset in BROKERAGE_PRESETS:
        fees.update({k: v for k, v in BROKERAGE_PRESETS[req.tw_broker_preset].items() if k != "label"})
        fees["tw_broker"] = req.tw_broker_preset
    if req.us_broker_preset and req.us_broker_preset in BROKERAGE_PRESETS:
        fees.update({k: v for k, v in BROKERAGE_PRESETS[req.us_broker_preset].items() if k != "label"})
        fees["us_broker"] = req.us_broker_preset
    if req.brokerage_fees:
        fees.update({k: v for k, v in req.brokerage_fees.items() if v is not None})
    s["brokerage_fees"] = fees
    save_settings(s)
    return {"ok": True}

# ─────────────── LLM 深度分析 (多 Provider + Workflow) ───────────────
def _fmt_pct(v, digits=1):
    """yfinance ratios come as fractions (0.166 = 16.6%)."""
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def _fmt_num(v, digits=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _build_fundamentals_text(symbol: str, price: Optional[float], fundamentals: Optional[dict] = None) -> str:
    """Human-readable fundamentals block for the LLM context."""
    f = fundamentals if fundamentals is not None else fetch_fundamentals(symbol)
    if not f:
        return "基本面數據：暫時無法取得（yfinance 無回應或標的無基本面）。"

    # ETF / 基金：改用基金等價資訊（無損益表）
    if f.get("is_fund") and f.get("etf"):
        e = f["etf"]
        lines = ["【ETF / 基金概況】"]
        if e.get("category") or e.get("fund_family"):
            lines.append(f"類別：{e.get('category') or '—'}，發行商：{e.get('fund_family') or '—'}")
        ta = e.get("total_assets")
        ta_txt = f"{ta/1e8:.0f}億" if ta else "—"
        lines.append(
            f"規模：{ta_txt}，NAV {_fmt_num(e.get('nav'))}，"
            f"配息率 {_fmt_pct(e.get('yield'))}，3 年 β {_fmt_num(e.get('beta_3y'))}"
        )
        lines.append(
            f"報酬：YTD {_fmt_num(e.get('ytd_return'))}%，"
            f"3 年平均 {_fmt_pct(e.get('three_year_return'))}，5 年平均 {_fmt_pct(e.get('five_year_return'))}"
        )
        if price and e.get("nav"):
            prem = (price / e["nav"] - 1) * 100
            lines.append(f"折溢價：市價相對 NAV {prem:+.2f}%")
        holdings = e.get("top_holdings") or []
        if holdings:
            hs = "，".join(f"{h['symbol']} {h['weight']}%" for h in holdings[:5])
            lines.append(f"前 5 大持股：{hs}")
        return "\n".join(lines)

    if all(v is None for k, v in f.items() if k not in ("sector", "industry", "quote_type", "is_fund")):
        return "基本面數據：暫時無法取得（yfinance 無回應或標的無基本面）。"
    lines = ["【基本面與估值】"]
    if f.get("sector") or f.get("industry"):
        lines.append(f"產業：{f.get('sector') or '—'} / {f.get('industry') or '—'}")
    # 估值
    lines.append(
        f"估值：本益比 TTM {_fmt_num(f.get('trailing_pe'))} / 預估 {_fmt_num(f.get('forward_pe'))}，"
        f"股價淨值比 {_fmt_num(f.get('price_to_book'))}，PEG {_fmt_num(f.get('peg_ratio'))}，"
        f"EPS TTM {_fmt_num(f.get('trailing_eps'))} / 預估 {_fmt_num(f.get('forward_eps'))}"
    )
    # 成長 + 獲利
    lines.append(
        f"成長：營收年增 {_fmt_pct(f.get('revenue_growth'))}，盈餘年增 {_fmt_pct(f.get('earnings_growth'))}"
    )
    lines.append(
        f"獲利能力：毛利率 {_fmt_pct(f.get('gross_margins'))}，營業利益率 {_fmt_pct(f.get('operating_margins'))}，"
        f"淨利率 {_fmt_pct(f.get('profit_margins'))}，ROE {_fmt_pct(f.get('return_on_equity'))}"
    )
    # 注意：yfinance 此版的 dividendYield 已是百分比（0.34 = 0.34%），不像
    # 其他 ratio 是分數，故直接顯示不再 ×100。
    dy = f.get("dividend_yield")
    dy_text = f"{_fmt_num(dy)}%" if dy is not None else "—"
    lines.append(
        f"財務體質：負債權益比 {_fmt_num(f.get('debt_to_equity'))}，殖利率 {dy_text}"
    )
    # 賣方共識 + 與現價相對位置
    tgt = f.get("target_mean_price")
    if tgt and price:
        upside = (tgt / price - 1) * 100
        lines.append(
            f"賣方共識：目標均價 {_fmt_num(tgt)}（區間 {_fmt_num(f.get('target_low_price'))}~{_fmt_num(f.get('target_high_price'))}），"
            f"相對現價 {upside:+.1f}%，評等 {f.get('recommendation_key') or '—'}（{f.get('num_analysts') or 0} 位分析師）"
        )
    elif f.get("recommendation_key"):
        lines.append(f"賣方共識：評等 {f.get('recommendation_key')}（{f.get('num_analysts') or 0} 位分析師）")
    return "\n".join(lines)


def _build_technical_matrix_text(symbol: str, matrix: Optional[dict] = None) -> str:
    """Condense the 17D technical matrix into an LLM-readable digest."""
    if matrix is None:
        try:
            matrix = _build_technical_matrix_payload(symbol, "6mo")
        except Exception:
            return "17D 技術矩陣：暫時無法計算（資料不足或抓取失敗）。"
    if not matrix or matrix.get("error"):
        return "17D 技術矩陣：暫時無法計算（資料不足或抓取失敗）。"
    summary = matrix.get("summary") or {}
    plan = matrix.get("execution_plan") or {}
    confluence = matrix.get("confluence_zones") or []
    dims = matrix.get("dimensions") or []
    interactions = matrix.get("interactions") or []

    lines = ["【17D 全景技術矩陣】"]
    lines.append(
        f"整體偏向：{summary.get('bias')}（淨分數 {summary.get('net_score')}，信心 {summary.get('confidence')}），"
        f"風險等級 {summary.get('risk_level')}；維度狀態 computed {summary.get('computed_count')}/"
        f"partial {summary.get('partial_count')}/unavailable {summary.get('unavailable_count')}"
    )
    # 各維度偏向（只列 computed 且非中性者，避免雜訊）
    notable = []
    for d in dims:
        if d.get("status") == "unavailable":
            continue
        bias = d.get("bias")
        if bias and bias != "neutral":
            notable.append(f"{d.get('name', d.get('id'))}={bias}({d.get('score')})")
    if notable:
        lines.append("關鍵維度偏向：" + "；".join(notable[:10]))
    # 交互關聯
    inter_active = [f"{i.get('name')}={i.get('status')}" for i in interactions if i.get("status") not in ("inactive", "complete", "neutral")]
    if inter_active:
        lines.append("交互關聯訊號：" + "；".join(inter_active))
    # 共振區
    if confluence:
        czs = [f"{c.get('center')}（score {c.get('score')}）" for c in confluence[:4]]
        lines.append("價格共振區（多工具重疊，高機率支撐/壓力）：" + "；".join(czs))
    # 執行計畫候選價位
    def _levels(items):
        return "；".join(f"{it.get('type')} {it.get('price')}" for it in (items or [])[:3]) or "—"
    lines.append(f"系統建議進場區：{_levels(plan.get('entries'))}")
    lines.append(f"系統建議停損區：{_levels(plan.get('stops'))}")
    lines.append(f"系統建議停利區：{_levels(plan.get('targets'))}")
    if plan.get("risk_notes"):
        lines.append("風險備註：" + "；".join(plan["risk_notes"][:3]))
    # 資料缺口透明化
    gaps = matrix.get("data_gaps") or []
    if gaps:
        lines.append(f"（資料缺口 {len(gaps)} 項，部分機構級維度未接外部 feed，判讀時請降權）")
    return "\n".join(lines)


# ─────────────── 財報 / 17D 落地（SQL + Obsidian） ───────────────

def _store_fundamentals_snapshot(symbol: str, data: dict, conn=None) -> None:
    """Persist today's fundamentals to SQL (idempotent per symbol+date)."""
    if not data:
        return
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO fundamentals_snapshots (symbol, date, data) VALUES (?, ?, ?)",
            (symbol, date.today().isoformat(), json.dumps(sanitize_float_values(data), ensure_ascii=False)),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _store_technical_matrix_snapshot(symbol: str, matrix: dict, period: str = "6mo", conn=None,
                                     snapshot_date: Optional[str] = None, store_full: bool = True) -> None:
    """Persist a 17D matrix summary + full payload to SQL (idempotent per day).

    snapshot_date: ISO date string; defaults to today. Used by historical
    backfill to stamp past trading days.
    store_full: when False, store an empty data blob (saves space for the many
    backfilled rows — only the summary fields are kept for the bias curve).
    """
    if not matrix or matrix.get("error"):
        return
    summary = matrix.get("summary") or {}
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO technical_matrix_snapshots
               (symbol, date, period, bias, net_score, confidence, risk_level, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, snapshot_date or date.today().isoformat(), period,
             summary.get("bias"), summary.get("net_score"), summary.get("confidence"),
             summary.get("risk_level"),
             json.dumps(sanitize_float_values(matrix), ensure_ascii=False) if store_full else "{}"),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _backfill_technical_matrix_history(symbol: str, lookback_days: int = 180,
                                       step_days: int = 5, conn=None) -> dict:
    """Reconstruct past 17D matrix bias by slicing historical OHLCV.

    For each sampled past trading day we slice the daily history up to that
    date and recompute the matrix, then store its summary. Intraday / options /
    breadth are point-in-time only and intentionally omitted from backfill —
    the core OHLCV-driven dimensions still produce the bias curve.

    Returns {filled, skipped, errors}. Idempotent — already-stored dates skip.
    """
    own = conn is None
    if own:
        conn = get_db()
    filled = skipped = errors = 0
    try:
        existing = {
            row["date"] for row in conn.execute(
                "SELECT date FROM technical_matrix_snapshots WHERE symbol=? AND period='6mo'",
                (symbol,),
            ).fetchall()
        }
        # Fetch enough daily history to cover lookback + MA warm-up
        h, _src = fetch_history(symbol, period="2y")
        if h is None or len(h) < 60:
            return {"filled": 0, "skipped": 0, "errors": 1, "reason": "insufficient history"}
        h = h.sort_index()
        benchmark_symbol = "^TWII" if _twse_channel(symbol) else "^GSPC"
        bench = fetch_benchmark_close(benchmark_symbol)

        cutoff_start = date.today() - timedelta(days=lookback_days)
        # Iterate dates present in the index, sampling every step_days
        unique_dates = sorted({ts.date() for ts in h.index})
        sampled = [d for i, d in enumerate(unique_dates) if d >= cutoff_start]
        # subsample by step
        sampled = sampled[::step_days] if step_days > 1 else sampled
        # always include the most recent date
        if unique_dates and unique_dates[-1] not in sampled:
            sampled.append(unique_dates[-1])

        for d in sampled:
            iso = d.isoformat()
            if iso in existing:
                skipped += 1
                continue
            try:
                cutoff_ts = pd.Timestamp(d) + pd.Timedelta(days=1)
                sliced = h[h.index < cutoff_ts]
                if len(sliced) < 60:
                    continue
                bench_sliced = bench[bench.index < cutoff_ts] if bench is not None and len(bench) else bench
                matrix = build_technical_matrix(
                    symbol, sliced,
                    benchmark_close=bench_sliced,
                    source="backfill",
                )
                _store_technical_matrix_snapshot(symbol, matrix, "6mo", conn,
                                                 snapshot_date=iso, store_full=False)
                filled += 1
            except Exception:
                errors += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"filled": filled, "skipped": skipped, "errors": errors}


def _load_matrix_bias_history(symbol: str, limit: int = 7) -> list[dict]:
    """Recent daily 17D bias/score history for temporal context in analysis."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT date, bias, net_score, confidence, risk_level
               FROM technical_matrix_snapshots
               WHERE symbol=? ORDER BY date DESC LIMIT ?""",
            (symbol, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows][::-1]  # chronological


def _obsidian_write_fundamentals(vault: Path, symbol: str, data: dict) -> None:
    """Write/update a per-symbol fundamentals note in Obsidian."""
    if not data:
        return
    safe = _safe_obsidian_name(symbol)
    fdir = vault / "Fundamentals"
    fdir.mkdir(parents=True, exist_ok=True)
    note = fdir / f"{safe}.md"
    today = date.today().isoformat()

    def g(k):
        v = data.get(k)
        return "" if v is None else v

    content = f"""---
type: fundamentals
symbol: {symbol}
sector: {_fmt(data.get('sector'))}
industry: {_fmt(data.get('industry'))}
trailing_pe: {_fmt(data.get('trailing_pe'))}
forward_pe: {_fmt(data.get('forward_pe'))}
peg_ratio: {_fmt(data.get('peg_ratio'))}
revenue_growth: {_fmt(data.get('revenue_growth'))}
earnings_growth: {_fmt(data.get('earnings_growth'))}
roe: {_fmt(data.get('return_on_equity'))}
target_mean_price: {_fmt(data.get('target_mean_price'))}
recommendation: {_fmt(data.get('recommendation_key'))}
updated: {today}
---

# {symbol} 基本面快照

> 更新：{today}

| 指標 | 數值 |
|------|------|
| 產業 | {g('sector')} / {g('industry')} |
| 本益比 TTM / 預估 | {g('trailing_pe')} / {g('forward_pe')} |
| 股價淨值比 | {g('price_to_book')} |
| PEG | {g('peg_ratio')} |
| EPS TTM / 預估 | {g('trailing_eps')} / {g('forward_eps')} |
| 營收年增 | {g('revenue_growth')} |
| 盈餘年增 | {g('earnings_growth')} |
| 毛利率 / 淨利率 | {g('gross_margins')} / {g('profit_margins')} |
| ROE | {g('return_on_equity')} |
| 負債權益比 | {g('debt_to_equity')} |
| 殖利率 | {g('dividend_yield')} |
| 賣方目標均價 | {g('target_mean_price')} |
| 評等 | {g('recommendation_key')} ({g('num_analysts')} 位分析師) |

## 連結
[[Portfolio/Positions/{safe}|{symbol} 持倉]]
[[TechnicalAnalysis/Symbols/{safe}/技術矩陣入口|{symbol} 17D 技術矩陣]]
"""
    note.write_text(content, encoding="utf-8")


def _store_financial_reports(symbol: str, records: list[dict], conn=None) -> int:
    """Persist quarterly financial reports to SQL. A reported quarter is final,
    so (symbol, period) rows are written once and never change."""
    if not records:
        return 0
    own = conn is None
    if own:
        conn = get_db()
    n = 0
    try:
        for rec in records:
            period = rec.get("period")
            if not period:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO financial_reports (symbol, period, period_type, data)
                   VALUES (?, ?, 'quarter', ?)""",
                (symbol, period, json.dumps(sanitize_float_values(rec), ensure_ascii=False)),
            )
            n += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return n


def _load_financial_reports(symbol: str, limit: int = 8) -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT data FROM financial_reports WHERE symbol=? ORDER BY period DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data"]))
        except (TypeError, ValueError):
            pass
    return out


def _build_financials_history_text(symbol: str, records: Optional[list] = None) -> str:
    """LLM-readable historical financial-report trend block."""
    recs = records if records is not None else _load_financial_reports(symbol, limit=6)
    if not recs:
        return ""  # ETF / no income statement — omit silently
    lines = ["【歷史財報趨勢（近幾季）】"]
    for r in recs[:6]:
        rev = r.get("revenue")
        rev_txt = f"{rev/1e8:.1f}億" if rev else "—"
        parts = [f"{r.get('period')}：營收 {rev_txt}"]
        if r.get("revenue_yoy") is not None:
            parts.append(f"YoY {r['revenue_yoy']:+.1f}%")
        if r.get("eps") is not None:
            parts.append(f"EPS {r['eps']}")
        if r.get("eps_yoy") is not None:
            parts.append(f"EPS YoY {r['eps_yoy']:+.1f}%")
        if r.get("gross_margin") is not None:
            parts.append(f"毛利率 {r['gross_margin']}%")
        if r.get("net_margin") is not None:
            parts.append(f"淨利率 {r['net_margin']}%")
        lines.append("・" + "，".join(parts))
    return "\n".join(lines)


def _obsidian_write_financials(vault: Path, symbol: str, records: list[dict]) -> None:
    """Write/update a per-symbol historical-financials note in Obsidian."""
    if not records:
        return
    safe = _safe_obsidian_name(symbol)
    fdir = vault / "Fundamentals"
    fdir.mkdir(parents=True, exist_ok=True)
    note = fdir / f"{safe}_財報歷史.md"
    rows_md = []
    for r in records:
        rev = r.get("revenue")
        rev_txt = f"{rev/1e8:.1f}" if rev else "—"
        rows_md.append(
            f"| {r.get('period')} | {rev_txt} | {_fmt(r.get('revenue_yoy'))} | {_fmt(r.get('eps'))} | "
            f"{_fmt(r.get('eps_yoy'))} | {_fmt(r.get('gross_margin'))} | {_fmt(r.get('net_margin'))} |"
        )
    content = f"""---
type: financial-history
symbol: {symbol}
updated: {date.today().isoformat()}
quarters: {len(records)}
---

# {symbol} 歷史財報（季度）

| 期別 | 營收(億) | 營收YoY% | EPS | EPS YoY% | 毛利率% | 淨利率% |
|------|---------:|---------:|----:|---------:|--------:|--------:|
{chr(10).join(rows_md)}

## 連結
[[Fundamentals/{safe}|{symbol} 基本面快照]]
[[Portfolio/Positions/{safe}|{symbol} 持倉]]
"""
    note.write_text(content, encoding="utf-8")


def _persist_symbol_research(symbol: str, fundamentals: dict, matrix: Optional[dict],
                            financials: Optional[list] = None) -> None:
    """One-shot: persist fundamentals + 17D + 財報歷史 to SQL and (if configured) Obsidian."""
    try:
        conn = get_db()
        _store_fundamentals_snapshot(symbol, fundamentals, conn)
        if matrix and not matrix.get("error"):
            _store_technical_matrix_snapshot(symbol, matrix, "6mo", conn)
        if financials:
            _store_financial_reports(symbol, financials, conn)
        conn.close()
    except Exception as e:
        print(f"  [WARN] persist {symbol} to SQL failed: {e}")
    vault = _get_vault()
    if not vault:
        return
    try:
        _obsidian_write_fundamentals(vault, symbol, fundamentals)
        if financials:
            _obsidian_write_financials(vault, symbol, financials)
        if matrix and not matrix.get("error"):
            _obsidian_write_technical_matrix(vault, matrix)
    except Exception as e:
        print(f"  [WARN] persist {symbol} to Obsidian failed: {e}")


def _build_context(symbol: str) -> dict:
    conn = get_db()
    cache = conn.execute("SELECT * FROM price_cache WHERE symbol=?", (symbol,)).fetchone()
    pos = conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    watch = conn.execute("SELECT * FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
    market = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
    conn.close()

    if not cache:
        return {"error": "無此標的快取資料，請先在儀表板按一次刷新"}
    ind = json.loads(dict(cache).get("data") or "{}")
    name = (dict(pos)["name"] if pos else (dict(watch)["name"] if watch else symbol))
    price = ind.get("price")

    parts = [
        f"標的: {symbol} ({name})",
        "",
        "【即時技術指標】",
        f"現價: {price}, RSI: {ind.get('rsi')}, β: {ind.get('beta')}",
        f"MA20: {ind.get('ma20')}, MA60: {ind.get('ma60')}",
        f"52週高/低: {ind.get('high52')} / {ind.get('low52')}",
        f"今日漲跌: {ind.get('change_1d')}%, 1月漲跌: {ind.get('change_1m')}%",
    ]
    if ind.get("nav"):
        prem = (price / ind["nav"] - 1) * 100 if price else None
        parts.append(f"ETF 折溢價: NAV {ind['nav']}，市價相對 NAV {prem:+.2f}%" if prem is not None else f"NAV {ind['nav']}")

    # 計算一次 17D 矩陣與基本面，落地到 SQL + Obsidian 供日後資料流複用
    matrix = None
    try:
        matrix = _build_technical_matrix_payload(symbol, "6mo")
    except Exception:
        matrix = None
    fundamentals = fetch_fundamentals(symbol)
    financials = fetch_financial_history(symbol)
    try:
        _persist_symbol_research(symbol, fundamentals, matrix, financials)
    except Exception as e:
        print(f"  [WARN] persist research {symbol} failed: {e}")

    # 17D 技術矩陣（核心新增）
    parts.append("")
    parts.append(_build_technical_matrix_text(symbol, matrix=matrix))

    # 若該股 17D 歷史不足，先回填過去半年（每週取樣）讓 AI 立刻有趨勢可看
    try:
        existing_cnt = 0
        conn2 = get_db()
        existing_cnt = conn2.execute(
            "SELECT COUNT(*) AS c FROM technical_matrix_snapshots WHERE symbol=?", (symbol,)
        ).fetchone()["c"]
        conn2.close()
        if existing_cnt < 4:
            _backfill_technical_matrix_history(symbol, lookback_days=180, step_days=5)
    except Exception as e:
        print(f"  [WARN] backfill {symbol} failed: {e}")

    # 過往數日 17D 偏向變化（供 AI 看趨勢，不只看當下）
    history = _load_matrix_bias_history(symbol, limit=12)
    if len(history) >= 2:
        trail = "；".join(
            f"{h['date'][5:]} {h.get('bias')}({h.get('net_score')})" for h in history
        )
        parts.append(f"【過往 17D 偏向變化】{trail}")

    # 基本面與估值（核心新增）
    parts.append("")
    parts.append(_build_fundamentals_text(symbol, price, fundamentals=fundamentals))

    # 歷史財報趨勢（季度營收/EPS YoY，供 AI 看基本面動能）
    fin_text = _build_financials_history_text(symbol, records=financials)
    if fin_text:
        parts.append("")
        parts.append(fin_text)

    parts.append("")
    if pos:
        d = dict(pos)
        ret_pct = (ind.get("price", d["cost_price"])/d["cost_price"]-1)*100
        parts.append(f"【持倉狀態】{d['shares']} 股 @ 成本 {d['cost_price']} (報酬 {ret_pct:+.2f}%)")
    if watch:
        d = dict(watch)
        parts.append(f"【觀察設定】進場目標: {d.get('target_entry')}, 停利: {d.get('target_profit')}, 停損: {d.get('target_stop')}")
        if d.get("notes"):
            parts.append(f"標的說明: {d['notes']}")
    if market:
        m = dict(market)
        parts.append(f"【大盤環境】VIX {m.get('vix')}, 風險等級 {m.get('risk_level')}, 警訊數 {m.get('warnings_count')}/3")

    return {"context": "\n".join(parts), "symbol": symbol, "name": name}

def _extract_tradingagents_sections(final_state) -> dict:
    keys = [
        "final_trade_decision",
        "trader_proposal",
        "risk_debate_state",
        "investment_debate_state",
        "market_report",
        "sentiment_report",
        "news_report",
        "fundamentals_report",
    ]
    out = {}
    for key in keys:
        value = final_state.get(key) if isinstance(final_state, dict) else getattr(final_state, key, None)
        if value:
            out[key] = str(value)[:2500]
    return out

def _run_tradingagents(symbol: str, mode: str = "full") -> dict:
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as exc:
        return {"error": f"TradingAgents 尚未安裝: {exc}"}

    analysts = ["market", "fundamentals"] if mode == "quick" else ["market", "fundamentals", "news", "social"]
    config = DEFAULT_CONFIG.copy()
    config.update({
        "llm_provider": "anthropic",
        "deep_think_llm": "claude-sonnet-4-6" if mode == "quick" else "claude-opus-4-7",
        "quick_think_llm": "claude-haiku-4-5-20251001",
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "output_language": "Chinese",
        "data_vendors": {
            "core_stock_apis": "yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
        },
    })
    try:
        settings = load_settings()
        keys = settings.get("api_keys", {})
        if keys.get("anthropic"): os.environ["ANTHROPIC_API_KEY"] = keys["anthropic"]
        if keys.get("openai"): os.environ["OPENAI_API_KEY"] = keys["openai"]
        if keys.get("google"): os.environ["GOOGLE_API_KEY"] = keys["google"]

        graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config)
        trade_date = str(date.today() - timedelta(days=1))
        final_state, decision = graph.propagate(symbol, trade_date)
        return {
            "symbol": symbol,
            "mode": mode,
            "trade_date": trade_date,
            "decision": decision,
            "analysts": analysts,
            "sections": _extract_tradingagents_sections(final_state),
        }
    except Exception as exc:
        return {"error": str(exc), "symbol": symbol, "mode": mode}

@app.get("/api/tradingagents/{symbol}")
def api_tradingagents(symbol: str, mode: str = "full"):
    mode = mode if mode in ("quick", "full") else "full"
    return _run_tradingagents(symbol, mode=mode)


# ─────────────── CLI 深度分析 (多代理人模擬，走訂閱免費) ───────────────

def _fetch_stock_context(symbol: str) -> str:
    """用 yfinance 抓取即時股票數據，組成分析上下文文字。"""
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        hist = tk.history(period="6mo")
        close = hist["Close"].astype(float) if len(hist) > 0 else pd.Series(dtype=float)

        price = round(float(close.iloc[-1]), 2) if len(close) > 0 else info.get("regularMarketPrice", "N/A")
        high52 = round(float(close.max()), 2) if len(close) > 20 else info.get("fiftyTwoWeekHigh", "N/A")
        low52 = round(float(close.min()), 2) if len(close) > 20 else info.get("fiftyTwoWeekLow", "N/A")

        # RSI
        rsi_val = "N/A"
        if len(close) >= 14:
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi_series = 100 - (100 / (1 + rs))
            rsi_val = round(float(rsi_series.iloc[-1]), 1)

        # MA
        ma20 = round(float(close.rolling(20).mean().iloc[-1]), 2) if len(close) >= 20 else "N/A"
        ma60 = round(float(close.rolling(60).mean().iloc[-1]), 2) if len(close) >= 60 else "N/A"

        # change
        change_1d = round(float((close.iloc[-1] / close.iloc[-2] - 1) * 100), 2) if len(close) >= 2 else "N/A"

        # Beta
        beta = round(float(info.get("beta", 0) or 0), 2)

        # Fundamentals
        pe = info.get("trailingPE", "N/A")
        pb = info.get("priceToBook", "N/A")
        mktcap = info.get("marketCap", "N/A")
        if isinstance(mktcap, (int, float)) and mktcap > 1e9:
            mktcap = f"{mktcap/1e9:.1f}B"
        sector = info.get("sector", "N/A")
        industry = info.get("industry", "N/A")
        name = info.get("shortName") or info.get("longName") or symbol

        # Volume
        vol = "N/A"
        if len(hist) > 0 and "Volume" in hist.columns:
            vol = f"{int(hist['Volume'].iloc[-1]):,}"

        lines = [
            f"標的: {symbol} ({name})",
            f"產業: {sector} / {industry}",
            f"現價: {price}, 日漲跌: {change_1d}%",
            f"RSI(14): {rsi_val}, MA20: {ma20}, MA60: {ma60}",
            f"52週高/低: {high52} / {low52}",
            f"Beta: {beta}, P/E: {pe}, P/B: {pb}, 市值: {mktcap}",
            f"今日成交量: {vol}",
        ]

        # 加入持倉/觀察資訊
        conn = get_db()
        pos = conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        watch = conn.execute("SELECT * FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
        market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
        conn.close()

        if pos:
            d = dict(pos)
            ret_pct = (float(price) / d["cost_price"] - 1) * 100 if isinstance(price, (int, float)) and d["cost_price"] > 0 else 0
            lines.append(f"持倉: {d['shares']} 股 @ 成本 {d['cost_price']} (報酬 {ret_pct:+.2f}%)")
        if watch:
            d = dict(watch)
            lines.append(f"觀察目標: 進場 {d.get('target_entry')}, 停利 {d.get('target_profit')}, 停損 {d.get('target_stop')}")
            if d.get("notes"):
                lines.append(f"備註: {d['notes']}")
        if market_row:
            m = dict(market_row)
            lines.append(f"大盤: VIX {m.get('vix')}, 風險等級 {m.get('risk_level')}, 警訊 {m.get('warnings_count')}/3")

        return "\n".join(lines)
    except Exception as e:
        return f"標的: {symbol}\n（數據抓取失敗: {e}）"


_CLI_DEEP_STEPS = [
    {
        "key": "market_report",
        "label": "技術分析師",
        "prompt": """你是專業的技術分析師。根據以下股票數據，產出**繁體中文**技術分析報告（markdown）：

{context}

請涵蓋：
1. 趨勢判斷（多頭/空頭/盤整）
2. 動能指標解讀（RSI、均線排列）
3. 關鍵支撐與壓力價位
4. 量價配合度
5. 短期技術面結論（看多/看空/中性）""",
    },
    {
        "key": "fundamentals_report",
        "label": "基本面分析師",
        "prompt": """你是專業的基本面分析師。根據以下股票數據，產出**繁體中文**基本面分析報告（markdown）：

{context}

請涵蓋：
1. 估值評估（P/E、P/B 與同業比較）
2. 市值與產業地位
3. 營收/獲利趨勢（如有數據）
4. 競爭優勢與護城河
5. 基本面結論（低估/合理/高估）""",
    },
    {
        "key": "news_report",
        "label": "新聞分析師",
        "prompt": """你是專業的新聞分析師。根據以下股票數據，從產業趨勢與潛在新聞面角度產出**繁體中文**分析（markdown）：

{context}

請涵蓋：
1. 該產業近期重大趨勢
2. 可能影響股價的催化劑（正面/負面）
3. 總體經濟環境影響
4. 新聞面結論""",
    },
    {
        "key": "sentiment_report",
        "label": "情緒分析師",
        "prompt": """你是市場情緒分析師。根據以下數據判斷市場對該標的的情緒狀態，產出**繁體中文**分析（markdown）：

{context}

請涵蓋：
1. 市場情緒（貪婪/恐懼/中性）
2. RSI + VIX 綜合判讀
3. 散戶 vs 法人可能動向
4. 情緒面結論""",
    },
    {
        "key": "investment_debate_state",
        "label": "多空辯論",
        "prompt": """你是投資辯論主持人。以下是四位分析師的報告與原始數據。請模擬一場**多空辯論**：

[原始數據]
{context}

[技術分析報告]
{market_report}

[基本面分析報告]
{fundamentals_report}

[新聞分析報告]
{news_report}

[情緒分析報告]
{sentiment_report}

請以**繁體中文** markdown 格式回應。禁止出現「綜上所述」「核心結論是」「修訂後」「基於以上分析」等贅詞，直接呈現內容。所有條列或序列內容強制用編號（1. 2. 3.）呈現：
## 多頭論點
（整合分析師報告中的正面因素，給出 3 個最強看多理由）

## 空頭論點
（整合報告中的風險與負面因素，給出 3 個最強看空理由）

## 辯論結論
（判定哪方論點更有力，給出多空比例如 60:40）""",
    },
    {
        "key": "risk_debate_state",
        "label": "風險委員會",
        "prompt": """你是投資風險委員會主席。以下是多空辯論結果與原始數據。請從風險管理角度做最後審查：

[原始數據]
{context}

[多空辯論]
{investment_debate_state}

請以**繁體中文** markdown 格式回應。禁止出現「綜上所述」「核心結論是」「修訂後」「基於以上分析」等贅詞，直接呈現內容。所有條列或序列內容強制用編號（1. 2. 3.）呈現：
## 主要風險因素
（列出 3 個最需要注意的風險）

## 風險緩解策略
（針對每個風險提出對策）

## 倉位建議
（建議投入資金比例、分批策略）

## 風險等級評估
（低/中/高風險，並說明理由）""",
    },
    {
        "key": "final_trade_decision",
        "label": "最終投資決策",
        "prompt": """你是資深投資組合經理。綜合所有分析與風險評估，做出最終投資決策：

[原始數據]
{context}

[技術分析]
{market_report}

[基本面分析]
{fundamentals_report}

[多空辯論]
{investment_debate_state}

[風險委員會]
{risk_debate_state}

請以**繁體中文** markdown 格式回應。禁止出現「綜上所述」「核心結論是」「修訂後」「基於以上分析」等贅詞，直接呈現內容。所有條列或序列內容強制用編號（1. 2. 3.）呈現：
## 最終決策
**買入 / 持有 / 賣出 / 觀望**（明確選一個）

## 操作計畫
1. 進場價位與時機
2. 停損價位（明確數字）
3. 停利目標（明確數字）
4. 建議倉位比例

## 時間框架
（短線 / 波段 / 長期）

## 信心度
（1~10 分，並說明理由）

## 一句話摘要
（用一句話概括你的建議）""",
    },
]


@app.get("/api/tradingagents-cli/{symbol}")
def api_tradingagents_cli_stream(symbol: str, mode: str = "full"):
    """CLI 版深度分析 — SSE 串流，走訂閱配額不扣 API 費。"""
    import time as _time

    def event_stream():
        def emit(event_type: str, data: dict):
            payload = json.dumps(data, ensure_ascii=False)
            return f"event: {event_type}\ndata: {payload}\n\n"

        t0 = _time.time()

        # 決定要跑哪些步驟
        if mode == "quick":
            step_keys = ["market_report", "fundamentals_report", "investment_debate_state", "final_trade_decision"]
        else:
            step_keys = [s["key"] for s in _CLI_DEEP_STEPS]

        steps = [s for s in _CLI_DEEP_STEPS if s["key"] in step_keys]

        # 讀取設定
        settings = load_settings()
        roles = settings["roles"]
        # 深度分析用 analyst 角色的 provider/model
        analyst_cfg = roles.get("analyst", {})
        provider = analyst_cfg.get("provider", "anthropic")
        model = analyst_cfg.get("model", "opus")
        cli_mode = analyst_cfg.get("mode", "cli")
        api_key = settings.get("api_keys", {}).get(provider, "")

        yield emit("started", {
            "symbol": symbol,
            "mode": mode,
            "provider": provider,
            "model": model,
            "cli_mode": cli_mode,
            "total_steps": len(steps),
        })

        # 抓股票數據
        yield emit("step_start", {"label": "抓取股票數據", "elapsed": round(_time.time() - t0, 1)})
        context = _fetch_stock_context(symbol)
        yield emit("step_done", {"label": "抓取股票數據", "output": context, "elapsed": round(_time.time() - t0, 1)})

        # 依序跑每個代理人
        results = {"context": context}
        for i, step in enumerate(steps):
            label = step["label"]
            key = step["key"]

            yield emit("step_start", {
                "label": label,
                "step_index": i + 1,
                "total_steps": len(steps),
                "elapsed": round(_time.time() - t0, 1),
            })

            # 組 prompt — 替換所有已有的結果
            prompt = step["prompt"]
            for rk, rv in results.items():
                prompt = prompt.replace("{" + rk + "}", str(rv))

            try:
                if cli_mode == "cli":
                    output = call_cli(provider, model, prompt, timeout=300)
                else:
                    output = call_llm(provider, model, prompt, api_key=api_key, mode="api")

                results[key] = output

                yield emit("step_done", {
                    "label": label,
                    "key": key,
                    "step_index": i + 1,
                    "output": output,
                    "elapsed": round(_time.time() - t0, 1),
                })
            except Exception as e:
                error_msg = str(e)
                results[key] = f"（{label}分析失敗: {error_msg}）"
                yield emit("step_error", {
                    "label": label,
                    "key": key,
                    "step_index": i + 1,
                    "error": error_msg,
                    "elapsed": round(_time.time() - t0, 1),
                })

        # ── 儲存分析結果到資料庫 ──
        sections_to_save = {k: v for k, v in results.items() if k != "context"}
        decision_summary = ""
        ftd = results.get("final_trade_decision", "")
        # 嘗試擷取一句話摘要
        for line in ftd.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("*"):
                decision_summary = stripped[:200]
                break

        try:
            # 查名稱
            conn = get_db()
            row = conn.execute(
                "SELECT name FROM positions WHERE symbol=? UNION SELECT name FROM watchlist WHERE symbol=?",
                (symbol, symbol)
            ).fetchone()
            sym_name = dict(row)["name"] if row else symbol
            conn.execute(
                """INSERT INTO analysis_results
                   (symbol, name, ts, mode, provider, model, elapsed, decision_summary, sections)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, sym_name, datetime.now().isoformat(),
                 mode, provider, model,
                 round(_time.time() - t0, 1),
                 decision_summary,
                 json.dumps(sections_to_save, ensure_ascii=False))
            )
            analysis_ts = conn.execute(
                "SELECT ts FROM analysis_results WHERE rowid = last_insert_rowid()"
            ).fetchone()[0]

            # ── 從最終決策中解析進出場價位，回寫到 watchlist ──
            import re as _re
            def _extract_prices(text):
                """從決策文本中抓數字價位。先去掉 markdown 粗體標記再解析。"""
                prices = {}
                clean = text.replace('**', '')  # 去掉 markdown 粗體

                # 進場 / 買入 — 嘗試抓「進場...數字～數字」的範圍格式
                m = _re.search(r'進場.*?(\d+(?:\.\d+)?)\s*[~～\-至到]+\s*(\d+(?:\.\d+)?)', clean)
                if m:
                    prices['entry'] = float(m.group(1))
                    prices['add'] = float(m.group(2))
                else:
                    # 退而求其次：抓「進場」或「買入」或「回測」後面第一個數字
                    m = _re.search(r'(?:進場|買入|回測)\D{0,30}?(\d+(?:\.\d+)?)', clean)
                    if m:
                        prices['entry'] = float(m.group(1))

                # 停損 — 「停損」後面最近的數字
                m = _re.search(r'停損\D{0,20}?(\d+(?:\.\d+)?)', clean)
                if m:
                    prices['stop'] = float(m.group(1))

                # 停利 — 「停利」後面最近的數字
                m = _re.search(r'停利\D{0,20}?(\d+(?:\.\d+)?)', clean)
                if m:
                    prices['profit'] = float(m.group(1))

                return prices

            extracted = _extract_prices(ftd)
            wrote_watchlist_levels = False
            if extracted:
                watch_row = conn.execute("SELECT id, target_entry, target_stop, target_profit FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
                if watch_row:
                    wd = dict(watch_row)
                    updates = []
                    params = []
                    if extracted.get('entry'):
                        updates.append("target_entry=?")
                        params.append(extracted['entry'])
                    if extracted.get('add'):
                        updates.append("target_add=?")
                        params.append(extracted['add'])
                    if extracted.get('stop'):
                        updates.append("target_stop=?")
                        params.append(extracted['stop'])
                    if extracted.get('profit'):
                        updates.append("target_profit=?")
                        params.append(extracted['profit'])
                    if updates:
                        params.append(wd["id"])
                        conn.execute(f"UPDATE watchlist SET {', '.join(updates)} WHERE id=?", params)
                        wrote_watchlist_levels = True

            conn.commit()
            vault = _get_vault()
            if vault:
                _obsidian_write_analysis(vault, {
                    "symbol": symbol,
                    "name": sym_name,
                    "ts": analysis_ts,
                    "mode": mode,
                    "provider": provider,
                    "model": model,
                    "elapsed": round(_time.time() - t0, 1),
                    "decision_summary": decision_summary,
                    "sections": sections_to_save,
                })
                sync_kinds = ["analysis"]
                if wrote_watchlist_levels:
                    _obsidian_write_watchlist_snapshot(vault, conn, symbol)
                    sync_kinds.append("watchlist")
                _obsidian_post_write_sync(vault, kinds=tuple(sync_kinds))
            conn.close()
        except Exception:
            pass  # 儲存失敗不影響串流

        yield emit("done", {
            "symbol": symbol,
            "mode": mode,
            "elapsed": round(_time.time() - t0, 1),
            "sections": sections_to_save,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

# ─────────────── 分析結果 CRUD ───────────────

@app.get("/api/analysis/{symbol}")
def api_get_analysis(symbol: str, limit: int = 10):
    """取得某標的的歷史分析結果。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM analysis_results WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        (symbol, limit)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["sections"] = json.loads(d["sections"]) if d.get("sections") else {}
        except Exception:
            d["sections"] = {}
        out.append(d)
    return {"analyses": out}


@app.get("/api/analysis")
def api_get_all_analysis(limit: int = 50):
    """取得最近的所有分析結果（概覽）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, symbol, name, ts, mode, provider, model, elapsed, decision_summary FROM analysis_results ORDER BY ts DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return {"analyses": [dict(r) for r in rows]}


@app.delete("/api/analysis/{aid}")
def api_delete_analysis(aid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM analysis_results WHERE id=?", (aid,)).fetchone()
    conn.execute("DELETE FROM analysis_results WHERE id=?", (aid,))
    conn.commit()
    vault = _get_vault()
    if vault and row:
        _obsidian_delete_analysis(vault, dict(row))
        _obsidian_post_write_sync(vault, kinds=("analysis",))
    conn.close()
    return {"ok": True}


@app.get("/api/llm-analyze-stream/{symbol}")
def api_llm_analyze_stream(symbol: str, mode: str = "both"):
    """SSE 串流端點：分析過程逐步推送進度與結果。"""
    import time as _time

    def event_stream():
        def emit(event_type: str, data: dict):
            payload = json.dumps(data, ensure_ascii=False)
            return f"event: {event_type}\ndata: {payload}\n\n"

        t0 = _time.time()
        ctx = _build_context(symbol)
        if "error" in ctx:
            yield emit("error", {"error": ctx["error"]})
            return

        yield emit("started", {
            "symbol": symbol,
            "name": ctx["name"],
            "context_lines": len(ctx["context"].split("\n")),
        })

        settings = load_settings()
        keys = settings["api_keys"]
        roles = settings["roles"]

        # ── Step 1: 分析師 ──
        if mode in ("analyst", "both"):
            analyst = roles["analyst"]
            yield emit("step_start", {
                "role": "analyst",
                "provider": analyst["provider"],
                "model": analyst["model"],
                "mode": analyst.get("mode", "api"),
                "elapsed": round(_time.time() - t0, 1),
            })

            analyst_prompt = f"""你是專業金融分析師。我提供你以下這檔標的的完整資料：歷史財報與基本面估值、17D 全景技術矩陣（含歷史偏向變化）、即時技術指標、持倉與大盤環境。請你**交叉判讀**這些資料後，給出操作建議與投資建議。請用**繁體中文**。

{ctx["context"]}

**請這樣分析**：
- 把財報（營收/EPS/毛利趨勢與成長性）、估值（本益比/PEG/賣方目標價）、17D 技術（共振區、各維度偏向、歷史偏向變化）三者**交叉比對**，而不是各看各的。
- 重點在三者的**關係**：基本面與技術面是同向強化，還是彼此背離？若背離（例如財報轉弱但技術轉強、或基本面紮實但技術破位），請明確指出並說明你怎麼權衡。
- 操作點位要有依據：進場參考技術共振區與估值是否合理；停損依結構失效（跌破支撐/共振區）；停利對照賣方目標價與壓力共振區。
- 資料缺口（17D 標示未接外部 feed 的維度、或財報缺漏）請降權，不要過度解讀。

直接給出結論，禁止「綜上所述」「核心結論是」等贅詞。請涵蓋（markdown，條列用編號）：

1. **交叉判讀** — 財報 × 估值 × 17D 技術三者的關係與你的綜合研判（這是核心）
2. **操作建議** — 買/賣/持有，以及有依據的進場、停損、停利價位
3. **投資建議** — 對不同時間框架（短線 / 中線 / 長線）的部位與策略建議
4. **風險與失效條件** — 什麼情況代表這個判斷失效、需要重新評估
"""
            try:
                analyst_text = call_llm(
                    analyst["provider"], analyst["model"],
                    analyst_prompt,
                    keys.get(analyst["provider"], ""),
                    mode=analyst.get("mode", "api"),
                )
                yield emit("step_done", {
                    "role": "analyst",
                    "provider": analyst["provider"],
                    "model": analyst["model"],
                    "mode": analyst.get("mode", "api"),
                    "output": analyst_text,
                    "elapsed": round(_time.time() - t0, 1),
                })
            except Exception as e:
                yield emit("step_error", {
                    "role": "analyst",
                    "provider": analyst["provider"],
                    "model": analyst["model"],
                    "error": str(e),
                    "elapsed": round(_time.time() - t0, 1),
                })
                yield emit("done", {"elapsed": round(_time.time() - t0, 1)})
                return

            # ── Step 2: 審查員 ──
            if mode == "both":
                reviewer = roles["reviewer"]
                yield emit("step_start", {
                    "role": "reviewer",
                    "provider": reviewer["provider"],
                    "model": reviewer["model"],
                    "mode": reviewer.get("mode", "api"),
                    "elapsed": round(_time.time() - t0, 1),
                })

                reviewer_prompt = f"""你是嚴格的投資審查員，負責**找出分析師報告的盲點與弱點**。

[原始數據]
{ctx["context"]}

[分析師報告]
{analyst_text}

請以**繁體中文** markdown 格式給出**犀利但建設性**的審查意見。禁止出現「綜上所述」「核心結論是」「修訂後」「基於以上分析」等贅詞，直接呈現內容。所有條列或序列內容強制用編號（1. 2. 3.）呈現：

## 一、分析師說對的地方
（簡述 1~2 點）

## 二、我有疑慮的地方
（指出邏輯漏洞、忽略的風險、過度樂觀/悲觀）

## 三、我認為錯誤或缺失的部分
（具體指出）

## 四、修正後的建議
（給出你認為更穩健的操作版本）
"""
                try:
                    reviewer_text = call_llm(
                        reviewer["provider"], reviewer["model"],
                        reviewer_prompt,
                        keys.get(reviewer["provider"], ""),
                        mode=reviewer.get("mode", "api"),
                    )
                    yield emit("step_done", {
                        "role": "reviewer",
                        "provider": reviewer["provider"],
                        "model": reviewer["model"],
                        "mode": reviewer.get("mode", "api"),
                        "output": reviewer_text,
                        "elapsed": round(_time.time() - t0, 1),
                    })
                except Exception as e:
                    yield emit("step_error", {
                        "role": "reviewer",
                        "provider": reviewer["provider"],
                        "model": reviewer["model"],
                        "error": str(e),
                        "elapsed": round(_time.time() - t0, 1),
                    })

        yield emit("done", {"elapsed": round(_time.time() - t0, 1)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


@app.get("/api/llm-analyze/{symbol}")
def api_llm_analyze(symbol: str, mode: str = "both"):
    """LLM 分析。
    mode:
      - 'analyst': 只跑分析師
      - 'reviewer': 不適用（需要分析師輸出）
      - 'both': 分析師 → 審查員（預設）
    """
    ctx = _build_context(symbol)
    if "error" in ctx:
        return ctx

    try:
        result = run_workflow(ctx["context"], mode=mode)
        return {
            "symbol": symbol,
            "name": ctx["name"],
            "context": ctx["context"],
            "steps": result["steps"],
            "mode": mode,
        }
    except Exception as e:
        return {"error": f"LLM 工作流失敗: {e}"}

def _run_deep_analysis(symbol: str, mode: str = "analyst") -> dict:
    """Run one symbol's deep analysis (17D + 財報 交叉判讀) and store the result.

    Reusable from the parallel batch endpoint. Returns a compact status dict.
    The analyst (and optional reviewer) LLM calls are the slow part; running
    several of these concurrently is what removes the per-symbol queue.
    """
    import time as _time
    t0 = _time.time()
    ctx = _build_context(symbol)
    if "error" in ctx:
        return {"symbol": symbol, "ok": False, "error": ctx["error"], "elapsed": round(_time.time() - t0, 1)}
    try:
        result = run_workflow(ctx["context"], mode=mode)
    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": str(e), "elapsed": round(_time.time() - t0, 1)}

    steps = result.get("steps", [])
    analyst_step = next((s for s in steps if s.get("role") == "analyst"), None)
    reviewer_step = next((s for s in steps if s.get("role") == "reviewer"), None)
    if not analyst_step or analyst_step.get("error"):
        return {"symbol": symbol, "ok": False,
                "error": (analyst_step or {}).get("error", "no analyst output"),
                "elapsed": round(_time.time() - t0, 1)}

    sections = {"analyst": analyst_step.get("output", "")}
    if reviewer_step and reviewer_step.get("output"):
        sections["reviewer"] = reviewer_step["output"]
    # 一句話摘要
    decision_summary = ""
    for line in (analyst_step.get("output", "") or "").split("\n"):
        s = line.strip().lstrip("#*0123456789. ").strip()
        if len(s) > 8:
            decision_summary = s[:200]
            break

    elapsed = round(_time.time() - t0, 1)
    provider = analyst_step.get("provider", "")
    model = analyst_step.get("model", "")
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT name FROM positions WHERE symbol=? UNION SELECT name FROM watchlist WHERE symbol=?",
            (symbol, symbol),
        ).fetchone()
        sym_name = dict(row)["name"] if row else ctx.get("name", symbol)
        conn.execute(
            """INSERT INTO analysis_results
               (symbol, name, ts, mode, provider, model, elapsed, decision_summary, sections)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, sym_name, datetime.now().isoformat(), mode, provider, model,
             elapsed, decision_summary, json.dumps(sections, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [WARN] store analysis {symbol} failed: {e}")
    return {"symbol": symbol, "ok": True, "name": sym_name,
            "decision_summary": decision_summary, "elapsed": elapsed}


@app.post("/api/batch-deep-analyze")
def api_batch_deep_analyze(mode: str = "analyst", max_workers: int = 3,
                           scope: str = "all"):
    """平行批次深度分析（17D + 財報 交叉判讀）。SSE 串流進度。

    - scope: 'all'（持倉+觀察）/ 'positions' / 'watchlist'
    - mode: 'analyst'（快，只分析師）/ 'both'（含審查員，較慢）
    - max_workers: 同時併發數；上限 4 以尊重 CLI 訂閱併發限制
    """
    max_workers = max(1, min(max_workers, 4))

    conn = get_db()
    syms, seen = [], set()
    tables = []
    if scope in ("all", "positions"):
        tables.append("positions")
    if scope in ("all", "watchlist"):
        tables.append("watchlist")
    for tbl in tables:
        for r in conn.execute(f"SELECT symbol, name, category FROM {tbl}").fetchall():
            if r["symbol"] in seen or _is_test_symbol(r["symbol"], r["name"], r["category"]):
                continue
            seen.add(r["symbol"])
            syms.append(r["symbol"])
    conn.close()

    def event_generator():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time as _time
        t0 = _time.time()
        total = len(syms)
        if total == 0:
            yield "data: " + json.dumps({"status": "done", "percent": 100, "message": "無標的", "results": []}) + "\n\n"
            return
        yield "data: " + json.dumps({"status": "progress", "percent": 3,
                                     "message": f"開始平行分析 {total} 檔（併發 {max_workers}）..."}) + "\n\n"
        results = []
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_run_deep_analysis, sym, mode): sym for sym in syms}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"symbol": sym, "ok": False, "error": str(e)}
                results.append(res)
                done += 1
                pct = round(3 + done / total * 94)
                status = "✓" if res.get("ok") else "✗"
                yield "data: " + json.dumps({
                    "status": "progress", "percent": pct,
                    "message": f"[{done}/{total}] {status} {sym}"
                    + (f"：{res.get('decision_summary','')[:40]}" if res.get("ok") else f"（{res.get('error','')[:40]}）"),
                    "symbol": sym, "result": res,
                }, ensure_ascii=False) + "\n\n"
        ok = sum(1 for r in results if r.get("ok"))
        yield "data: " + json.dumps({
            "status": "done", "percent": 100,
            "message": f"完成 {ok}/{total} 檔，耗時 {round(_time.time()-t0,1)}s",
            "results": results,
        }, ensure_ascii=False) + "\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/diagnose/{symbol}")
def api_diagnose(symbol: str, bypass_cache: bool = False):
    """On-demand diagnosis for a symbol."""
    conn = get_db()
    if bypass_cache:
        try:
            ind = fetch_indicators(symbol)
            if ind:
                cursor = conn.cursor()
                store_price_cache(cursor, symbol, ind)
                conn.commit()
        except Exception:
            pass
    market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
    market = dict(market_row) if market_row else {}
    cache = conn.execute("SELECT * FROM price_cache WHERE symbol=?", (symbol,)).fetchone()
    pos = conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    watch = conn.execute("SELECT * FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
    name = (dict(pos)["name"] if pos else (dict(watch)["name"] if watch else symbol))
    conn.close()
    if not cache:
        return {"error": "no price cache for symbol"}
    ind = json.loads(dict(cache).get("data") or "{}")
    diag = diagnose(symbol, name, ind, market, position=dict(pos) if pos else None)
    watch_recommendation = _recommend_watch_levels(ind, market) if watch else None
    watch_id = dict(watch)["id"] if watch else None
    return sanitize_float_values({
        "symbol": symbol,
        "name": name,
        "diagnosis": diag,
        "indicators": ind,
        "watch_id": watch_id,
        "watch_recommendation": watch_recommendation,
    })


# ─────────────── Obsidian 雙向同步 ───────────────

def _get_vault() -> Optional[Path]:
    """Return expanded vault Path if configured, else None."""
    vp = load_settings().get("obsidian_vault_path", "")
    if not vp:
        return None
    p = Path(vp).expanduser()
    return p if p.is_dir() else None


def _fmt(v) -> str:
    """Format a value for YAML frontmatter (None → empty string)."""
    if v is None:
        return ""
    return str(v)


def _safe_obsidian_name(value: str) -> str:
    return re.sub(r'[\\/*?"<>|]', "_", value or "")


def _obsidian_portfolio_index_path(vault: Path) -> Path:
    return vault / "Portfolio" / "持倉總覽.md"


def _obsidian_position_note_path(vault: Path, symbol: str) -> Path:
    return vault / "Portfolio" / "Positions" / f"{_safe_obsidian_name(symbol)}.md"


def _obsidian_watchlist_index_path(vault: Path) -> Path:
    return vault / "Watchlist" / "觀察清單總覽.md"


def _obsidian_alerts_index_path(vault: Path) -> Path:
    return vault / "Alerts" / "警報總覽.md"


def _obsidian_domain_latest_entry_path(vault: Path, domain: str) -> Path:
    return vault / "Research" / _safe_obsidian_name(domain) / "研究入口.md"


def _obsidian_domain_snapshot_path(vault: Path, domain: str, ts_value: str) -> Path:
    research_dir = _obsidian_domain_dir(vault, domain, ts_value)
    safe_domain = _safe_obsidian_name(domain)
    return research_dir / f"{safe_domain} 研究快照.md"


def _obsidian_bool(v) -> str:
    return "1" if v else "0"


def _extract_json_codeblock(section: str):
    section = (section or "").strip()
    if not section:
        return None
    if section.startswith("```"):
        lines = section.splitlines()
        if len(lines) >= 3:
            section = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(section)
    except Exception:
        return None


def _obsidian_write_position(vault: Path, pos: dict) -> None:
    """Write/update a single position note to Obsidian."""
    sym = pos.get("symbol", "")
    if not sym:
        return
    safe = _safe_obsidian_name(sym)
    pos_dir = vault / "Portfolio" / "Positions"
    pos_dir.mkdir(parents=True, exist_ok=True)
    note = pos_dir / f"{safe}.md"
    content = f"""---
type: position
symbol: {sym}
name: {_fmt(pos.get('name'))}
category: {_fmt(pos.get('category'))}
shares: {_fmt(pos.get('shares'))}
cost_price: {_fmt(pos.get('cost_price'))}
currency: {_fmt(pos.get('currency', 'TWD'))}
purchase_date: {_fmt(pos.get('purchase_date'))}
target_entry: {_fmt(pos.get('target_entry'))}
target_profit: {_fmt(pos.get('target_profit'))}
target_stop: {_fmt(pos.get('target_stop'))}
updated: {date.today().isoformat()}
---

# {pos.get('name', sym)} ({sym})

| 欄位 | 數值 |
|------|------|
| 股數 | {_fmt(pos.get('shares'))} |
| 成本價 | {_fmt(pos.get('cost_price'))} {_fmt(pos.get('currency','TWD'))} |
| 進場日 | {_fmt(pos.get('purchase_date'))} |
| 目標進場 | {_fmt(pos.get('target_entry'))} |
| 目標停利 | {_fmt(pos.get('target_profit'))} |
| 目標停損 | {_fmt(pos.get('target_stop'))} |

## 個人筆記

<!-- 此處可自由填寫，不影響同步 -->

## 連結
[[Portfolio/持倉總覽|回到持倉總覽]]
[[Alerts/Symbols/{safe}|{sym} 警報中心]]
"""
    note.write_text(content, encoding="utf-8")


def _obsidian_write_portfolio_index(vault: Path, positions: list) -> None:
    """Rewrite the Portfolio overview note."""
    idx = _obsidian_portfolio_index_path(vault)
    idx.parent.mkdir(parents=True, exist_ok=True)
    def _pos_row(p):
        safe = _safe_obsidian_name(p.get("symbol", ""))
        return (
            f"| [[Positions/{safe}|{p.get('name', p.get('symbol',''))}]] "
            f"| {p.get('symbol','')} | {p.get('shares','')} | {p.get('cost_price','')} | {p.get('currency','')} |"
        )
    rows = "\n".join(_pos_row(p) for p in positions)
    content = f"""---
type: portfolio-index
updated: {date.today().isoformat()}
---

# 持倉總覽

| 名稱 | 代號 | 股數 | 成本價 | 幣別 |
|------|------|------|--------|------|
{rows}
"""
    idx.write_text(content, encoding="utf-8")


def _obsidian_write_watchlist_item(vault: Path, watch: dict) -> None:
    """Write/update a single watchlist note to Obsidian."""
    sym = watch.get("symbol", "")
    if not sym:
        return
    safe = _safe_obsidian_name(sym)
    wl_dir = vault / "Watchlist"
    wl_dir.mkdir(parents=True, exist_ok=True)
    note = wl_dir / f"{safe}.md"
    content = f"""---
type: watchlist
symbol: {sym}
name: {_fmt(watch.get('name'))}
category: {_fmt(watch.get('category'))}
currency: {_fmt(watch.get('currency', 'TWD'))}
target_entry: {_fmt(watch.get('target_entry'))}
target_add: {_fmt(watch.get('target_add'))}
target_profit: {_fmt(watch.get('target_profit'))}
target_stop: {_fmt(watch.get('target_stop'))}
updated: {date.today().isoformat()}
---

# {watch.get('name', sym)} ({sym})

## 觀察重點

{watch.get('notes') or '<!-- 此處可填入觀察重點 -->'}

## 連結
[[Watchlist/觀察清單總覽|回到觀察清單]]
[[Alerts/Symbols/{safe}|{sym} 警報中心]]
"""
    note.write_text(content, encoding="utf-8")


def _obsidian_write_watchlist_index(vault: Path, items: list) -> None:
    """Rewrite the Watchlist overview note."""
    idx = _obsidian_watchlist_index_path(vault)
    idx.parent.mkdir(parents=True, exist_ok=True)
    def _wl_row(w):
        safe = _safe_obsidian_name(w.get("symbol", ""))
        return (
            f"| [[{safe}|{w.get('name', w.get('symbol',''))}]] "
            f"| {w.get('symbol','')} | {w.get('currency','')} | {_fmt(w.get('target_entry'))} | {_fmt(w.get('target_stop'))} |"
        )
    rows = "\n".join(_wl_row(w) for w in items)
    content = f"""---
type: watchlist-index
updated: {date.today().isoformat()}
---

# 觀察清單總覽

| 名稱 | 代號 | 幣別 | 目標進場 | 停損 |
|------|------|------|----------|------|
{rows}
"""
    idx.write_text(content, encoding="utf-8")


def _obsidian_alert_note_path(vault: Path, alert: dict) -> Path:
    ts = alert.get("ts", "") or datetime.now().isoformat(timespec="seconds")
    day = ts[:10] if ts else date.today().isoformat()
    alert_dir = vault / "Alerts" / "Entries" / day
    safe_name = "__".join([
        _safe_obsidian_name(ts.replace(":", "-")),
        _safe_obsidian_name(alert.get("symbol", "")),
        _safe_obsidian_name(alert.get("type", "")),
    ]).strip("_")
    return alert_dir / f"{safe_name or 'alert'}.md"


def _obsidian_alert_symbol_index_path(vault: Path, symbol: str) -> Path:
    return vault / "Alerts" / "Symbols" / f"{_safe_obsidian_name(symbol)}.md"


def _obsidian_alert_day_index_path(vault: Path, day: str) -> Path:
    return vault / "Alerts" / f"{day}.md"


def _obsidian_related_symbol_links(vault: Path, symbol: str) -> list[str]:
    links = []
    safe_symbol = _safe_obsidian_name(symbol)
    watch = vault / "Watchlist" / f"{safe_symbol}.md"
    pos = vault / "Portfolio" / "Positions" / f"{safe_symbol}.md"
    if watch.exists():
        links.append(f"[[Watchlist/{safe_symbol}|觀察清單]]")
    if pos.exists():
        links.append(f"[[Portfolio/Positions/{safe_symbol}|持倉]]")
    return links


def _obsidian_write_alert(vault: Path, alert: dict) -> None:
    """Write/update a single alert note to Obsidian."""
    note = _obsidian_alert_note_path(vault, alert)
    note.parent.mkdir(parents=True, exist_ok=True)
    day = (alert.get("ts", "") or "")[:10] or date.today().isoformat()
    symbol = alert.get("symbol", "")
    related_links = _obsidian_related_symbol_links(vault, symbol)
    related_links.append(f"[[Alerts/Symbols/{_safe_obsidian_name(symbol)}|{symbol} 警報中心]]")
    related_links.append(f"[[Alerts/{day}|{day} 警報日誌]]")
    content = f"""---
type: alert
symbol: {_fmt(alert.get('symbol'))}
ts: {_fmt(alert.get('ts'))}
level: {_fmt(alert.get('level'))}
alert_type: {_fmt(alert.get('type'))}
price: {_fmt(alert.get('price'))}
acknowledged: {_obsidian_bool(alert.get('acknowledged'))}
updated: {date.today().isoformat()}
---

# {_fmt(alert.get('symbol'))} {_fmt(alert.get('type_label') or alert.get('type'))}

## 訊息

{alert.get('message') or '—'}

## 診斷

{alert.get('diagnosis') or '—'}

## 關聯

{chr(10).join(f"- {link}" for link in related_links)}
"""
    note.write_text(content, encoding="utf-8")


def _obsidian_delete_alert(vault: Path, alert: dict) -> None:
    note = _obsidian_alert_note_path(vault, alert)
    if note.exists():
        note.unlink()


def _obsidian_rebuild_alert_views(vault: Path) -> None:
    entries_root = vault / "Alerts" / "Entries"
    alerts_root = vault / "Alerts"
    symbols_root = alerts_root / "Symbols"
    alerts_root.mkdir(parents=True, exist_ok=True)
    symbols_root.mkdir(parents=True, exist_ok=True)

    by_day: dict[str, list[dict]] = {}
    by_symbol: dict[str, list[dict]] = {}

    for note_path in sorted(entries_root.rglob("*.md")) if entries_root.exists() else []:
        parsed = _obsidian_parse_alert(note_path)
        if not parsed:
            continue
        ts = parsed.get("ts", "")
        day = ts[:10] if ts else note_path.parent.name
        parsed["_path"] = note_path
        by_day.setdefault(day, []).append(parsed)
        by_symbol.setdefault(parsed["symbol"], []).append(parsed)

    for day, items in by_day.items():
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        lines = []
        for item in items:
            rel = item["_path"].relative_to(vault)
            label = item.get("message") or item.get("type") or item.get("symbol")
            lines.append(
                f"- [[{rel.as_posix()}|{item['symbol']} {item.get('type','')}]]"
                f" · {item.get('level','')} · {label}"
            )
        day_path = _obsidian_alert_day_index_path(vault, day)
        day_path.write_text(
            f"---\n"
            f"type: alert-day-index\n"
            f"day: {day}\n"
            f"updated: {date.today().isoformat()}\n"
            f"---\n\n"
            f"# {day} 警報日誌\n\n"
            f"{chr(10).join(lines) or '（無警報）'}\n",
            encoding="utf-8",
        )

    for symbol, items in by_symbol.items():
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        related_links = _obsidian_related_symbol_links(vault, symbol)
        lines = []
        for item in items:
            rel = item["_path"].relative_to(vault)
            day = item.get("ts", "")[:10]
            lines.append(
                f"- [[{rel.as_posix()}|{item.get('ts','')} {item.get('type','')}]]"
                f" · [[Alerts/{day}|{day}]] · {item.get('message') or '—'}"
            )
        symbol_path = _obsidian_alert_symbol_index_path(vault, symbol)
        symbol_path.parent.mkdir(parents=True, exist_ok=True)
        symbol_path.write_text(
            f"---\n"
            f"type: alert-symbol-index\n"
            f"symbol: {symbol}\n"
            f"updated: {date.today().isoformat()}\n"
            f"---\n\n"
            f"# {symbol} 警報中心\n\n"
            f"## 關聯標的\n\n"
            f"{chr(10).join(f'- {link}' for link in related_links) or '（無）'}\n\n"
            f"## 警報紀錄\n\n"
            f"{chr(10).join(lines) or '（無警報）'}\n",
            encoding="utf-8",
        )

    day_links = [
        f"- [[Alerts/{day}|{day}]] ({len(items)})"
        for day, items in sorted(by_day.items(), reverse=True)
    ]
    symbol_links = [
        f"- [[Alerts/Symbols/{_safe_obsidian_name(symbol)}|{symbol}]] ({len(items)})"
        for symbol, items in sorted(by_symbol.items())
    ]
    _obsidian_alerts_index_path(vault).write_text(
        f"---\n"
        f"type: alerts-index\n"
        f"updated: {date.today().isoformat()}\n"
        f"---\n\n"
        f"# 警報總覽\n\n"
        f"## 依日期\n\n"
        f"{chr(10).join(day_links) or '（無警報）'}\n\n"
        f"## 依個股\n\n"
        f"{chr(10).join(symbol_links) or '（無警報）'}\n",
        encoding="utf-8",
    )


def _obsidian_write_position_snapshot(vault: Path, conn, symbol: str) -> None:
    """Rewrite one position note plus the portfolio index from current DB state."""
    row = conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    if row:
        _obsidian_write_position(vault, dict(row))
    all_pos = [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
    _obsidian_write_portfolio_index(vault, all_pos)


def _obsidian_write_watchlist_snapshot(vault: Path, conn, symbol: str) -> None:
    """Rewrite one watchlist note plus the watchlist index from current DB state."""
    row = conn.execute("SELECT * FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
    if row:
        _obsidian_write_watchlist_item(vault, dict(row))
    all_wl = [dict(r) for r in conn.execute("SELECT * FROM watchlist").fetchall()]
    _obsidian_write_watchlist_index(vault, all_wl)


def _obsidian_analysis_note_path(vault: Path, analysis: dict) -> Path:
    symbol = _safe_obsidian_name(analysis.get("symbol", "UNKNOWN"))
    ts = _safe_obsidian_name((analysis.get("ts") or "").replace(":", "-"))
    analysis_dir = vault / "Analysis" / symbol
    return analysis_dir / f"{ts or 'analysis'}.md"


def _obsidian_write_analysis(vault: Path, analysis: dict) -> None:
    note = _obsidian_analysis_note_path(vault, analysis)
    note.parent.mkdir(parents=True, exist_ok=True)
    sections = analysis.get("sections")
    if isinstance(sections, str):
        try:
            sections = json.loads(sections)
        except Exception:
            sections = {}
    sections_json = json.dumps(sections or {}, ensure_ascii=False, indent=2)
    content = f"""---
type: analysis-result
symbol: {_fmt(analysis.get('symbol'))}
name: {_fmt(analysis.get('name'))}
ts: {_fmt(analysis.get('ts'))}
mode: {_fmt(analysis.get('mode'))}
provider: {_fmt(analysis.get('provider'))}
model: {_fmt(analysis.get('model'))}
elapsed: {_fmt(analysis.get('elapsed'))}
decision_summary: {_fmt(analysis.get('decision_summary'))}
updated: {date.today().isoformat()}
---

# {_fmt(analysis.get('name') or analysis.get('symbol'))} ({_fmt(analysis.get('symbol'))})

## 決策摘要

{analysis.get('decision_summary') or '—'}

## Sections JSON

```json
{sections_json}
```
"""
    note.write_text(content, encoding="utf-8")


def _obsidian_technical_matrix_symbol_dir(vault: Path, symbol: str) -> Path:
    return vault / "TechnicalAnalysis" / "Symbols" / _safe_obsidian_name(symbol)


def _obsidian_technical_matrix_snapshot_path(vault: Path, matrix: dict) -> Path:
    symbol = matrix.get("symbol", "UNKNOWN")
    ts = _safe_obsidian_name((matrix.get("generated_at") or datetime.now().isoformat(timespec="seconds")).replace(":", "-"))
    return _obsidian_technical_matrix_symbol_dir(vault, symbol) / "Snapshots" / f"{ts} 17D矩陣.md"


def _obsidian_technical_matrix_index_path(vault: Path, symbol: str) -> Path:
    return _obsidian_technical_matrix_symbol_dir(vault, symbol) / "技術矩陣入口.md"


def _obsidian_technical_matrix_root_index_path(vault: Path) -> Path:
    return vault / "TechnicalAnalysis" / "技術矩陣總覽.md"


def _technical_matrix_markdown_table(rows: list[dict]) -> str:
    if not rows:
        return "（無）"
    lines = ["| 類型 | 價格 | 來源 | 邏輯 |", "|---|---:|---|---|"]
    for row in rows:
        lines.append(
            f"| {_fmt(row.get('type'))} | {_fmt(row.get('price'))} | "
            f"{_fmt(row.get('dimension') or row.get('source'))} | {_fmt(row.get('logic') or row.get('label'))} |"
        )
    return "\n".join(lines)


def _obsidian_write_technical_matrix(vault: Path, matrix: dict) -> Path:
    """Write one 17D matrix snapshot plus symbol/root indexes."""
    note = _obsidian_technical_matrix_snapshot_path(vault, matrix)
    note.parent.mkdir(parents=True, exist_ok=True)
    symbol = matrix.get("symbol", "UNKNOWN")
    summary = matrix.get("summary") or {}
    plan = matrix.get("execution_plan") or {}
    marker_summary = matrix.get("marker_summary") or {}
    dimensions = matrix.get("dimensions") or []
    confluence = matrix.get("confluence_zones") or []
    interactions = matrix.get("interactions") or []
    matrix_json = json.dumps(sanitize_float_values(matrix), ensure_ascii=False, indent=2)

    dimension_sections = []
    for dim in dimensions:
        observations = "\n".join(f"- {item}" for item in dim.get("observations", [])) or "- （無）"
        signals = "\n".join(
            f"- {sig.get('label')} · {sig.get('direction')} · strength {sig.get('strength')} · {sig.get('evidence', '')}"
            for sig in dim.get("signals", [])
        ) or "- （無）"
        gaps = "\n".join(f"- {gap}" for gap in dim.get("data_gaps", [])) or "- （無）"
        dimension_sections.append(
            f"### {dim.get('name')} ({dim.get('id')})\n\n"
            f"- 狀態：{dim.get('status')}\n"
            f"- 偏向：{dim.get('bias')} / score {dim.get('score')} / confidence {dim.get('confidence')}\n"
            f"- 風險：{dim.get('severity')}\n\n"
            f"觀察：\n{observations}\n\n"
            f"訊號：\n{signals}\n\n"
            f"資料缺口：\n{gaps}\n"
        )

    interaction_lines = "\n".join(
        f"- **{item.get('name')}**：{item.get('status')}。{item.get('logic')}"
        for item in interactions
    ) or "（無）"
    confluence_lines = "\n".join(
        f"- {zone.get('center')} · score {zone.get('score')} · "
        f"{', '.join(sorted({src.get('dimension', '') for src in zone.get('sources', [])}))}"
        for zone in confluence
    ) or "（無）"

    content = f"""---
type: technical-matrix-snapshot
symbol: {_fmt(symbol)}
generated_at: {_fmt(matrix.get('generated_at'))}
source: {_fmt(matrix.get('source'))}
bias: {_fmt(summary.get('bias'))}
net_score: {_fmt(summary.get('net_score'))}
risk_level: {_fmt(summary.get('risk_level'))}
confidence: {_fmt(summary.get('confidence'))}
updated: {date.today().isoformat()}
---

# {symbol} 17D 技術矩陣

## 總結

- 偏向：{summary.get('bias')} / net score {summary.get('net_score')}
- 風險：{summary.get('risk_level')} / risk score {summary.get('risk_score')}
- 信心：{summary.get('confidence')}
- 維度：computed {summary.get('computed_count')} / partial {summary.get('partial_count')} / unavailable {summary.get('unavailable_count')}
- 最新價：{summary.get('latest_price')}
- 標記：{marker_summary.get('total', 0)} 筆，高信念 {len(marker_summary.get('high_conviction', []))} 筆

## 執行計畫

### Entries

{_technical_matrix_markdown_table(plan.get('entries') or [])}

### Stops

{_technical_matrix_markdown_table(plan.get('stops') or [])}

### Targets

{_technical_matrix_markdown_table(plan.get('targets') or [])}

### Risk Notes

{chr(10).join(f"- {item}" for item in plan.get('risk_notes', [])) or "（無）"}

## 交互關聯

{interaction_lines}

## Confluence Zones

{confluence_lines}

## 17 維明細

{chr(10).join(dimension_sections)}

## Matrix JSON

```json
{matrix_json}
```
"""
    note.write_text(content, encoding="utf-8")

    index_path = _obsidian_technical_matrix_index_path(vault, symbol)
    rel_note = note.relative_to(vault).as_posix()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        f"---\n"
        f"type: technical-matrix-symbol-index\n"
        f"symbol: {_fmt(symbol)}\n"
        f"updated: {date.today().isoformat()}\n"
        f"---\n\n"
        f"# {symbol} 技術矩陣入口\n\n"
        f"- 最新快照：[[{rel_note}|{matrix.get('generated_at')} 17D矩陣]]\n"
        f"- 偏向：{summary.get('bias')} / net score {summary.get('net_score')}\n"
        f"- 風險：{summary.get('risk_level')} / confidence {summary.get('confidence')}\n"
        f"- 維度狀態：computed {summary.get('computed_count')} / partial {summary.get('partial_count')} / unavailable {summary.get('unavailable_count')}\n\n"
        f"## 關聯\n\n"
        f"- [[TechnicalAnalysis/17維全景技術分析建置規劃|方法建置規劃]]\n"
        f"- [[TechnicalAnalysis/技術矩陣總覽|技術矩陣總覽]]\n",
        encoding="utf-8",
    )

    root_index = _obsidian_technical_matrix_root_index_path(vault)
    root_index.parent.mkdir(parents=True, exist_ok=True)
    symbol_indexes = sorted((vault / "TechnicalAnalysis" / "Symbols").glob("*/技術矩陣入口.md"))
    links = []
    for path in symbol_indexes:
        rel = path.relative_to(vault).as_posix()
        links.append(f"- [[{rel}|{path.parent.name}]]")
    root_index.write_text(
        f"---\n"
        f"type: technical-matrix-root-index\n"
        f"updated: {date.today().isoformat()}\n"
        f"---\n\n"
        f"# 技術矩陣總覽\n\n"
        f"{chr(10).join(links) or '（無）'}\n",
        encoding="utf-8",
    )
    return note


def _obsidian_delete_analysis(vault: Path, analysis: dict) -> None:
    note = _obsidian_analysis_note_path(vault, analysis)
    if note.exists():
        note.unlink()


def _obsidian_domain_dir(vault: Path, domain: str, ts_value: str) -> Path:
    safe_domain = _safe_obsidian_name(domain)
    safe_ts = _safe_obsidian_name(ts_value.replace(":", "-").replace(" ", "_"))
    return vault / "Research" / safe_domain / safe_ts


def _obsidian_domain_index_paths(vault: Path) -> list[Path]:
    """Return canonical domain research paths without counting latest mirrors twice."""
    research_dir = vault / "Research"
    if not research_dir.is_dir():
        return []

    index_paths: list[Path] = []
    for domain_dir in sorted(p for p in research_dir.iterdir() if p.is_dir()):
        timestamp_indexes = []
        for subdir in sorted(p for p in domain_dir.iterdir() if p.is_dir()):
            for candidate in (
                subdir / f"{domain_dir.name} 研究快照.md",
                subdir / "研究快照.md",
                subdir / "index.md",
            ):
                if candidate.is_file():
                    timestamp_indexes.append(candidate)
                    break
        if timestamp_indexes:
            index_paths.extend(timestamp_indexes)
            continue

        for candidate in (domain_dir / "研究入口.md", domain_dir / "index.md"):
            if candidate.is_file():
                index_paths.append(candidate)
                break
    return index_paths


def _obsidian_parse_position(note_path: Path) -> Optional[dict]:
    """Parse a position note's frontmatter into a dict."""
    try:
        text = note_path.read_text(encoding="utf-8")
        meta, _ = _parse_obsidian_frontmatter(text)
        if meta.get("type") != "position" or not meta.get("symbol"):
            return None
        return {
            "symbol": meta["symbol"],
            "name": meta.get("name", ""),
            "category": meta.get("category", ""),
            "shares": _safe_float(meta.get("shares")),
            "cost_price": _safe_float(meta.get("cost_price")),
            "currency": meta.get("currency", "TWD"),
            "purchase_date": _normalize_purchase_date(meta.get("purchase_date")),
            "target_entry": _safe_float(meta.get("target_entry")),
            "target_profit": _safe_float(meta.get("target_profit")),
            "target_stop": _safe_float(meta.get("target_stop")),
        }
    except Exception:
        return None


def _obsidian_position_fallback(vault: Optional[Path], symbol: str) -> Optional[dict]:
    if not vault or not symbol:
        return None
    note_path = _obsidian_position_note_path(vault, symbol)
    if not note_path.is_file():
        return None
    return _obsidian_parse_position(note_path)


def _obsidian_parse_watchlist(note_path: Path) -> Optional[dict]:
    """Parse a watchlist note's frontmatter into a dict."""
    try:
        text = note_path.read_text(encoding="utf-8")
        meta, body = _parse_obsidian_frontmatter(text)
        if meta.get("type") != "watchlist" or not meta.get("symbol"):
            return None
        # Extract notes section
        notes_section = _extract_md_section(body, "觀察重點")
        notes = notes_section.replace("<!-- 此處可填入觀察重點 -->", "").strip()
        return {
            "symbol": meta["symbol"],
            "name": meta.get("name", ""),
            "category": meta.get("category", ""),
            "currency": meta.get("currency", "TWD"),
            "target_entry": _safe_float(meta.get("target_entry")),
            "target_add": _safe_float(meta.get("target_add")),
            "target_profit": _safe_float(meta.get("target_profit")),
            "target_stop": _safe_float(meta.get("target_stop")),
            "notes": notes,
        }
    except Exception:
        return None


def _obsidian_parse_alert(note_path: Path) -> Optional[dict]:
    try:
        text = note_path.read_text(encoding="utf-8")
        meta, body = _parse_obsidian_frontmatter(text)
        if meta.get("type") != "alert" or not meta.get("symbol") or not meta.get("ts"):
            return None
        return {
            "ts": meta.get("ts", ""),
            "symbol": meta.get("symbol", ""),
            "level": meta.get("level", ""),
            "type": meta.get("alert_type", ""),
            "message": _extract_md_section(body, "訊息") or "",
            "price": _safe_float(meta.get("price")),
            "diagnosis": _extract_md_section(body, "診斷") or "",
            "acknowledged": 1 if str(meta.get("acknowledged", "0")).strip() in ("1", "true", "True", "yes") else 0,
        }
    except Exception:
        return None


def _obsidian_parse_analysis(note_path: Path) -> Optional[dict]:
    try:
        text = note_path.read_text(encoding="utf-8")
        meta, body = _parse_obsidian_frontmatter(text)
        if meta.get("type") != "analysis-result" or not meta.get("symbol") or not meta.get("ts"):
            return None
        sections = _extract_json_codeblock(_extract_md_section(body, "Sections JSON")) or {}
        return {
            "symbol": meta.get("symbol", ""),
            "name": meta.get("name", ""),
            "ts": meta.get("ts", ""),
            "mode": meta.get("mode", ""),
            "provider": meta.get("provider", ""),
            "model": meta.get("model", ""),
            "elapsed": _safe_float(meta.get("elapsed")),
            "decision_summary": _extract_md_section(body, "決策摘要") or meta.get("decision_summary", ""),
            "sections": json.dumps(sections, ensure_ascii=False),
        }
    except Exception:
        return None


def _obsidian_sync_positions(vault: Path) -> dict:
    """Read all position notes from Obsidian and upsert into SQLite."""
    pos_dir = vault / "Portfolio" / "Positions"
    if not pos_dir.is_dir():
        return {"synced": 0, "errors": 0}
    synced = 0
    errors = 0
    seen_symbols = set()
    conn = get_db()
    for note_path in pos_dir.glob("*.md"):
        parsed = _obsidian_parse_position(note_path)
        if not parsed:
            continue
        seen_symbols.add(parsed["symbol"])
        try:
            existing = conn.execute(
                "SELECT id FROM positions WHERE symbol=?", (parsed["symbol"],)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE positions SET name=?, category=?, shares=?, cost_price=?,
                       currency=?, purchase_date=?, target_entry=?, target_profit=?, target_stop=?
                       WHERE symbol=?""",
                    (parsed["name"], parsed["category"], parsed["shares"], parsed["cost_price"],
                     parsed["currency"], parsed["purchase_date"], parsed["target_entry"],
                     parsed["target_profit"], parsed["target_stop"], parsed["symbol"])
                )
            else:
                conn.execute(
                    """INSERT INTO positions (symbol, name, category, shares, cost_price, currency,
                       purchase_date, target_entry, target_profit, target_stop)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (parsed["symbol"], parsed["name"], parsed["category"], parsed["shares"],
                     parsed["cost_price"], parsed["currency"], parsed["purchase_date"],
                     parsed["target_entry"], parsed["target_profit"], parsed["target_stop"])
                )
            synced += 1
        except Exception:
            errors += 1
    existing_symbols = {
        row["symbol"] for row in conn.execute("SELECT symbol FROM positions").fetchall()
    }
    for symbol in existing_symbols - seen_symbols:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()
    return {"synced": synced, "errors": errors}


def _obsidian_sync_watchlist(vault: Path) -> dict:
    """Read all watchlist notes from Obsidian and upsert into SQLite."""
    wl_dir = vault / "Watchlist"
    if not wl_dir.is_dir():
        return {"synced": 0, "errors": 0}
    synced = 0
    errors = 0
    seen_symbols = set()
    conn = get_db()
    for note_path in wl_dir.glob("*.md"):
        if note_path.stem == "index":
            continue
        parsed = _obsidian_parse_watchlist(note_path)
        if not parsed:
            continue
        seen_symbols.add(parsed["symbol"])
        try:
            existing = conn.execute(
                "SELECT id FROM watchlist WHERE symbol=?", (parsed["symbol"],)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE watchlist SET name=?, category=?, currency=?, target_entry=?,
                       target_add=?, target_profit=?, target_stop=?, notes=? WHERE symbol=?""",
                    (parsed["name"], parsed["category"], parsed["currency"], parsed["target_entry"],
                     parsed["target_add"], parsed["target_profit"], parsed["target_stop"],
                     parsed["notes"], parsed["symbol"])
                )
            else:
                conn.execute(
                    """INSERT INTO watchlist (symbol, name, category, currency, target_entry,
                       target_add, target_profit, target_stop, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (parsed["symbol"], parsed["name"], parsed["category"], parsed["currency"],
                     parsed["target_entry"], parsed["target_add"], parsed["target_profit"],
                     parsed["target_stop"], parsed["notes"])
                )
            synced += 1
        except Exception:
            errors += 1
    existing_symbols = {
        row["symbol"] for row in conn.execute("SELECT symbol FROM watchlist").fetchall()
    }
    for symbol in existing_symbols - seen_symbols:
        conn.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()
    return {"synced": synced, "errors": errors}


def _obsidian_sync_alerts(vault: Path) -> dict:
    alert_dir = vault / "Alerts" / "Entries"
    if not alert_dir.is_dir():
        return {"synced": 0, "errors": 0}
    synced = 0
    errors = 0
    seen_keys = set()
    conn = get_db()
    for note_path in alert_dir.rglob("*.md"):
        parsed = _obsidian_parse_alert(note_path)
        if not parsed:
            continue
        key = (parsed["ts"], parsed["symbol"], parsed["type"])
        seen_keys.add(key)
        try:
            existing = conn.execute(
                "SELECT id FROM alerts WHERE ts=? AND symbol=? AND type=?",
                key,
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE alerts
                       SET level=?, message=?, price=?, diagnosis=?, acknowledged=?
                       WHERE ts=? AND symbol=? AND type=?""",
                    (
                        parsed["level"], parsed["message"], parsed["price"],
                        parsed["diagnosis"], parsed["acknowledged"],
                        parsed["ts"], parsed["symbol"], parsed["type"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO alerts
                       (ts, symbol, level, type, message, price, diagnosis, acknowledged)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        parsed["ts"], parsed["symbol"], parsed["level"], parsed["type"],
                        parsed["message"], parsed["price"], parsed["diagnosis"], parsed["acknowledged"],
                    ),
                )
            synced += 1
        except Exception:
            errors += 1
    existing_rows = conn.execute("SELECT id, ts, symbol, type FROM alerts").fetchall()
    for row in existing_rows:
        key = (row["ts"], row["symbol"], row["type"])
        if key not in seen_keys:
            conn.execute("DELETE FROM alerts WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return {"synced": synced, "errors": errors}


def _obsidian_sync_analysis(vault: Path) -> dict:
    analysis_dir = vault / "Analysis"
    if not analysis_dir.is_dir():
        return {"synced": 0, "errors": 0}
    synced = 0
    errors = 0
    seen_keys = set()
    conn = get_db()
    for note_path in analysis_dir.rglob("*.md"):
        parsed = _obsidian_parse_analysis(note_path)
        if not parsed:
            continue
        key = (parsed["symbol"], parsed["ts"])
        seen_keys.add(key)
        try:
            existing = conn.execute(
                "SELECT id FROM analysis_results WHERE symbol=? AND ts=?",
                key,
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE analysis_results
                       SET name=?, mode=?, provider=?, model=?, elapsed=?, decision_summary=?, sections=?
                       WHERE symbol=? AND ts=?""",
                    (
                        parsed["name"], parsed["mode"], parsed["provider"], parsed["model"],
                        parsed["elapsed"], parsed["decision_summary"], parsed["sections"],
                        parsed["symbol"], parsed["ts"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO analysis_results
                       (symbol, name, ts, mode, provider, model, elapsed, decision_summary, sections)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        parsed["symbol"], parsed["name"], parsed["ts"], parsed["mode"],
                        parsed["provider"], parsed["model"], parsed["elapsed"],
                        parsed["decision_summary"], parsed["sections"],
                    ),
                )
            synced += 1
        except Exception:
            errors += 1
    existing_rows = conn.execute("SELECT id, symbol, ts FROM analysis_results").fetchall()
    for row in existing_rows:
        key = (row["symbol"], row["ts"])
        if key not in seen_keys:
            conn.execute("DELETE FROM analysis_results WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return {"synced": synced, "errors": errors}


def _obsidian_sync_domain_research(vault: Path) -> dict:
    research_dir = vault / "Research"
    if not research_dir.is_dir():
        return {"synced": 0, "errors": 0}
    synced = 0
    errors = 0
    seen_paths = set()
    conn = get_db()
    for index_path in _obsidian_domain_index_paths(vault):
        row = {
            "obsidian_path": str(index_path),
            "domain": index_path.parent.parent.name if index_path.parent.parent != research_dir else index_path.parent.name,
            "frontier_stocks": "[]",
            "leading_stocks": "[]",
            "analyst_report": "",
            "reviewer_report": "",
        }
        parsed = _load_domain_research_from_obsidian(row)
        if not parsed or parsed.get("data_source") != "obsidian":
            continue
        obsidian_path = str(index_path)
        seen_paths.add(obsidian_path)
        try:
            existing = conn.execute(
                "SELECT id FROM domain_research WHERE obsidian_path=?",
                (obsidian_path,),
            ).fetchone()
            frontier = json.dumps(parsed.get("frontier_stocks", []), ensure_ascii=False)
            leading = json.dumps(parsed.get("leading_stocks", []), ensure_ascii=False)
            if existing:
                conn.execute(
                    """UPDATE domain_research
                       SET domain=?, ts=?, frontier_stocks=?, leading_stocks=?,
                           analyst_report=?, reviewer_report=?
                       WHERE obsidian_path=?""",
                    (
                        parsed.get("domain", ""), parsed.get("ts", ""),
                        frontier, leading,
                        parsed.get("analyst_report", ""), parsed.get("reviewer_report", ""),
                        obsidian_path,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO domain_research
                       (domain, ts, frontier_stocks, leading_stocks, analyst_report, reviewer_report, obsidian_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        parsed.get("domain", ""), parsed.get("ts", ""),
                        frontier, leading,
                        parsed.get("analyst_report", ""), parsed.get("reviewer_report", ""),
                        obsidian_path,
                    ),
                )
            synced += 1
        except Exception:
            errors += 1
    existing_rows = conn.execute("SELECT id, obsidian_path FROM domain_research").fetchall()
    for row in existing_rows:
        if (row["obsidian_path"] or "") not in seen_paths:
            conn.execute("DELETE FROM domain_research WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    return {"synced": synced, "errors": errors}


def _obsidian_post_write_sync(vault: Optional[Path], kinds: tuple[str, ...] = ("positions", "watchlist")) -> dict:
    """Best-effort sync-back after SQLite -> Obsidian writes."""
    empty = {"synced": 0, "errors": 0}
    if not vault:
        return {"ok": False, **{kind: empty.copy() for kind in kinds}}

    sync_map = {
        "positions": _obsidian_sync_positions,
        "watchlist": _obsidian_sync_watchlist,
        "alerts": _obsidian_sync_alerts,
        "analysis": _obsidian_sync_analysis,
        "domain_research": _obsidian_sync_domain_research,
    }
    result = {"ok": True}
    for kind in kinds:
        func = sync_map.get(kind)
        result[kind] = func(vault) if func else empty.copy()
    if "alerts" in kinds:
        _obsidian_rebuild_alert_views(vault)
    return result


@app.post("/api/obsidian-sync")
def api_obsidian_sync():
    """Obsidian → SQLite：讀取 Obsidian .md，更新資料庫。"""
    vault = _get_vault()
    if not vault:
        raise HTTPException(400, "obsidian_vault_path 未設定或路徑不存在")
    pos_result = _obsidian_sync_positions(vault)
    wl_result = _obsidian_sync_watchlist(vault)
    alert_result = _obsidian_sync_alerts(vault)
    analysis_result = _obsidian_sync_analysis(vault)
    domain_result = _obsidian_sync_domain_research(vault)
    return {
        "ok": True,
        "positions": pos_result,
        "watchlist": wl_result,
        "alerts": alert_result,
        "analysis": analysis_result,
        "domain_research": domain_result,
    }


@app.post("/api/obsidian-export")
def api_obsidian_export():
    """SQLite → Obsidian：將所有 UI 持久化資料匯出為 Obsidian .md。"""
    vault = _get_vault()
    if not vault:
        raise HTTPException(400, "obsidian_vault_path 未設定或路徑不存在")
    conn = get_db()
    positions = [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
    watchlist = [dict(r) for r in conn.execute("SELECT * FROM watchlist").fetchall()]
    alerts = [dict(r) for r in conn.execute(
        "SELECT * FROM alerts ORDER BY ts DESC"
    ).fetchall()]
    analyses = [dict(r) for r in conn.execute("SELECT * FROM analysis_results ORDER BY ts DESC").fetchall()]
    research_rows = [dict(r) for r in conn.execute("SELECT * FROM domain_research ORDER BY ts DESC").fetchall()]
    conn.close()

    for pos in positions:
        _obsidian_write_position(vault, pos)
    _obsidian_write_portfolio_index(vault, positions)

    for watch in watchlist:
        _obsidian_write_watchlist_item(vault, watch)
    _obsidian_write_watchlist_index(vault, watchlist)

    for alert in alerts:
        _obsidian_write_alert(vault, alert)

    for analysis in analyses:
        _obsidian_write_analysis(vault, analysis)

    exported_research = 0
    for row in research_rows:
        try:
            result = {
                "domain": row.get("domain", ""),
                "ts": row.get("ts", ""),
                "summary": row.get("summary", ""),
                "frontier": json.loads(row.get("frontier_stocks") or "[]"),
                "leading": json.loads(row.get("leading_stocks") or "[]"),
                "analyst_report": row.get("analyst_report", ""),
                "reviewer_report": row.get("reviewer_report", ""),
            }
            path = _save_obsidian_notes(row.get("domain", ""), result, str(vault))
            if path:
                exported_research += 1
        except Exception:
            pass

    sync_result = _obsidian_post_write_sync(
        vault,
        kinds=("positions", "watchlist", "alerts", "analysis", "domain_research"),
    )

    return {
        "ok": True,
        "exported": {
            "positions": len(positions),
            "watchlist": len(watchlist),
            "alerts": len(alerts),
            "analysis": len(analyses),
            "domain_research": exported_research,
        },
        "sync": sync_result,
    }


# ─────────────── 領域研究工作流 ───────────────

def _save_obsidian_notes(domain: str, result: dict, vault_path: str) -> str:
    """Save domain research as Obsidian-compatible markdown notes.

    Returns the path of the index file created, or empty string on failure.
    """
    import re
    try:
        vault = Path(vault_path).expanduser()
        safe_domain = re.sub(r'[\\/*?"<>|]', "_", domain)
        ts = result.get("ts", datetime.now().strftime("%Y-%m-%d %H:%M"))
        research_dir = _obsidian_domain_dir(vault, domain, ts)
        latest_dir = vault / "Research" / safe_domain
        frontier_dir = research_dir / "前瞻技術"
        leading_dir = research_dir / "龍頭技術"
        frontier_dir.mkdir(parents=True, exist_ok=True)
        leading_dir.mkdir(parents=True, exist_ok=True)
        latest_dir.mkdir(parents=True, exist_ok=True)

        # Write individual stock notes
        all_frontier_links = []
        all_leading_links = []

        for cat_key, target_dir, links_list in [
            ("frontier", frontier_dir, all_frontier_links),
            ("leading", leading_dir, all_leading_links),
        ]:
            for stock in result.get(cat_key, []):
                sym = stock.get("symbol", "UNKNOWN")
                name = stock.get("name", sym)
                best_fit = stock.get("best_fit") or []
                if isinstance(best_fit, str):
                    best_fit = [best_fit]
                best_fit_yaml = ", ".join(str(x) for x in best_fit)
                safe_sym = re.sub(r'[\\/*?"<>|]', "_", sym)
                note_path = target_dir / f"{safe_sym}.md"
                cat_label = "前瞻技術" if cat_key == "frontier" else "龍頭技術"

                content = f"""---
tags: [research, {safe_domain}, {cat_label}]
symbol: {sym}
name: {name}
analyzed: {ts}
category: {cat_label}
domain: {domain}
best_fit: [{best_fit_yaml}]
---

# {name} ({sym})

> 領域：{domain} | 類別：{cat_label} | 分析日：{ts}

## 投資論點

{stock.get("thesis", "—")}

## 基本面

{stock.get("fundamentals", "—")}

## 新聞與媒體

{stock.get("news", "—")}

## 產業技術

{stock.get("technology", "—")}

## 訂單狀況

{stock.get("orders", "—")}

## 投資時框

### 當沖至週線（1 週）
{stock.get("week_term", "—")}

### 短線（3 個月）
{stock.get("short_term", "—")}

### 中線（1 年）
{stock.get("mid_term", "—")}

### 長線（3 年以上）
{stock.get("long_term", "—")}

## 技術指標（抓取時）
"""
                ind = stock.get("indicators", {})
                if ind:
                    content += f"""
| 指標 | 數值 |
|------|------|
| 現價 | {ind.get("price", "—")} |
| RSI | {ind.get("rsi", "—")} |
| MA20 | {ind.get("ma20", "—")} |
| MA60 | {ind.get("ma60", "—")} |
| Beta | {ind.get("beta", "—")} |
| 52週高/低 | {ind.get("high52", "—")} / {ind.get("low52", "—")} |
"""
                else:
                    content += "\n（未取得即時資料）\n"

                content += f"\n## 連結\n[[Research/{safe_domain}/研究入口|回到 {domain} 總覽]]\n"
                note_path.write_text(content, encoding="utf-8")
                links_list.append(f"[[{cat_label}/{safe_sym}|{name} ({sym})]]")

        # Write snapshot note
        index_path = _obsidian_domain_snapshot_path(Path(vault_path), domain, ts)
        analyst_report = result.get("analyst_report", "")
        reviewer_report = result.get("reviewer_report", "")

        index_content = f"""---
type: domain-research
tags: [research, {safe_domain}]
domain: {domain}
analyzed: {ts}
---

# {domain} — 領域研究總覽

> 分析日：{ts}

{result.get("summary", "")}

## 前瞻技術標的

{chr(10).join("- " + l for l in all_frontier_links) or "（無）"}

## 龍頭技術標的

{chr(10).join("- " + l for l in all_leading_links) or "（無）"}

## 分析師報告

{analyst_report}

## 審查員複核

{reviewer_report}
"""
        index_path.write_text(index_content, encoding="utf-8")
        latest_index = _obsidian_domain_latest_entry_path(Path(vault_path), domain)
        latest_index.write_text(
            f"---\n"
            f"type: domain-research-latest\n"
            f"domain: {domain}\n"
            f"latest_snapshot: {ts}\n"
            f"updated: {date.today().isoformat()}\n"
            f"---\n\n"
            f"# {domain}\n\n"
            f"## 最新研究\n\n"
            f"- [[{ts.replace(':', '-').replace(' ', '_')}/{_safe_obsidian_name(domain)} 研究快照|{ts} 研究快照]]\n\n"
            f"## 歷史快照\n\n"
            f"- [[{ts.replace(':', '-').replace(' ', '_')}/{_safe_obsidian_name(domain)} 研究快照|{ts}]]\n",
            encoding="utf-8",
        )
        return str(index_path)
    except Exception as e:
        return ""


def _parse_obsidian_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a small YAML-like frontmatter block from an Obsidian note."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [x.strip().strip("'\"") for x in value[1:-1].split(",") if x.strip()]
            meta[key] = items
        else:
            meta[key] = value.strip("'\"")
    return meta, parts[2].lstrip()


def _extract_md_section(text: str, title: str) -> str:
    """Extract text under a markdown ##/### heading until the next heading."""
    import re as _re
    pattern = _re.compile(rf"^###?\s+{_re.escape(title)}\s*$", _re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end():]
    next_heading = _re.search(r"^###?\s+", rest, _re.MULTILINE)
    section = rest[:next_heading.start()] if next_heading else rest
    return section.strip()


def _parse_obsidian_indicators(section: str) -> dict:
    indicators = {}
    key_map = {
        "現價": "price",
        "RSI": "rsi",
        "MA20": "ma20",
        "MA60": "ma60",
        "Beta": "beta",
    }
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or cells[0] in ("指標", "------"):
            continue
        if cells[0] == "52週高/低":
            high_low = [x.strip() for x in cells[1].split("/")]
            if len(high_low) == 2:
                indicators["high52"] = _safe_float(high_low[0])
                indicators["low52"] = _safe_float(high_low[1])
            continue
        key = key_map.get(cells[0])
        if key:
            indicators[key] = _safe_float(cells[1])
    return {k: v for k, v in indicators.items() if v is not None}


def _parse_obsidian_stock_note(note_path: Path, cat_key: str, fallback: dict | None = None) -> dict:
    text = note_path.read_text(encoding="utf-8")
    meta, body = _parse_obsidian_frontmatter(text)
    fallback = fallback or {}
    stock = {
        "symbol": meta.get("symbol") or fallback.get("symbol") or note_path.stem,
        "name": meta.get("name") or fallback.get("name") or note_path.stem,
        "thesis": _extract_md_section(body, "投資論點") or fallback.get("thesis", ""),
        "fundamentals": _extract_md_section(body, "基本面") or fallback.get("fundamentals", ""),
        "news": _extract_md_section(body, "新聞與媒體") or fallback.get("news", ""),
        "technology": _extract_md_section(body, "產業技術") or fallback.get("technology", ""),
        "orders": _extract_md_section(body, "訂單狀況") or fallback.get("orders", ""),
        "week_term": _extract_md_section(body, "當沖至週線（1 週）") or fallback.get("week_term", ""),
        "short_term": _extract_md_section(body, "短線（3 個月）") or fallback.get("short_term", ""),
        "mid_term": _extract_md_section(body, "中線（1 年）") or fallback.get("mid_term", ""),
        "long_term": _extract_md_section(body, "長線（3 年以上）") or fallback.get("long_term", ""),
        "best_fit": meta.get("best_fit") or fallback.get("best_fit") or (["short"] if cat_key == "frontier" else ["mid", "long"]),
        "indicators": _parse_obsidian_indicators(_extract_md_section(body, "技術指標（抓取時）"))
            or fallback.get("indicators", {}),
    }
    return sanitize_float_values(stock)


def _obsidian_file_status(path_value: str | None) -> dict:
    if not path_value:
        return {"obsidian_status": "none", "obsidian_error": ""}
    try:
        path = Path(path_value).expanduser()
        if not path.exists():
            return {"obsidian_status": "missing", "obsidian_error": "Obsidian 檔案不存在"}
        if not path.is_file():
            return {"obsidian_status": "invalid", "obsidian_error": "Obsidian 路徑不是檔案"}
        if not path.stat().st_size:
            return {"obsidian_status": "empty", "obsidian_error": "Obsidian 檔案是空的"}
        return {"obsidian_status": "readable", "obsidian_error": ""}
    except Exception as exc:
        return {"obsidian_status": "error", "obsidian_error": str(exc)}


def _load_domain_research_from_obsidian(row: dict) -> dict | None:
    """Load domain research from Obsidian notes, using SQLite only as metadata fallback."""
    obsidian_path = row.get("obsidian_path") or ""
    status = _obsidian_file_status(obsidian_path)
    if status["obsidian_status"] != "readable":
        return None

    try:
        index_path = Path(obsidian_path).expanduser()
        research_dir = index_path.parent
        index_text = index_path.read_text(encoding="utf-8")
        meta, index_body = _parse_obsidian_frontmatter(index_text)

        fallback_frontier = json.loads(row.get("frontier_stocks") or "[]")
        fallback_leading = json.loads(row.get("leading_stocks") or "[]")
        fallback_by_symbol = {
            s.get("symbol"): s
            for s in fallback_frontier + fallback_leading
            if isinstance(s, dict) and s.get("symbol")
        }

        def _load_group(folder: str, cat_key: str) -> list:
            group_dir = research_dir / folder
            if not group_dir.is_dir():
                return fallback_frontier if cat_key == "frontier" else fallback_leading
            stocks = []
            for note_path in sorted(group_dir.glob("*.md")):
                parse_path = note_path
                latest_mirror = research_dir.parent / folder / note_path.name
                if latest_mirror.exists():
                    try:
                        if latest_mirror.stat().st_mtime >= note_path.stat().st_mtime:
                            parse_path = latest_mirror
                    except OSError:
                        pass
                parsed = _parse_obsidian_stock_note(
                    parse_path,
                    cat_key,
                    fallback_by_symbol.get(note_path.stem),
                )
                stocks.append(parsed)
            return stocks

        summary = ""
        marker = "> 分析日"
        if marker in index_body:
            after_marker = index_body.split(marker, 1)[1]
            if "\n" in after_marker:
                after_marker = after_marker.split("\n", 1)[1]
            summary = after_marker.split("## 前瞻技術標的", 1)[0].strip()
        if not summary:
            summary = row.get("summary", "")

        return {
            **row,
            "domain": meta.get("domain") or row.get("domain"),
            "ts": meta.get("analyzed") or row.get("ts"),
            "summary": summary,
            "frontier_stocks": _load_group("前瞻技術", "frontier"),
            "leading_stocks": _load_group("龍頭技術", "leading"),
            "analyst_report": _extract_md_section(index_body, "分析師報告") or row.get("analyst_report", ""),
            "reviewer_report": _extract_md_section(index_body, "審查員複核") or row.get("reviewer_report", ""),
            "obsidian_loaded": True,
            "data_source": "obsidian",
            **status,
        }
    except Exception as exc:
        return {
            **row,
            "obsidian_loaded": False,
            "data_source": "sqlite",
            "obsidian_status": "error",
            "obsidian_error": str(exc),
        }


def _enrich_stocks_with_indicators(stocks: list) -> list:
    """Parallel-fetch yfinance indicators for a list of stocks."""
    import concurrent.futures

    def _fetch(stock):
        sym = stock.get("symbol", "")
        if not sym:
            return stock
        try:
            ind = fetch_indicators(sym)
            stock["indicators"] = sanitize_float_values(ind)
        except Exception:
            stock["indicators"] = {}
        return stock

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(_fetch, stocks))


_DOMAIN_RESEARCH_PROMPT = """You are a senior investment researcher with deep industry-technology expertise and financial analysis skills.
Conduct a thorough, structured deep-dive on the following investment domain. Output should serve directly as an investment memo.

Domain: {domain}

Stock selection criteria:
1. Distinguish "Frontier Technology" (early-stage, high-growth, higher risk) vs "Leading Technology" (established moat, steady compounding)
2. Select 1-5 representative stocks per category. MUST include both Taiwan stocks (.TW) and US stocks in each category — at least 1 Taiwan stock and at least 1 US stock per category. Do not fill a category with only one market.

Research depth required per stock (all text fields in English):

- symbol: ticker (Taiwan format e.g. 2330.TW, US e.g. NVDA)
- name: company name in English
- thesis: core investment thesis — why this company has a unique edge in this domain and the primary reason for entry
- fundamentals: deep fundamental analysis including:
    * Revenue scale and YoY/QoQ growth rate for the past 1-2 years (give specific numbers)
    * Gross margin, operating margin, net margin trends
    * Capital structure: cash position, net debt, capex plans
    * Valuation: P/E, EV/EBITDA, PEG vs historical averages
    * Key highlights or red flags from the most recent earnings call or financial report
- news: recent major events covering:
    * Major contracts, strategic partnerships, or M&A news
    * Analyst upgrades/downgrades and target price changes
    * Important management statements or guidance revisions
    * Potential risk events the market is watching
- technology: in-depth technology analysis including:
    * Core technology description (process node, architecture generation, key materials, IP moat — specific parameters)
    * Quantified technology gap vs main competitors (e.g., yield rate, power consumption, performance figures)
    * Certification barriers, switching costs, sole-source supply positions
    * Technology roadmap: next-gen product specs and planned mass production timeline
- orders: order book and shipment visibility including:
    * Key customer names (be specific where possible; otherwise give proportions or regions)
    * Backlog size or quarterly shipment volume trends
    * ASP trends and product mix upgrades
    * End-market breakdown (data center / EV / consumer electronics etc. by percentage)
    * Visibility for the latest quarter or the next quarter
- week_term: intraday-to-weekly trading view (within 1 week) — recent price momentum, key support/resistance, short-term news catalysts, suitability for day trading or short swing
- short_term: 3-month view — specific catalyst timeline, technical support/resistance, near-term risks
- mid_term: 1-year view — industry cycle position, order visibility, fair value range
- long_term: 3-year+ structural view — industry penetration trend, competitive landscape evolution, potential TAM size
- best_fit: JSON array of suitable trading timeframes for this stock based on its business model, earnings visibility, volatility, and technical setup. Each element must be one of: "week" (day-trade/weekly swing), "short" (3-month), "mid" (1-year), "long" (3-year+). A stock may belong to multiple timeframes. HARD REQUIREMENT: across all stocks in frontier + leading combined, every one of the four timeframes ("week", "short", "mid", "long") must appear at least twice. Adjust assignments or add stocks as needed to satisfy this.

Also provide:
- summary: domain-level investment theme — technology trends, policy environment, cycle positioning, competitive landscape across Taiwan/US/Japan/Korea

Rules:
- Numbers first: give specific percentages or dollar amounts wherever possible — never just say "growing"
- Technology must be precise: never write "industry-leading" without explaining which specific area and by how much
- Flag uncertain or knowledge-cutoff-limited information with "(as of knowledge cutoff)"
- Plain text only — no emoji, icons, or decorative symbols
- No meta-commentary: never write phrases like "In conclusion", "Based on the above", "Revised analysis shows", "Core thesis is", or any language that reveals the analytical process. Present findings directly.
- Use numbered lists (1. 2. 3.) for all enumerated content within each field. Each distinct fact, metric, or event should be its own numbered item. Separate each item with \n (newline character within the JSON string value).
- Output strictly in the JSON format below — no markdown fences (no ```json)

{{
  "summary": "...",
  "frontier": [
    {{
      "symbol": "...",
      "name": "...",
      "thesis": "...",
      "fundamentals": "...",
      "news": "...",
      "technology": "...",
      "orders": "...",
      "week_term": "...",
      "short_term": "...",
      "mid_term": "...",
      "long_term": "...",
      "best_fit": ["short", "mid"]
    }}
  ],
  "leading": [
    {{
      "symbol": "...",
      "name": "...",
      "thesis": "...",
      "fundamentals": "...",
      "news": "...",
      "technology": "...",
      "orders": "...",
      "week_term": "...",
      "short_term": "...",
      "mid_term": "...",
      "long_term": "...",
      "best_fit": ["short", "mid"]
    }}
  ]
}}"""

_DOMAIN_REVIEWER_PROMPT = """You are a strict industry investment reviewer. Your only task is to identify information gaps and insufficient depth in the analyst's draft, and produce a "gap instruction list" for the analyst's second-pass refinement.

Domain: {domain}

[Analyst draft (JSON)]
{analyst_json}

Review each stock one by one and flag gaps or shallow content in the following areas (plain text, bullet list, no scoring or praise):

1. fundamentals field: missing specific financial figures? Growth rates, margins, valuation — are they quantified? Is the earnings call content cited?
2. technology field: does the description stay at a surface level? Is the gap vs competitors quantified? Are process/architecture/IP specifics concrete?
3. orders field: are customer names too vague? Is backlog size given with numbers? Is ASP trend explained?
4. news field: are there specific events with dates? Are analyst ratings cited?
5. Stock selection completeness: are there obvious missing names in this domain? Is the Frontier vs Leading classification well-reasoned?
6. Time horizons: do short/mid/long views have specific catalysts or numbers, or just directional statements?

Output format (plain text bullet list, in English):
[symbol]
- Gap 1: ...
- Gap 2: ...

[Overall]
- Gap/suggestion: ..."""

_DOMAIN_ANALYST_REFINEMENT_PROMPT = """You are a senior investment researcher doing a second-pass deep revision of your draft.

Domain: {domain}

[Your draft (JSON)]
{analyst_json}

[Reviewer gap instructions]
{reviewer_gaps}

Tasks:
1. Address every gap instruction from the reviewer — add more specific, deeper content to the relevant fields
2. Fields not flagged can also be improved opportunistically, but all flagged fields must show substantive improvement
3. Numbers first: quantify wherever possible; flag uncertainty with "(as of knowledge cutoff)"
4. Technology must be precise: "industry-leading" is not acceptable — state which specific area and by how much
5. Output the exact same JSON structure as the draft (same fields), no additions or removals
6. Plain text only — no emoji, icons, or decorative symbols
7. No meta-commentary: never write phrases like "In conclusion", "Based on the above", "Revised analysis shows". Present findings directly.
8. Use numbered lists (1. 2. 3.) for all enumerated content within each field. Each distinct fact, metric, or event should be its own numbered item.
9. Output only JSON — no markdown fences (no ```json)"""

_DOMAIN_TRANSLATION_PROMPT = """你是專業金融翻譯員，擅長投資研究報告的繁體中文翻譯。

請將以下英文投資研究 JSON 翻譯成繁體中文。

翻譯規則：
1. 只翻譯文字欄位的內容（string values）
2. 以下內容保持原文不翻譯：股票代碼（symbol）、公司名稱（name）、best_fit 欄位（陣列結構與其中的 "week"/"short"/"mid"/"long" 字串值全部保留原始英文不翻）、所有數字、百分比、日期、英文專有名詞（如 TSMC、NVIDIA、backlog、ASP、EV/EBITDA 等技術/財務術語）
3. 保持 JSON 結構完全不變（所有 key 名稱維持英文），不得增減欄位
4. 翻譯要自然流暢，符合台灣投資圈慣用語
5. 純文字，不使用 emoji、icon 或裝飾性符號
6. 不得加入「綜上所述」「修訂後」「核心結論是」「基於以上分析」等透露分析過程的贅詞，直接呈現內容
7. 原文中的編號列表（1. 2. 3.）翻譯後必須保留編號，每個編號項目之間以 \n 換行（即 JSON 字串中的換行符號）
8. 嚴格只輸出 JSON，不含任何 markdown 包裹符號（不要 ```json）

[英文 JSON]
{english_json}"""


class DomainResearchRequest(BaseModel):
    domain: str


def _parse_analyst_json(raw: str):
    """Parse JSON from analyst LLM output, stripping markdown fences if present."""
    import re as _re
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        clean = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
    try:
        return json.loads(clean)
    except Exception:
        m = _re.search(r'\{[\s\S]*\}', clean)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


@app.get("/api/domain-research-stream")
def api_domain_research_stream(domain: str):
    """SSE 串流端點：五步驟工作流
    1. 分析師初稿 → 2. yfinance 指標補強 → 3. 審查員缺口清單（內部）→ 4. 分析師深化 → 5. 儲存
    """
    import time as _time

    def event_stream():
        def emit(event_type: str, data: dict):
            payload = json.dumps(data, ensure_ascii=False)
            return f"event: {event_type}\ndata: {payload}\n\n"

        t0 = _time.time()
        settings = load_settings()
        keys = settings["api_keys"]
        roles = settings["roles"]
        analyst = roles["analyst"]
        reviewer = roles["reviewer"]
        vault_path = settings.get("obsidian_vault_path", "")

        yield emit("started", {"domain": domain})

        # ── Step 1: 分析師初稿 ──
        yield emit("progress", {
            "step": 1, "total": 6,
            "message": f"分析師（{analyst['provider']}/{analyst['model']}）正在撰寫「{domain}」領域初稿...",
            "elapsed": round(_time.time() - t0, 1),
        })

        analyst_prompt = _DOMAIN_RESEARCH_PROMPT.format(domain=domain)
        try:
            raw_pass1 = call_llm(
                analyst["provider"], analyst["model"],
                analyst_prompt,
                keys.get(analyst["provider"], ""),
                mode=analyst.get("mode", "api"),
                timeout=600,
            )
        except Exception as e:
            yield emit("error", {"error": f"分析師初稿失敗: {e}"})
            return

        analyst_data = _parse_analyst_json(raw_pass1)
        if analyst_data is None:
            yield emit("error", {"error": "分析師初稿無法解析為 JSON，請重試"})
            return

        frontier_stocks = analyst_data.get("frontier", [])
        leading_stocks = analyst_data.get("leading", [])
        summary = analyst_data.get("summary", "")

        yield emit("analyst_done", {
            "summary": summary,
            "frontier_count": len(frontier_stocks),
            "leading_count": len(leading_stocks),
            "elapsed": round(_time.time() - t0, 1),
        })

        # ── Step 2: yfinance 指標補強 ──
        all_stocks = frontier_stocks + leading_stocks
        yield emit("progress", {
            "step": 2, "total": 6,
            "message": f"正在抓取 {len(all_stocks)} 檔個股即時指標（yfinance）...",
            "elapsed": round(_time.time() - t0, 1),
        })

        enriched_frontier = _enrich_stocks_with_indicators(frontier_stocks)
        enriched_leading = _enrich_stocks_with_indicators(leading_stocks)

        yield emit("indicators_done", {
            "message": "技術指標補強完成",
            "elapsed": round(_time.time() - t0, 1),
        })

        # ── Step 3: 審查員產出缺口清單（英文，不顯示於 UI，僅作為回滾指令） ──
        yield emit("progress", {
            "step": 3, "total": 6,
            "message": f"審查員（{reviewer['provider']}/{reviewer['model']}）審查初稿缺口...",
            "elapsed": round(_time.time() - t0, 1),
        })

        reviewer_prompt = _DOMAIN_REVIEWER_PROMPT.format(
            domain=domain,
            analyst_json=json.dumps(analyst_data, ensure_ascii=False, indent=2)[:4000],
        )
        try:
            reviewer_gaps = call_llm(
                reviewer["provider"], reviewer["model"],
                reviewer_prompt,
                keys.get(reviewer["provider"], ""),
                mode=reviewer.get("mode", "api"),
                timeout=300,
            )
        except Exception as e:
            # Reviewer failure is non-fatal — skip pass 2 and use pass 1 output
            reviewer_gaps = ""

        # ── Step 4: 分析師深化（Pass 2，英文，以審查員缺口清單為指引） ──
        yield emit("progress", {
            "step": 4, "total": 6,
            "message": f"分析師（{analyst['provider']}/{analyst['model']}）正在深化補強分析（英文）...",
            "elapsed": round(_time.time() - t0, 1),
        })

        final_analyst_data = analyst_data  # fallback
        raw_pass2 = ""
        if reviewer_gaps:
            refinement_prompt = _DOMAIN_ANALYST_REFINEMENT_PROMPT.format(
                domain=domain,
                analyst_json=json.dumps(analyst_data, ensure_ascii=False, indent=2)[:4000],
                reviewer_gaps=reviewer_gaps[:2000],
            )
            try:
                raw_pass2 = call_llm(
                    analyst["provider"], analyst["model"],
                    refinement_prompt,
                    keys.get(analyst["provider"], ""),
                    mode=analyst.get("mode", "api"),
                    timeout=600,
                )
                parsed2 = _parse_analyst_json(raw_pass2)
                if parsed2 is not None:
                    final_analyst_data = parsed2
                    enriched_frontier = _enrich_stocks_with_indicators(
                        final_analyst_data.get("frontier", enriched_frontier)
                    )
                    enriched_leading = _enrich_stocks_with_indicators(
                        final_analyst_data.get("leading", enriched_leading)
                    )
                    summary = final_analyst_data.get("summary", summary)
            except Exception:
                pass  # keep pass 1 output

        # ── Step 5: 翻譯成繁體中文 ──
        yield emit("progress", {
            "step": 5, "total": 6,
            "message": f"翻譯成繁體中文（{analyst['provider']}/{analyst['model']}）...",
            "elapsed": round(_time.time() - t0, 1),
        })

        # Strip indicators before translation (they're numbers, not text — reduce payload size)
        def _strip_ind(stocks):
            return [{k: v for k, v in s.items() if k != "indicators"} for s in stocks]

        def _merge_ind(translated_stocks, original_stocks):
            """Re-attach indicators from original after translation."""
            orig_by_sym = {s.get("symbol", ""): s.get("indicators", {}) for s in original_stocks}
            for s in translated_stocks:
                s["indicators"] = orig_by_sym.get(s.get("symbol", ""), {})
            return translated_stocks

        translation_payload = {
            "summary": summary,
            "frontier": _strip_ind(enriched_frontier),
            "leading": _strip_ind(enriched_leading),
        }
        english_json_str = json.dumps(translation_payload, ensure_ascii=False, indent=2)
        translation_prompt = _DOMAIN_TRANSLATION_PROMPT.format(
            english_json=english_json_str  # no truncation
        )
        try:
            raw_translated = call_llm(
                analyst["provider"], analyst["model"],
                translation_prompt,
                keys.get(analyst["provider"], ""),
                mode=analyst.get("mode", "api"),
                timeout=600,
            )
            translated_data = _parse_analyst_json(raw_translated)
            if translated_data is not None:
                summary = translated_data.get("summary", summary)
                trans_f = translated_data.get("frontier", [])
                trans_l = translated_data.get("leading", [])
                if trans_f:
                    enriched_frontier = _merge_ind(trans_f, enriched_frontier)
                if trans_l:
                    enriched_leading = _merge_ind(trans_l, enriched_leading)
        except Exception:
            pass  # keep English output if translation fails

        # ── Step 6: 存 DB + Obsidian ──
        yield emit("progress", {
            "step": 6, "total": 6,
            "message": "儲存研究結果...",
            "elapsed": round(_time.time() - t0, 1),
        })

        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M")
        result = {
            "domain": domain,
            "ts": ts_now,
            "summary": summary,
            "frontier": enriched_frontier,
            "leading": enriched_leading,
            "analyst_report": raw_pass2 or raw_pass1,
            "reviewer_report": reviewer_gaps,  # stored for audit, not shown in UI
        }

        obsidian_path = ""
        if vault_path:
            obsidian_path = _save_obsidian_notes(domain, result, vault_path)

        conn = get_db()
        conn.execute(
            """INSERT INTO domain_research
               (domain, ts, frontier_stocks, leading_stocks, analyst_report, reviewer_report, obsidian_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                domain, ts_now,
                json.dumps(enriched_frontier, ensure_ascii=False),
                json.dumps(enriched_leading, ensure_ascii=False),
                raw_pass2 or raw_pass1, reviewer_gaps, obsidian_path,
            )
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        if vault_path:
            _obsidian_post_write_sync(_get_vault(), kinds=("domain_research",))
        conn.close()

        yield emit("done", {
            "id": rid,
            "domain": domain,
            "ts": ts_now,
            "summary": summary,
            "frontier": enriched_frontier,
            "leading": enriched_leading,
            "obsidian_path": obsidian_path,
            "elapsed": round(_time.time() - t0, 1),
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/domain-research")
def api_list_domain_research(limit: int = 20):
    """列出最近的領域研究記錄。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, domain, ts, obsidian_path FROM domain_research ORDER BY ts DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    records = []
    for r in rows:
        d = dict(r)
        d.update(_obsidian_file_status(d.get("obsidian_path")))
        records.append(d)
    return {"records": records}


@app.get("/api/domain-research/{rid}")
def api_get_domain_research(rid: int):
    """取得單筆領域研究詳情。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM domain_research WHERE id=?", (rid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    d = dict(row)
    obsidian_data = _load_domain_research_from_obsidian(d)
    if obsidian_data:
        if obsidian_data.get("data_source") == "obsidian":
            return sanitize_float_values(obsidian_data)
        d.update({
            "obsidian_loaded": False,
            "data_source": "sqlite",
            "obsidian_status": obsidian_data.get("obsidian_status", "error"),
            "obsidian_error": obsidian_data.get("obsidian_error", ""),
        })
    else:
        d.update({
            "obsidian_loaded": False,
            "data_source": "sqlite",
            **_obsidian_file_status(d.get("obsidian_path")),
        })
    try:
        d["frontier_stocks"] = json.loads(d["frontier_stocks"] or "[]")
        d["leading_stocks"] = json.loads(d["leading_stocks"] or "[]")
    except Exception:
        pass
    return sanitize_float_values(d)


@app.delete("/api/domain-research/{rid}")
def api_delete_domain_research(rid: int):
    """刪除單筆領域研究。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM domain_research WHERE id=?", (rid,)).fetchone()
    conn.execute("DELETE FROM domain_research WHERE id=?", (rid,))
    conn.commit()
    vault = _get_vault()
    if vault and row and row["obsidian_path"]:
        index_path = Path(row["obsidian_path"]).expanduser()
        research_dir = index_path.parent
        if research_dir.exists():
            for child in sorted(research_dir.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            try:
                research_dir.rmdir()
            except OSError:
                pass
        _obsidian_post_write_sync(vault, kinds=("domain_research",))
    conn.close()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="127.0.0.1", port=6500)
