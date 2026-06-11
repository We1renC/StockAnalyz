# SMC 學習子系統 — 運維參考

本文件彙整 E→Q 各輪架構改善後的所有 runtime 旋鈕、ops endpoint 與維運流程。
（內容只描述 `tradingagents/web/` 內的 SMC × crypto 學習子系統。）

## 環境變數

| 變數 | 預設 | 作用 | 來源輪次 |
|---|---|---|---|
| `SMC_AUTOLEARN_ENABLED` | unset(關) | `=1` 啟用 server-side 學習迴路（headless，不需開瀏覽器） | E1 |
| `SMC_AUTOLEARN_SYMBOLS` | `BTC-USDT,ETH-USDT,SOL-USDT` | 排程學習的 symbol 清單（逗號分隔） | E1 |
| `SMC_AUTOLEARN_MIN_INTERVAL` | `30` | 每 symbol tick 最小間隔（秒），防 busy-spin | E1 |
| `SMC_MAINTENANCE_INTERVAL` | `21600`(6h) | 自動維護週期（rotation + decommission + WAL checkpoint） | J/N |
| `SMC_LEDGER_KEEP_PER_SYMBOL` | `1000` | 自動 rotation 每 symbol 保留筆數 | J |
| `SMC_LEDGER_DIR` | `tmp` | ledger jsonl 根目錄（測試/prod 切換用） | C2 |
| `SMC_LEARNING_DB` | unset | （E2 cut-over 後）學習資料庫路徑 | E2 |
| `DASHBOARD_API_TOKEN` | unset(關) | 設定後，`/api/smc-crypto/*` 等需帶 `X-API-Token` header | A2 |
| `LOG_LEVEL` | `INFO` | 結構化 log 等級（`smc.*` namespace） | G1 |
| `OBSIDIAN_VAULT_PATH` | settings.json | audit note / 週報 / sweep 紀錄輸出 vault | C3/B1 |

> 敏感值（`DASHBOARD_API_TOKEN` 等）依 CLAUDE.md 規則存 `settings.json`，**不入版控**。

## Ops endpoints

| 方法 | 路徑 | 用途 | 輪次 |
|---|---|---|---|
| GET | `/api/smc-crypto/selfcheck` | 部署 preflight（7 項 pass/warn/fail） | P |
| GET | `/api/smc-crypto/ops-metrics` | scheduler 狀態 / cache 命中 / swallow 計數 / ledger+WAL 大小 | G2/N |
| GET | `/api/smc-crypto/learning-health` | 0-100 學習健康分（4 component） | C1 |
| POST | `/api/smc-crypto/wal-checkpoint` | 手動 WAL TRUNCATE checkpoint | N |
| POST | `/api/smc-crypto/rotate-ledger` | 手動 ledger 修剪 + gzip 歸檔 | G3 |
| POST | `/api/smc-crypto/decommission-sweep` | 手動 per-detector 下架/復活掃描 | D3 |
| GET | `/api/smc-crypto/hyperparameter-sweep` | walk-forward 超參數掃描 | P3-18/D2 |
| GET | `/api/smc-crypto/cluster-ensemble` | per-cluster factor lift（BH-FDR 校正） | P3-19/D1 |
| GET | `/api/smc-crypto/real-pnl-gates` | 三項真實 PnL 硬閘門 | P3-17 |
| GET | `/api/smc-crypto/learning-curve` | 累積曲線 + velocity + ETA | P3-20 |
| POST | `/api/smc-crypto/weekly-digest` | 週報 Obsidian markdown | C3 |
| POST | `/api/smc-crypto/baseline-equity/reset` | 重設淨值基準 | (PnL fix) |

## 自動運轉（headless）

```bash
export SMC_AUTOLEARN_ENABLED=1
export DASHBOARD_API_TOKEN=$(openssl rand -hex 32)   # prod 建議
# 啟動後系統自動：
#   每 30s+   per-symbol tick 學習（throttling 自動調速）
#   每 6h     rotate ledger + decommission sweep + WAL checkpoint
#   開機時    印出 selfcheck 摘要（warn/fail 立即可見）
#   全程      失敗記入 ops-metrics.swallowed_errors（不靜默）
```

## E2 DB cut-over（重大操作，需人工確認）

```bash
cd web
python -m learning.db_split portfolio.db learning.db            # dry-run
python -m learning.db_split portfolio.db learning.db --execute  # 複製 + 驗證
# 驗證 ok 後：export SMC_LEARNING_DB=learning.db → 重啟 → 確認 dashboard
#            → 手動 drop portfolio.db 內舊 learning 表
```

## 架構分層（E→Q）

- **E** 架構：E1 server scheduler / E2 DB 分離工具 / E3 WAL / E4 thread-safe 權重
- **F** 可維護：F1 routers 拆分（smc_learning 15 + paper_acceptance 46）/ F2 swallow 帳本 / F3 ledger I/O 抽出 + deps.py
- **G** 可觀測：G1 structured log / G2 ops-metrics / G3 ledger rotation
- **H** 測試：H1 spine / H2 executor（既有）/ H3 route smoke
- **J→Q** 運維：J 自動維護 / K runner swallow / L paper-acceptance router / M pure analytics router / N WAL checkpoint / O strategy.yaml 驗證 / P selfcheck / Q boot 自檢 + 本文件

## Phase-1 全自動化地基（2026-06-11）

### launchd 守護（開機自啟 + crash 自動重啟）
```bash
# 已安裝於 ~/Library/LaunchAgents/com.smc.dashboard.plist
launchctl unload ~/Library/LaunchAgents/com.smc.dashboard.plist   # 停用
launchctl load   ~/Library/LaunchAgents/com.smc.dashboard.plist   # 啟用
# KeepAlive: crash 10 秒內重啟（已實測 kill -9 驗證）
# start_daemon.sh 內含 caffeinate -i 防 idle sleep（合蓋仍會睡）
```
注意：手動 ./start.sh 前先 unload，否則 port 衝突。

### 告警通道（learning/alerting.py）
- 預設 macOS 桌面通知（零設定）；
  settings.json 加 `telegram_bot_token` + `telegram_chat_id` 後自動升級為 Telegram（手機可收）
- 觸發點：開機自檢 FAIL / 學習迴路每連續 5 次錯誤 / 自動維護失敗
- 同標題 30 分鐘冷卻防轟炸

### 市場資料
- settings.json `binance_api_url` 已切 `https://data-api.binance.vision`
  （mainnet 公開資料，免 key；BTC 1h 從 73 根 → 500 根）
