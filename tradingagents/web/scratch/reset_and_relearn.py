#!/usr/bin/env python3
"""Reset adaptive learning state, evaluate multiple intervals (1m~30m), and relearn using historical Binance data."""

import argparse
import os
import sqlite3
import sys
import shutil
from pathlib import Path
from dataclasses import asdict

# Add web directory to path so imports work correctly
WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

from smc_quant import LedgerPaths, compute_expectancy, read_trade_ledger
from deps import _portfolio_db_path
from smc_training_loop import train_from_ledger
from learning.historical_seeder import seed_one_symbol


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
                print(f"[reset] Table {table} clear skipped: {e}")
        conn.commit()
        print("[reset] Database tables reset successfully.")
    except Exception as e:
        print(f"[reset] Error resetting database: {e}")
        conn.rollback()
    finally:
        conn.close()


def clean_ledger_files(ledger_path: str):
    """Backup and remove old ledger files for all intervals."""
    ledger_path_obj = Path(ledger_path)
    ledger_dir = ledger_path_obj.parent
    
    # 備份主 ledger
    if ledger_path_obj.exists():
        backup_path = str(ledger_path_obj) + ".bak"
        print(f"[reset] 備份舊的 Ledger: {ledger_path_obj} -> {backup_path}")
        shutil.copyfile(str(ledger_path_obj), backup_path)
        ledger_path_obj.unlink()
        print(f"[reset] 已刪除舊的 Ledger 檔案: {ledger_path_obj}")
        
    # 刪除時框暫存 ledger
    for p in ledger_dir.glob("smc_training_ledger_*.jsonl"):
        try:
            p.unlink()
            print(f"[reset] 已刪除暫存時框 Ledger: {p.name}")
        except Exception as e:
            print(f"[reset] 刪除暫存 Ledger {p.name} 失敗: {e}")


def main():
    parser = argparse.ArgumentParser(description="Reset adaptive learning, evaluate intervals 1m~30m, and relearn")
    parser.add_argument("--symbols", type=str, default="BTC-USDT", help="Comma-separated list of symbols (default: BTC-USDT)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    db_path = _portfolio_db_path()
    ledger_path = LedgerPaths.training_ledger()

    print("=== 1. 重置自適應學習狀態與所有時框 Ledger ===\n")
    clean_ledger_files(ledger_path)
    reset_database(db_path)

    # 時框與歷史回溯月份對應（1m 數據量極大故縮短，確保 API 效能與樣本數均衡）
    interval_months = {
        "1m": 0.5,    # 15 天
        "3m": 1.0,    # 1 個月
        "5m": 2.0,    # 2 個月
        "15m": 6.0,   # 6 個月
        "30m": 6.0    # 6 個月
    }

    print("\n=== 2. 多時框交易模擬播種 (1m ~ 30m) ===")
    
    # 為了評估，我們逐一針對每個時框跑播種
    interval_results = {}
    for sym in symbols:
        for interval, months in interval_months.items():
            temp_ledger = str(Path(ledger_path).parent / f"smc_training_ledger_{sym}_{interval}.jsonl")
            print(f"\n[seeder] 開始播種 {sym} 時框: {interval}，回溯時間: {months} 個月...")
            try:
                res = seed_one_symbol(
                    api_stub=None,
                    symbol=sym,
                    interval=interval,
                    months=months,
                    ledger_path=temp_ledger,
                    db_path=db_path
                )
                print(f"[seeder] {sym} {interval} 播種完成 ✓ (抓取 K 線: {res.klines_fetched}, 模擬次數: {res.backtests_run}, 交易筆數: {res.trades_persisted})")
                
                # 載入產出的交易以進行評估
                records = read_trade_ledger(temp_ledger, symbol=sym)
                if len(records) >= 15:
                    exp = compute_expectancy(records)
                    expected_r = float(exp.get("expected_R") or 0.0)
                    win_rate = float(exp.get("win_rate") or 0.0)
                    interval_results[interval] = {
                        "expected_r": expected_r,
                        "win_rate": win_rate,
                        "sample_size": len(records),
                        "temp_ledger": temp_ledger
                    }
                    print(f"  -> 評估指標: 筆數={len(records)}, 每筆期望收益={expected_r:.4f}R, 勝率={win_rate*100:.2f}%")
                else:
                    print(f"  -> 交易筆數 {len(records)} 不足 15 筆，不納入評估。")
            except Exception as e:
                print(f"[seeder] {sym} {interval} 播種時發生錯誤，跳過該時框: {e}")

    print("\n=== 3. 系統自行評估最佳交易時框 ===")
    best_interval = None
    best_stats = None
    
    # 尋求期望值最高且交易筆數足夠的時框
    for interval, stats in interval_results.items():
        if best_interval is None:
            best_interval = interval
            best_stats = stats
        else:
            # 優先比較期望收益，期望收益相同則比樣本大小
            if stats["expected_r"] > best_stats["expected_r"]:
                best_interval = interval
                best_stats = stats
            elif abs(stats["expected_r"] - best_stats["expected_r"]) < 1e-6 and stats["sample_size"] > best_stats["sample_size"]:
                best_interval = interval
                best_stats = stats

    if best_interval:
        print(f"\n[evaluation] 評估完成！系統自動選擇最佳交易區間為: **{best_interval}**")
        print(f"  - 每筆期望收益 (Expected R): {best_stats['expected_r']:.4f}R")
        print(f"  - 預估勝率 (Win Rate): {best_stats['win_rate']*100:.2f}%")
        print(f"  - 歷史樣本數 (Sample Size): {best_stats['sample_size']}")
        
        # 將最佳時框的暫存 ledger 複製為正式的 training_ledger
        shutil.copyfile(best_stats["temp_ledger"], ledger_path)
        print(f"[evaluation] 已將 {best_interval} 的歷史數據注入正式 Ledger 檔案中。")
    else:
        # Fallback to default
        best_interval = "15m"
        print(f"\n[evaluation] 所有時框均無足夠交易樣本，fallback 至預設時框: {best_interval}")
        # 建立空 ledger
        Path(ledger_path).touch()

    # 清除所有暫存 ledger
    for p in Path(ledger_path).parent.glob("smc_training_ledger_*.jsonl"):
        try:
            p.unlink()
        except Exception:
            pass

    print("\n=== 4. 執行全量自適應學習模型重新訓練與最佳時框寫入 ===")
    print(f"[learning] 基於最佳時框 {best_interval} 的 {best_stats['sample_size'] if best_stats else 0} 筆歷史樣本進行模型訓練...")
    
    # 執行 train_from_ledger，傳入選定的最佳時框
    result = train_from_ledger(
        ledger_path=ledger_path,
        db_path=db_path,
        symbol="ALL" if len(symbols) > 1 else symbols[0],
        optimal_interval=best_interval,
        apply_strategy_patch=True
    )

    print("\n=== 重新學習完成報告 ===")
    print(f"採用時框 (Selected Interval): {best_interval}")
    print(f"樣本總數 (Sample Size): {result.sample_size}")
    print(f"自適應模式 (Mode): {result.adaptive_state.get('mode')}")
    print(f"是否採用新權重 (Adopted): {result.adopted}")
    print(f"校準裁定 (Verdict): {result.verdict}")
    if result.weights_changed:
        print(f"被改變的特徵權重: {result.weights_changed}")
    if result.notes:
        print("學習日誌備註:")
        for note in result.notes:
            print(f"  - {note}")


if __name__ == "__main__":
    main()
