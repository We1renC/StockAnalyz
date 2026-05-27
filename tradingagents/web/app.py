#!/usr/bin/env python3
"""TradingAgents Portfolio Dashboard — FastAPI single-file app."""

import asyncio
import json
import math
import sqlite3
import ssl
import warnings
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    """)
    conn.commit()

    # Migration for existing databases to add target columns to positions table
    c = conn.cursor()
    for col in ("target_entry", "target_profit", "target_stop"):
        try:
            c.execute(f"ALTER TABLE positions ADD COLUMN {col} REAL")
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

def fetch_yfinance_indicators(symbol: str, bench_close=None) -> dict:
    """Full indicator source backed by Yahoo Finance history."""
    try:
        h = yf.Ticker(symbol).history(period="1y")
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
    """
    yf_ind = fetch_yfinance_indicators(symbol, bench_close) or {}
    if not yf_ind and _twse_channel(symbol):
        yf_ind = fetch_official_tw_indicators(symbol, bench_close) or {}

    # 用 yfinance info.regularMarketPrice 補充盤中即時價
    if yf_ind:
        try:
            info = yf.Ticker(symbol).info or {}
            rmp = info.get("regularMarketPrice") or info.get("currentPrice")
            if rmp and rmp > 0:
                yf_ind["_yf_realtime"] = float(rmp)
        except Exception:
            pass

    if _twse_channel(symbol):
        official = fetch_tw_realtime_quote(symbol)
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

    yf_ind.pop("_yf_realtime", None)
    return yf_ind

def store_price_cache(c, symbol: str, ind: dict):
    """Persist fresh quote data while keeping older indicator fields when absent."""
    ind = sanitize_float_values(ind)
    existing = c.execute("SELECT * FROM price_cache WHERE symbol=?", (symbol,)).fetchone()
    existing_data = json.loads(existing["data"] or "{}") if existing and existing["data"] else {}
    merged = dict(existing_data)
    for key, value in ind.items():
        if value is not None:
            merged[key] = value
    merged["source"] = ind.get("source", merged.get("source"))
    merged = sanitize_float_values(merged)

    c.execute(
        """INSERT OR REPLACE INTO price_cache
           (symbol, ts, price, rsi, ma20, ma60, high52, low52, change_1d, change_1m, beta, data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            json.dumps(merged),
        ),
    )

def get_market_state():
    """Pull VIX, TWII, SPX state."""
    try:
        vix = yf.Ticker("^VIX").history(period="1mo")["Close"]
        twii = yf.Ticker("^TWII").history(period="6mo")["Close"]
        spx = yf.Ticker("^GSPC").history(period="6mo")["Close"]

        vix_val = float(vix.iloc[-1])
        twii_val = float(twii.iloc[-1])
        twii_ma60 = float(twii.rolling(60).mean().iloc[-1]) if len(twii) >= 60 else twii_val
        spx_val = float(spx.iloc[-1])
        spx_ma60 = float(spx.rolling(60).mean().iloc[-1]) if len(spx) >= 60 else spx_val

        warnings = 0
        if vix_val > 25: warnings += 1
        if twii_val < twii_ma60: warnings += 1
        if spx_val < spx_ma60: warnings += 1

        if warnings >= 3:
            level = "danger"
        elif warnings >= 1:
            level = "warning"
        else:
            level = "safe"

        return {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "vix": round(vix_val, 2),
            "twii": round(twii_val, 0),
            "twii_ma60": round(twii_ma60, 0),
            "spx": round(spx_val, 2),
            "spx_ma60": round(spx_ma60, 2),
            "risk_level": level,
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
        c.execute(
            "INSERT INTO alerts (ts, symbol, level, type, message, price, diagnosis) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                symbol,
                a["level"],
                a["type"],
                a["message"],
                a["price"],
                diag_text,
            ),
        )
        created += 1
    return created

# ─────────────── Background Monitor ───────────────
async def monitor_loop():
    """Background task: refresh prices + evaluate alerts every 5 minutes."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    while True:
        try:
            t0 = datetime.now()
            print(f"[{t0:%H:%M:%S}] Monitor cycle started...")

            # ── Phase 1: 平行抓大盤指數 + 市場狀態 ──
            with ThreadPoolExecutor(max_workers=3) as ex:
                f_twii = ex.submit(fetch_benchmark_close, "^TWII")
                f_spx = ex.submit(fetch_benchmark_close, "^GSPC")
                f_market = ex.submit(get_market_state)
                twii = f_twii.result()
                spx = f_spx.result()
                market = f_market.result()

            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO market_state (id, ts, vix, twii, twii_ma60, spx, spx_ma60, risk_level, warnings_count) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (market.get("ts"), market.get("vix"), market.get("twii"), market.get("twii_ma60"),
                       market.get("spx"), market.get("spx_ma60"), market.get("risk_level"), market.get("warnings_count")))

            # ── Phase 2: 收集所有需抓價格的標的 ──
            positions = [dict(row) for row in c.execute("SELECT * FROM positions").fetchall()]
            watchlist = [dict(row) for row in c.execute("SELECT * FROM watchlist").fetchall()]

            # 去重：同一個 symbol 只抓一次
            all_symbols = {}
            for d in positions:
                all_symbols[d["symbol"]] = {"bench": twii if ".TW" in d["symbol"] else spx}
            for d in watchlist:
                all_symbols.setdefault(d["symbol"], {"bench": twii if ".TW" in d["symbol"] else spx})

            # ── Phase 3: 平行抓所有標的的即時指標 ──
            indicators = {}  # symbol -> ind dict

            def _fetch_one(symbol, bench):
                return symbol, fetch_indicators(symbol, bench)

            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {
                    ex.submit(_fetch_one, sym, info["bench"]): sym
                    for sym, info in all_symbols.items()
                }
                for future in as_completed(futures):
                    try:
                        sym, ind = future.result()
                        if ind and "price" in ind:
                            indicators[sym] = ind
                    except Exception as e:
                        sym = futures[future]
                        print(f"  [WARN] {sym} fetch failed: {e}")

            # ── Phase 4: 寫入快取 + 產生警報（循序寫 DB） ──
            for d in positions:
                ind = indicators.get(d["symbol"])
                if ind:
                    store_price_cache(c, d["symbol"], ind)
                    created = insert_alerts(c, d["symbol"], d["name"], ind, market, position=d)
                    if created:
                        print(f"  [ALERT] {d['symbol']}: {created} new alert(s)")

            for d in watchlist:
                ind = indicators.get(d["symbol"])
                if ind:
                    store_price_cache(c, d["symbol"], ind)
                    created = insert_alerts(c, d["symbol"], d["name"], ind, market, watch=d)
                    if created:
                        print(f"  [ALERT] {d['symbol']}: {created} new alert(s)")

            conn.commit()
            conn.close()
            elapsed = (datetime.now() - t0).total_seconds()
            print(f"[{datetime.now():%H:%M:%S}] Monitor cycle done. ({elapsed:.1f}s, {len(indicators)}/{len(all_symbols)} symbols)")
        except Exception as e:
            print(f"Monitor error: {e}")

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
    items = [_run_backtest_for_position(dict(r), months=months) for r in rows]
    valid = [x for x in items if not x.get("error")]
    total_position_pnl = sum(x["position_pnl"] for x in valid)
    total_buy_hold_pnl = sum(x["buy_hold_pnl"] for x in valid)
    avg_return = sum(x["period_return_pct"] for x in valid) / len(valid) if valid else 0
    return {
        "months": months,
        "items": items,
        "summary": {
            "valid_count": len(valid),
            "total_position_pnl": round(total_position_pnl, 0),
            "total_buy_hold_pnl": round(total_buy_hold_pnl, 0),
            "avg_return_pct": round(avg_return, 2),
        },
    }


@app.get("/api/portfolio")
def api_portfolio():
    conn = get_db()
    rows = conn.execute("""
        SELECT p.*, pc.price as current_price, pc.rsi, pc.change_1d, pc.beta, pc.ma20, pc.high52
        FROM positions p
        LEFT JOIN price_cache pc ON p.symbol = pc.symbol
    """).fetchall()
    market_row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
    conn.close()
    market = dict(market_row) if market_row else None

    out = []
    total_cost = total_value = 0
    for r in rows:
        d = dict(r)
        cur = d.get("current_price") or d["cost_price"]
        cost_total = d["cost_price"] * d["shares"]
        val_total = cur * d["shares"]
        if d["currency"] == "USD":
            cost_total *= USD_TWD
            val_total *= USD_TWD
        d["pnl"] = round(val_total - cost_total, 0)
        d["pnl_pct"] = round((cur/d["cost_price"]-1)*100, 2)
        d["market_value"] = round(val_total, 0)
        d["cost_total"] = round(cost_total, 0)
        d["recommendation"] = _recommend_position(d, market)
        total_cost += cost_total
        total_value += val_total
        out.append(d)
    return sanitize_float_values({
        "positions": out,
        "summary": {
            "total_cost": round(total_cost, 0),
            "total_value": round(total_value, 0),
            "total_pnl": round(total_value - total_cost, 0),
            "total_pnl_pct": round((total_value/total_cost-1)*100, 2) if total_cost > 0 else 0,
        }
    })


@app.get("/api/portfolio/trend")
def api_portfolio_trend():
    conn = get_db()
    rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.close()
    
    positions = [dict(r) for r in rows]
    if not positions:
        return {"tw": [], "us": []}
        
    from concurrent.futures import ThreadPoolExecutor
    
    def _fetch_history(symbol):
        try:
            h = yf.Ticker(symbol).history(period="1wk", interval="15m")
            h = h.dropna(subset=["Close"])
            prices = {int(ts.timestamp()): float(close) for ts, close in h["Close"].items()}
            return symbol, prices
        except Exception:
            return symbol, {}
            
    histories = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_fetch_history, p["symbol"]) for p in positions]
        for f in futures:
            sym, prices = f.result()
            histories[sym] = prices
            
    all_timestamps = set()
    for prices in histories.values():
        all_timestamps.update(prices.keys())
        
    if not all_timestamps:
        return {"tw": [], "us": []}
        
    sorted_timestamps = sorted(list(all_timestamps))
    
    last_seen_prices = {}
    for p in positions:
        sym = p["symbol"]
        sym_prices = histories.get(sym, {})
        if sym_prices:
            first_ts = min(sym_prices.keys())
            last_seen_prices[sym] = sym_prices[first_ts]
        else:
            last_seen_prices[sym] = p["cost_price"]
            
    import datetime
    
    # 解析各持倉的購買日期
    purchase_dates = {}
    for p in positions:
        p_date_str = p.get("purchase_date")
        if p_date_str:
            try:
                purchase_dates[p["id"]] = datetime.date.fromisoformat(p_date_str.strip())
            except Exception:
                purchase_dates[p["id"]] = None
        else:
            purchase_dates[p["id"]] = None
            
    trend_tw = []
    trend_us = []
    for ts in sorted_timestamps:
        # 將時間戳記轉為本地日期做比較
        ts_date = datetime.datetime.fromtimestamp(ts).date()
        
        tw_cost = tw_val = 0.0
        us_cost = us_val = 0.0
        tw_active = us_active = False
        
        for p in positions:
            p_date = purchase_dates.get(p["id"])
            # 若該時間點尚未購買此股票，則不計入持倉成本與市值
            if p_date and ts_date < p_date:
                continue
                
            sym = p["symbol"]
            sym_prices = histories.get(sym, {})
            if ts in sym_prices:
                last_seen_prices[sym] = sym_prices[ts]
                
            price = last_seen_prices[sym]
            cost_total = p["cost_price"] * p["shares"]
            val_total = price * p["shares"]
            
            is_tw = p["currency"] == "TWD" or p["symbol"].endswith(".TW")
            if is_tw:
                tw_cost += cost_total
                tw_val += val_total
                tw_active = True
            else:
                us_cost += cost_total
                us_val += val_total
                us_active = True
                
        if tw_active:
            trend_tw.append({
                "time": ts,
                "total_value": round(tw_val, 0),
                "total_cost": round(tw_cost, 0),
            })
        if us_active:
            trend_us.append({
                "time": ts,
                "total_value": round(us_val, 2),
                "total_cost": round(us_cost, 2),
            })
        
    return sanitize_float_values({
        "tw": trend_tw,
        "us": trend_us
    })


@app.get("/api/watchlist")
def api_watchlist():
    conn = get_db()
    watchlist_rows = conn.execute("""
        SELECT w.*, pc.price as current_price, pc.rsi, pc.change_1d, pc.ma20, pc.ma60, pc.beta
        FROM watchlist w
        LEFT JOIN price_cache pc ON w.symbol = pc.symbol
    """).fetchall()
    positions_rows = conn.execute("""
        SELECT p.id, p.symbol, p.name, p.category, p.currency, p.target_entry, p.target_profit, p.target_stop,
               pc.price as current_price, pc.rsi, pc.change_1d, pc.ma20, pc.ma60, pc.beta
        FROM positions p
        LEFT JOIN price_cache pc ON p.symbol = pc.symbol
    """).fetchall()
    conn.close()

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
    for d in out_map.values():
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
    conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (req.id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/refresh")
async def api_refresh():
    """Manually refresh prices and evaluate alerts immediately (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time
    t0 = _time.time()

    # Phase 1: 平行抓大盤
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_twii = ex.submit(fetch_benchmark_close, "^TWII")
        f_spx = ex.submit(fetch_benchmark_close, "^GSPC")
        f_market = ex.submit(get_market_state)
        twii = f_twii.result()
        spx = f_spx.result()
        market = f_market.result()

    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO market_state (id, ts, vix, twii, twii_ma60, spx, spx_ma60, risk_level, warnings_count) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
              (market.get("ts"), market.get("vix"), market.get("twii"), market.get("twii_ma60"),
               market.get("spx"), market.get("spx_ma60"), market.get("risk_level"), market.get("warnings_count")))

    # Phase 2: 收集標的 + 去重
    positions = [dict(row) for row in c.execute("SELECT * FROM positions").fetchall()]
    watchlist_rows = [dict(row) for row in c.execute("SELECT * FROM watchlist").fetchall()]

    all_symbols = {}
    for d in positions:
        all_symbols[d["symbol"]] = {"bench": twii if ".TW" in d["symbol"] else spx}
    for d in watchlist_rows:
        all_symbols.setdefault(d["symbol"], {"bench": twii if ".TW" in d["symbol"] else spx})

    # Phase 3: 平行抓報價
    indicators = {}
    def _fetch_one(symbol, bench):
        return symbol, fetch_indicators(symbol, bench)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_one, sym, info["bench"]): sym for sym, info in all_symbols.items()}
        for future in as_completed(futures):
            try:
                sym, ind = future.result()
                if ind and "price" in ind:
                    indicators[sym] = ind
            except Exception:
                pass

    # Phase 4: 寫入 DB
    refreshed = 0
    alerts_created = 0
    for d in positions:
        ind = indicators.get(d["symbol"])
        if ind:
            store_price_cache(c, d["symbol"], ind)
            refreshed += 1
            alerts_created += insert_alerts(c, d["symbol"], d["name"], ind, market, position=d)
    for d in watchlist_rows:
        ind = indicators.get(d["symbol"])
        if ind:
            store_price_cache(c, d["symbol"], ind)
            refreshed += 1
            alerts_created += insert_alerts(c, d["symbol"], d["name"], ind, market, watch=d)

    conn.commit()
    conn.close()
    elapsed = round(_time.time() - t0, 1)
    return {"refreshed": refreshed, "alerts_created": alerts_created, "market": market, "elapsed_seconds": elapsed}

@app.get("/api/history/{symbol}")
def api_history(symbol: str, period: str = "6mo"):
    """歷史 OHLC 數據 (給 K 線圖使用)。"""
    try:
        h = yf.Ticker(symbol).history(period=period)
        h = h.dropna(subset=["Open", "High", "Low", "Close"])
        if len(h) == 0:
            return {"error": "no data"}
        h.index = h.index.tz_localize(None)
        candles = []
        volumes = []
        for ts, row in h.iterrows():
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
        # MA 線
        close = h["Close"]
        ma20 = []
        ma60 = []
        ma20_series = close.rolling(20).mean()
        ma60_series = close.rolling(60).mean()
        for ts, val in ma20_series.dropna().items():
            ma20.append({"time": int(ts.timestamp()), "value": round(float(val), 2)})
        for ts, val in ma60_series.dropna().items():
            ma60.append({"time": int(ts.timestamp()), "value": round(float(val), 2)})
        return sanitize_float_values({"symbol": symbol, "candles": candles, "volumes": volumes, "ma20": ma20, "ma60": ma60})
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
    p_date = p.purchase_date or date.today().isoformat()
    conn.execute(
        "INSERT INTO positions (symbol, name, category, shares, cost_price, currency, purchase_date, target_entry, target_profit, target_stop) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p.symbol, p.name, p.category, p.shares, p.cost_price, p.currency, p_date, p.target_entry, p.target_profit, p.target_stop)
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/positions/{pid}")
def api_del_position(pid: int):
    conn = get_db()
    conn.execute("DELETE FROM positions WHERE id=?", (pid,))
    conn.commit()
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
    
    set_fields = p.dict(exclude_unset=True)
    for field in ("shares", "cost_price", "name", "category", "currency", "purchase_date", "target_entry", "target_profit", "target_stop"):
        if field in set_fields:
            updates.append(f"{field}=?")
            params.append(set_fields[field])
            
    if updates:
        params.append(pid)
        conn.execute(f"UPDATE positions SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
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
    conn.close()
    return {"ok": True}

@app.delete("/api/watchlist/{wid}")
def api_del_watch(wid: int):
    conn = get_db()
    conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    conn.commit()
    conn.close()
    return {"ok": True}

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
def api_clear_alerts(days: Optional[int] = None):
    """清除警報。days=None 全清，否則清除 N 天前的。"""
    conn = get_db()
    if days is None:
        conn.execute("DELETE FROM alerts")
    else:
        conn.execute("DELETE FROM alerts WHERE date(ts) < date('now', ?)", (f"-{days} days",))
    conn.commit()
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
    conn.execute("DELETE FROM alerts WHERE id=?", (aid,))
    conn.commit()
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
                
    conn.commit()
    conn.close()
    return {"ok": True}

# ─────────────── 設定 API (LLM Keys + Roles) ───────────────
@app.get("/api/settings")
def api_get_settings():
    """回傳設定，API key 用 mask 格式。"""
    s = load_settings()
    return {
        "api_keys_masked": {k: mask_key(v) for k, v in s["api_keys"].items()},
        "api_keys_set": {k: bool(v) for k, v in s["api_keys"].items()},
        "roles": s["roles"],
        "available_models": AVAILABLE_MODELS,
        "cli_status": detect_cli_availability(),
    }

class SettingsUpdate(BaseModel):
    api_keys: Optional[dict] = None  # {anthropic, openai, google} - 空字串 = 不更新
    roles: Optional[dict] = None     # {analyst: {provider, model}, reviewer: {...}}

@app.post("/api/settings")
def api_save_settings(req: SettingsUpdate):
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
    save_settings(s)
    return {"ok": True}

# ─────────────── LLM 深度分析 (多 Provider + Workflow) ───────────────
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

    parts = [
        f"標的: {symbol} ({name})",
        f"現價: {ind.get('price')}, RSI: {ind.get('rsi')}, β: {ind.get('beta')}",
        f"MA20: {ind.get('ma20')}, MA60: {ind.get('ma60')}",
        f"52週高/低: {ind.get('high52')} / {ind.get('low52')}",
        f"今日漲跌: {ind.get('change_1d')}%, 1月漲跌: {ind.get('change_1m')}%",
    ]
    if pos:
        d = dict(pos)
        ret_pct = (ind.get("price", d["cost_price"])/d["cost_price"]-1)*100
        parts.append(f"持倉: {d['shares']} 股 @ 成本 {d['cost_price']} (報酬 {ret_pct:+.2f}%)")
    if watch:
        d = dict(watch)
        parts.append(f"進場目標: {d.get('target_entry')}, 停利: {d.get('target_profit')}, 停損: {d.get('target_stop')}")
        if d.get("notes"):
            parts.append(f"標的說明: {d['notes']}")
    if market:
        m = dict(market)
        parts.append(f"大盤: VIX {m.get('vix')}, 風險等級 {m.get('risk_level')}, 警訊數 {m.get('warnings_count')}/3")

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
        "prompt": """你是專業的技術分析師。根據以下股票數據，產出**繁體中文**技術分析報告（markdown，400字內）：

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
        "prompt": """你是專業的基本面分析師。根據以下股票數據，產出**繁體中文**基本面分析報告（markdown，400字內）：

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
        "prompt": """你是專業的新聞分析師。根據以下股票數據，從產業趨勢與潛在新聞面角度產出**繁體中文**分析（markdown，300字內）：

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
        "prompt": """你是市場情緒分析師。根據以下數據判斷市場對該標的的情緒狀態，產出**繁體中文**分析（markdown，300字內）：

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

請以**繁體中文** markdown 格式回應（500字內）：
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

請以**繁體中文** markdown 格式回應（400字內）：
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

請以**繁體中文** markdown 格式回應（500字內）：
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

            conn.commit()
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
    conn.execute("DELETE FROM analysis_results WHERE id=?", (aid,))
    conn.commit()
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

            analyst_prompt = f"""你是專業金融分析師。基於以下即時數據，給出**繁體中文**深度分析報告：

{ctx["context"]}

請依以下結構回應（markdown 格式，不超過 600 字）：

## 1. 技術面解讀
（趨勢、動能、支撐壓力）

## 2. 風險與機會
（多空因素、催化劑）

## 3. 操作建議
（具體買/賣/持有 + 進出場價位）

## 4. 時間框架
（短/中/長線）

## 5. 風險警示
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

請以**繁體中文** markdown 格式給出**犀利但建設性**的審查意見，不超過 400 字：

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

@app.get("/api/diagnose/{symbol}")
def api_diagnose(symbol: str):
    """On-demand diagnosis for a symbol."""
    conn = get_db()
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
    return sanitize_float_values({"symbol": symbol, "name": name, "diagnosis": diag, "indicators": ind})

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="127.0.0.1", port=6500)
