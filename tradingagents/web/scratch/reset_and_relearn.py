#!/usr/bin/env python3
"""Reset adaptive learning state and retrain using historical Binance data."""

import argparse
import os
import sqlite3
import sys
import shutil
from pathlib import Path

# Add web directory to path so imports work correctly
WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

from smc_quant import LedgerPaths
from deps import _portfolio_db_path
from learning.historical_seeder import seed_one_symbol, run_post_seed_training


def reset_database(db_path: str):
    """Clear all adaptive tables in the SQLite database to reset state."""
    if not os.path.exists(db_path):
        print(f"[reset] Database {db_path} does not exist. Skipping database reset.")
        return

    print(f"[reset] Connecting to database: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            "smc_adaptive_trade_ledger",
            "smc_adaptive_config_patches",
            "smc_adaptive_audit_logs",
            "smc_adaptive_training_history"
        ]
        for table in tables:
            try:
                conn.execute(f"DELETE FROM {table}")
                print(f"[reset] Cleared table: {table}")
            except sqlite3.OperationalError as e:
                # Table might not exist yet
                print(f"[reset] Table {table} clear skipped: {e}")
        conn.commit()
        print("[reset] Database tables reset successfully.")
    except Exception as e:
        print(f"[reset] Error resetting database: {e}")
        conn.rollback()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Reset adaptive learning and relearn from history")
    parser.add_argument("--months", type=int, default=6, help="How many months of history to seed (default 6)")
    parser.add_argument("--symbols", type=str, default="BTC-USDT", help="Comma-separated list of symbols (default: BTC-USDT)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    db_path = _portfolio_db_path()
    ledger_path = LedgerPaths.training_ledger()

    print("=== 1. 重置自適應學習狀態與 Ledger 檔案 ===")
    
    # 備份並移除舊的 ledger.jsonl
    if os.path.exists(ledger_path):
        backup_path = ledger_path + ".bak"
        print(f"[reset] 備份舊的 Ledger: {ledger_path} -> {backup_path}")
        shutil.copyfile(ledger_path, backup_path)
        os.remove(ledger_path)
        print(f"[reset] 已刪除舊的 Ledger 檔案: {ledger_path}")
    else:
        print("[reset] 找不到舊的 Ledger 檔案，跳過刪除。")

    # 清空 SQLite 數據表
    reset_database(db_path)

    print("\n=== 2. 拉取幣安歷史數據並跑前向 Backtest 播種 ===")
    for sym in symbols:
        # 取得 symbol 設定的 interval
        from smc_auto_workflow import profile_for_symbol
        try:
            profile = profile_for_symbol(sym)
            interval = profile.interval
        except Exception:
            interval = "1h"

        print(f"[seeder] 開始播種 {sym} ({interval})，回溯時間：{args.months} 個月...")
        res = seed_one_symbol(
            api_stub=None,
            symbol=sym,
            interval=interval,
            months=args.months,
            ledger_path=ledger_path,
            db_path=db_path
        )
        print(f"[seeder] {sym} 播種完成 ✓")
        print(f"  - 抓取 K 線數: {res.klines_fetched}")
        print(f"  - 執行模擬數: {res.backtests_run}")
        print(f"  - 產出交易數: {res.trades_persisted}")
        print(f"  - 花費時間: {res.elapsed_s} 秒")

    print("\n=== 3. 執行全量歷史交易自適應模型重新訓練與校準 ===")
    print("[learning] 正在執行 post-seed 訓練...")
    training = run_post_seed_training(
        ledger_path=ledger_path,
        db_path=db_path,
        symbol="ALL" if len(symbols) > 1 else symbols[0]
    )

    print("\n=== 重新學習完成報告 ===")
    print(f"樣本總數 (Sample Size): {training['sample_size']}")
    print(f"自適應模式 (Mode): {training.get('learning_indicator')}")
    print(f"是否採用新權重 (Adopted): {training['adopted']}")
    print(f"校準裁定 (Verdict): {training['verdict']}")
    if training.get("weights_changed"):
        print(f"被改變的特徵權重: {training['weights_changed']}")
    if training.get("notes"):
        print("學習日誌備註:")
        for note in training["notes"]:
            print(f"  - {note}")


if __name__ == "__main__":
    main()
