#!/usr/bin/env python3
"""TradingAgents Portfolio Dashboard — FastAPI single-file app."""

import asyncio
import json
import sqlite3
import warnings
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    call_llm,
)

warnings.filterwarnings("ignore")

BASE = Path(__file__).parent
DB = BASE / "portfolio.db"
USD_TWD = 32.0  # 預估匯率 (簡化)
TWSE_MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

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
        purchase_date TEXT
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
    """)
    conn.commit()
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
        with urlopen(req, timeout=8) as resp:
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

def fetch_yfinance_indicators(symbol: str, bench_close=None) -> dict:
    """Full indicator source backed by Yahoo Finance history."""
    try:
        h = yf.Ticker(symbol).history(period="1y")
        if len(h) < 20:
            return {}
        h.index = h.index.tz_localize(None)
        c = h["Close"]
        price = float(c.iloc[-1])
        prev_d = float(c.iloc[-2]) if len(c) > 1 else price
        ma20 = float(c.rolling(20).mean().iloc[-1])
        ma60 = float(c.rolling(60).mean().iloc[-1]) if len(c) >= 60 else ma20
        high52 = float(c.iloc[-252:].max() if len(c) >= 20 else c.max())
        low52 = float(c.iloc[-252:].min() if len(c) >= 20 else c.min())
        d = c.diff()
        gain = d.clip(lower=0).rolling(14).mean()
        loss = (-d.clip(upper=0)).rolling(14).mean()
        rsi = float((100 - 100/(1 + gain/loss)).iloc[-1])
        change_1d = (price/prev_d - 1) * 100
        change_1m = (price/float(c.iloc[-22]) - 1)*100 if len(c) > 21 else 0

        beta = None
        if bench_close is not None and len(bench_close) > 20:
            al = pd.concat([c.rename("s"), bench_close.rename("m")], axis=1).dropna()
            if len(al) > 20:
                rs = al["s"].pct_change().dropna()
                rm = al["m"].pct_change().dropna()
                cv = np.cov(rs, rm)
                beta = float(cv[0, 1]/cv[1, 1])

        return {
            "price": round(price, 2),
            "change_1d": round(change_1d, 2),
            "change_1m": round(change_1m, 2),
            "rsi": round(rsi, 1),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "high52": round(high52, 2),
            "low52": round(low52, 2),
            "beta": round(beta, 2) if beta else None,
            "source": "yfinance",
        }
    except Exception:
        return {}

def fetch_indicators(symbol: str, bench_close=None) -> dict:
    """Market-aware quote fetcher.

    Strategy: 永遠取 yfinance 的歷史指標（RSI/MA/Beta/52週高低），
    台股額外用 TWSE/TPEX 即時 quote 覆蓋 price/change_1d 提升即時性。
    這樣可避免「全走 TWSE 導致技術指標凍結」的問題。
    """
    yf_ind = fetch_yfinance_indicators(symbol, bench_close) or {}

    if _twse_channel(symbol):
        official = fetch_tw_realtime_quote(symbol)
        if official:
            # TWSE 即時 quote 優先覆蓋 price 與 change_1d，
            # 但保留 yfinance 算出的 RSI/MA/Beta 等歷史指標。
            for key in ("price", "change_1d"):
                if official.get(key) is not None:
                    yf_ind[key] = official[key]
            # high/low 只在 yfinance 沒有時用 TWSE 的當日高低備援
            for key in ("high52", "low52"):
                if yf_ind.get(key) is None and official.get(key) is not None:
                    yf_ind[key] = official[key]
            yf_ind["source"] = (
                "twse_realtime+yfinance" if yf_ind.get("rsi") is not None
                else "twse_realtime"
            )
            return yf_ind

    return yf_ind

def store_price_cache(c, symbol: str, ind: dict):
    """Persist fresh quote data while keeping older indicator fields when absent."""
    existing = c.execute("SELECT * FROM price_cache WHERE symbol=?", (symbol,)).fetchone()
    existing_data = json.loads(existing["data"] or "{}") if existing and existing["data"] else {}
    merged = dict(existing_data)
    for key, value in ind.items():
        if value is not None:
            merged[key] = value
    merged["source"] = ind.get("source", merged.get("source"))

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
    while True:
        try:
            print(f"[{datetime.now():%H:%M:%S}] Monitor cycle started...")
            twii = fetch_benchmark_close("^TWII")
            spx = fetch_benchmark_close("^GSPC")

            market = get_market_state()
            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO market_state (id, ts, vix, twii, twii_ma60, spx, spx_ma60, risk_level, warnings_count) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (market.get("ts"), market.get("vix"), market.get("twii"), market.get("twii_ma60"),
                       market.get("spx"), market.get("spx_ma60"), market.get("risk_level"), market.get("warnings_count")))

            # Positions
            for row in c.execute("SELECT * FROM positions").fetchall():
                d = dict(row)
                bench = twii if ".TW" in d["symbol"] else spx
                ind = fetch_indicators(d["symbol"], bench)
                if ind and "price" in ind:
                    store_price_cache(c, d["symbol"], ind)
                    created = insert_alerts(c, d["symbol"], d["name"], ind, market, position=d)
                    if created:
                        print(f"  [ALERT] {d['symbol']}: {created} new alert(s)")

            # Watchlist
            for row in c.execute("SELECT * FROM watchlist").fetchall():
                d = dict(row)
                bench = twii if ".TW" in d["symbol"] else spx
                ind = fetch_indicators(d["symbol"], bench)
                if ind and "price" in ind:
                    store_price_cache(c, d["symbol"], ind)
                    created = insert_alerts(c, d["symbol"], d["name"], ind, market, watch=d)
                    if created:
                        print(f"  [ALERT] {d['symbol']}: {created} new alert(s)")

            conn.commit()
            conn.close()
            print(f"[{datetime.now():%H:%M:%S}] Monitor cycle done.")
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
    return {
        "positions": out,
        "summary": {
            "total_cost": round(total_cost, 0),
            "total_value": round(total_value, 0),
            "total_pnl": round(total_value - total_cost, 0),
            "total_pnl_pct": round((total_value/total_cost-1)*100, 2) if total_cost > 0 else 0,
        }
    }

@app.get("/api/watchlist")
def api_watchlist():
    conn = get_db()
    rows = conn.execute("""
        SELECT w.*, pc.price as current_price, pc.rsi, pc.change_1d, pc.ma20, pc.ma60
        FROM watchlist w
        LEFT JOIN price_cache pc ON w.symbol = pc.symbol
    """).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
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
                # 距離越近分數越高
                priority = max(20, 50 - abs(dist))
        else:
            d["status"] = "資料中"
            d["status_class"] = "text-gray-500"
            priority = 0

        d["priority"] = round(priority, 1)
        out.append(d)

    # 按優先度降序，再按類別
    out.sort(key=lambda x: (-x["priority"], x.get("category") or ""))
    return {"watchlist": out}

@app.get("/api/alerts")
def api_alerts(limit: int = 50):
    conn = get_db()
    rows = conn.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return {"alerts": [dict(r) for r in rows]}

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
    """Manually refresh prices and evaluate alerts immediately."""
    twii = fetch_benchmark_close("^TWII")
    spx = fetch_benchmark_close("^GSPC")
    market = get_market_state()
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO market_state (id, ts, vix, twii, twii_ma60, spx, spx_ma60, risk_level, warnings_count) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
              (market.get("ts"), market.get("vix"), market.get("twii"), market.get("twii_ma60"),
               market.get("spx"), market.get("spx_ma60"), market.get("risk_level"), market.get("warnings_count")))

    refreshed = 0
    alerts_created = 0
    for table in ("positions", "watchlist"):
        for row in c.execute(f"SELECT * FROM {table}").fetchall():
            d = dict(row)
            bench = twii if ".TW" in d["symbol"] else spx
            ind = fetch_indicators(d["symbol"], bench)
            if ind and "price" in ind:
                store_price_cache(c, d["symbol"], ind)
                refreshed += 1
                if table == "positions":
                    alerts_created += insert_alerts(c, d["symbol"], d["name"], ind, market, position=d)
                else:
                    alerts_created += insert_alerts(c, d["symbol"], d["name"], ind, market, watch=d)
    conn.commit()
    conn.close()
    return {"refreshed": refreshed, "alerts_created": alerts_created, "market": market}

@app.get("/api/history/{symbol}")
def api_history(symbol: str, period: str = "6mo"):
    """歷史 OHLC 數據 (給 K 線圖使用)。"""
    try:
        h = yf.Ticker(symbol).history(period=period)
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
                "color": "rgba(16,185,129,0.6)" if row["Close"] >= row["Open"] else "rgba(239,68,68,0.6)",
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
        return {"symbol": symbol, "candles": candles, "volumes": volumes, "ma20": ma20, "ma60": ma60}
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

@app.post("/api/positions")
def api_add_position(p: PositionCreate):
    conn = get_db()
    conn.execute(
        "INSERT INTO positions (symbol, name, category, shares, cost_price, currency, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (p.symbol, p.name, p.category, p.shares, p.cost_price, p.currency, datetime.now().date().isoformat())
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
    return {"alerts": [dict(r) for r in rows]}

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

## ✅ 分析師說對的地方
（簡述 1~2 點）

## ⚠️ 我有疑慮的地方
（指出邏輯漏洞、忽略的風險、過度樂觀/悲觀）

## ❌ 我認為錯誤或缺失的部分
（具體指出）

## 🎯 修正後的建議
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
    return {"symbol": symbol, "name": name, "diagnosis": diag, "indicators": ind}

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="127.0.0.1", port=8765)
