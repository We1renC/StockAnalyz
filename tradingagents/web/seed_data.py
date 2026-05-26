#!/usr/bin/env python3
"""Seed the database with example positions + recommended watchlist.

如要使用您自己的真實持倉，請建立 seed_data_private.py（已被 .gitignore 排除）：

    POSITIONS = [
        ("2330.TW", "台積電", "半導體", 100, 1000.0, "TWD"),
        ...
    ]

本檔會在 import 時自動偵測並覆蓋下方範例 POSITIONS。
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "portfolio.db"

# 範例持倉（公開可分享 — 不含真實成本價）
POSITIONS = [
    # (symbol, name, category, shares, cost, currency)
    ("2330.TW", "台積電",   "半導體", 0, 0.0, "TWD"),
    ("2412.TW", "中華電信", "電信",   0, 0.0, "TWD"),
]

# 嘗試讀取個人覆蓋檔（不會被 git 追蹤）
try:
    from seed_data_private import POSITIONS as _PRIVATE_POSITIONS
    POSITIONS = _PRIVATE_POSITIONS
    print("✓ 載入個人持倉資料 (seed_data_private.py)")
except ImportError:
    print("ℹ 使用範例持倉資料；如要使用真實資料請建立 seed_data_private.py")

# 推薦觀察清單（電力 + 潛力股）
WATCHLIST = [
    # symbol, name, category, currency, entry, add, profit, stop, notes
    # 電力 S 級
    ("VRT",     "Vertiv",            "電力-S級",  "USD", 319.00, 362.51, 412.76, 264.44, "AI機房電源/冷卻王者"),
    ("GEV",     "GE Vernova",        "電力-S級",  "USD", 926.72, 1161.03, 1321.96, 880.38, "燃氣輪機訂單到2028"),
    ("CEG",     "Constellation",     "電力-S級",  "USD", 302.75, 406.98, 463.39, 286.50, "微軟長約PPA"),
    ("2308.TW", "台達電",            "電力-S級",  "TWD", 2012, 2303, 2622, 1505, "全球電源供應器龍頭"),
    ("3017.TW", "奇鋐",              "電力-S級",  "TWD", 2087, 2974, 3387, 1982, "GB200散熱獨家供應"),
    # 電力 A 級
    ("1519.TW", "華城",              "電力-A級",  "TWD", 879, 1091, 1242, 809, "台積電變壓器供應商"),
    ("1513.TW", "中興電",            "電力-A級",  "TWD", 153, 187, 213, 147, "GIS氣體絕緣開關"),
    # 新創（政府背書）
    ("OKLO",    "Oklo",              "新創-SMR",  "USD", 68.57, 175.88, 200.26, 70.90, "Sam Altman+DOE ARDP"),
    ("LEU",     "Centrus Energy",    "新創-核燃料","USD", 205.85, 440.36, 501.40, 223.33, "DOE HALEU合約"),
    ("IONQ",    "IonQ",              "新創-量子", "USD", 44.57, 82.91, 94.40, 39.46, "DOE合約量子龍頭"),
    # 潛力 S 級
    ("6691.TW", "洋基工程",          "潛力-S級",  "TWD", 652, 691, 787, 591, "台積電潔淨室指定承包"),
    ("2049.TW", "上銀",              "潛力-S級",  "TWD", 297, 324, 369, 242, "Tesla Optimus供應鏈"),
    ("PL",      "Planet Labs",       "潛力-S級",  "USD", 36.89, 40.29, 45.87, 23.90, "對地觀測+AI"),
    # 潛力 A/B 級
    ("4576.TW", "大銀微",            "潛力-A級",  "TWD", 203, 244, 278, 123, "線性馬達/機器人"),
    ("UEC",     "Uranium Energy",    "潛力-A級",  "USD", 14.79, 20.34, 23.16, 13.66, "美本土ISR鈾礦"),
    ("MBLY",    "Mobileye",          "潛力-跌深", "USD", 8.45, 19.27, 21.94, 8.92, "RoboTaxi 2026催化"),
    ("6533.TW", "晶心科",            "潛力-跌深", "TWD", 227, 329, 374, 220, "RISC-V IP, NVDA合作"),
    ("TEM",     "Tempus AI",         "潛力-AI醫", "USD", 45, 75, 100, 35, "AI癌症診斷"),
    ("ABBNY",   "ABB Ltd",           "潛力-機器人","USD", 100, 130, 150, 90, "工業機器人龍頭"),
    # 防禦工具（避險用）
    ("00713.TW","元大台灣高息低波",   "防禦-低波", "TWD", 50, 60, 65, 45, "波動最低的高息ETF"),
    ("2412.TW", "中華電信",          "防禦-電信", "TWD", 130, 145, 155, 122, "Beta 0.06 抗跌"),
    ("00632R.TW","元大台灣50反1",     "對沖-反向", "TWD", 11.5, 14.0, 16.0, 10.0, "短期回檔對沖工具"),
    ("00635U.TW","元大S&P黃金",      "對沖-黃金", "TWD", 47, 55, 60, 42, "全球風險對沖"),
]

def seed():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # init schema
    c.executescript("""
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, name TEXT, category TEXT,
        shares REAL NOT NULL, cost_price REAL NOT NULL,
        currency TEXT DEFAULT 'TWD', purchase_date TEXT
    );
    CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, name TEXT, category TEXT,
        currency TEXT DEFAULT 'TWD',
        target_entry REAL, target_add REAL,
        target_profit REAL, target_stop REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, symbol TEXT NOT NULL,
        level TEXT NOT NULL, type TEXT NOT NULL,
        message TEXT, price REAL, diagnosis TEXT,
        acknowledged INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS market_state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        ts TEXT, vix REAL, twii REAL, twii_ma60 REAL,
        spx REAL, spx_ma60 REAL, risk_level TEXT, warnings_count INTEGER
    );
    CREATE TABLE IF NOT EXISTS price_cache (
        symbol TEXT PRIMARY KEY, ts TEXT, price REAL, rsi REAL,
        ma20 REAL, ma60 REAL, high52 REAL, low52 REAL,
        change_1d REAL, change_1m REAL, beta REAL, data TEXT
    );
    """)

    # 清空後重建
    c.execute("DELETE FROM positions")
    c.execute("DELETE FROM watchlist")
    for p in POSITIONS:
        c.execute("INSERT INTO positions (symbol, name, category, shares, cost_price, currency) VALUES (?, ?, ?, ?, ?, ?)", p)
    for w in WATCHLIST:
        c.execute("INSERT INTO watchlist (symbol, name, category, currency, target_entry, target_add, target_profit, target_stop, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", w)
    conn.commit()

    pos_count = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    watch_count = c.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    conn.close()
    print(f"✅ Seeded {pos_count} positions and {watch_count} watchlist items")

if __name__ == "__main__":
    seed()
