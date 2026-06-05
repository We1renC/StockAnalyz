# Quant Paper Acceptance 完整開發規劃

## 目標

依循 `quant_paper_trading_acceptance_standard_v1.0.md`，將目前分支 `codex/quant-paper-acceptance` 上既有的前測驗收能力，擴充成一套可持續運作的 acceptance framework，而不是一次性的報告產生器。

本規劃的完成標準不是「有一份報告」，而是：

1. 標準文件第 3 到 22 節都能映射到系統中的資料、規則、事件或測試。
2. 每一個 acceptance gate 都有明確證據來源：
   - `framework`：程式結構或執行模型本身保證；
   - `observed`：由實際前測、回測、監控、對帳、事件紀錄觀察得到；
   - `manual`：需要操作者人工補證，但必須留下結構化紀錄。
3. 缺資料與未驗證項目必須明示，不可用推測值假裝通過。
4. 驗收結果要能驅動下一步動作：
   - 允許進入下一階段；
   - 限定修補後重驗；
   - 直接禁止上線；
   - 回到研究階段。

## 現況基線

目前這個分支已經有以下能力：

### 已落地

1. `tradingagents/web/paper_acceptance.py`
   - 將標準轉成 gate/prohibition/report schema。
   - 支援 summary、blocking issues、section aggregation、markdown report。
2. `tradingagents/web/paper_execution.py`
   - 已有保守版 market / limit paper execution。
   - 已有 unknown order state handling 與 risk-first order approval。
3. `tradingagents/web/paper_acceptance_store.py`
   - 已有 SQLite 儲存層：
     - `paper_acceptance_runs`
     - `paper_acceptance_events`
     - `paper_acceptance_context_overrides`
     - `paper_acceptance_evidence`
   - 已可從 SMC journal / backtest / events 組出 acceptance workspace。
4. `tradingagents/web/app.py`
   - 已有 report / workspace / check / event CRUD API。
5. `tradingagents/web/templates/index.html`
   - 已有 SMC 前測驗收工作區 UI，可瀏覽 section、gate、manual check、override。
6. 測試
   - `test_paper_acceptance.py`
   - `test_paper_execution.py`
   - `test_paper_acceptance_store.py`
   - `test_paper_acceptance_api.py`
   - `test_paper_acceptance_metrics.py`
   - `test_paper_acceptance_policy.py`
   - `test_paper_acceptance_scenarios.py`
   - `test_paper_acceptance_security.py`

### 2026-06-05 實作快照

目前這條分支不再只是 acceptance report 生成器，已經具備下列持續運作能力：

1. telemetry / reconciliation / order audit / alert delivery 已有獨立資料表與 observed evidence 聚合。
2. abnormal scenario harness 已落地，可把 `9.x / 10.x` 結果回寫 acceptance workspace。
3. governance workflow 已落地，包含 review、change log、promotion check、coverage audit。
4. security hygiene 已接到 acceptance policy，可把 secret / permission hygiene 納入 promotion decision。
5. capacity stage / deviation snapshot 已落地，並可由 paper/live telemetry 自動生成。
6. refresh cycle 已可自動重算 acceptance report，避免 evidence 長期停在手動生成狀態。
7. coverage summary 已細化到 section / gate / missing check 層級，可直接用來追缺口。
8. shadow parity 已有獨立 evidence flow、summary、workspace panel 與 promotion ladder 連動。
9. sample size 不再只看筆數，已納入 regime / liquidity / session coverage matrix。
10. research discipline 已有 governance events、freeze window、restart-stats 與 override taxonomy。
11. final closure summary 已能整合 report / policy / coverage / governance / event stitching。

### 目前仍偏弱或缺失

現有系統強在：

- gate schema 與報告層；
- conservative paper execution baseline；
- manual/observed evidence workspace；
- API/UI 可操作性。

現有系統弱在：

- `2.2` shared architecture 仍停留在 acceptance 判定層，尚未真正把 live execution adapter 與 paper adapter 收斂到同一個 runtime contract。
- `14` shadow trading 雖已有 parity trace 與 workspace，但仍缺真正共用 execution adapter contract。
- `18 / 19 / 20` 已有 capacity/deviation 基礎資料流，但 live capital stage 的自動晉級規則與 quantitative threshold calibration 仍偏保守 placeholder。
- `22` 已有 closure summary 與 rationale stitching，但 markdown 報告本體仍可再強化更細的 section-level narrative。

## 完成度標記

- `done`：已有 framework / observed / manual 閉環，且至少有測試覆蓋。
- `partial`：已有 schema 或 policy，但 observed evidence 或 runtime contract 尚未完整。
- `missing`：仍需新增資料流或新模組。

## 章節狀態總覽

| 標準章節 | 狀態 | 目前支撐 | 主要缺口 |
|---|---|---|---|
| `2.2` shared architecture | `partial` | acceptance policy / report 已要求共享架構 | 缺真正共用 execution/runtime adapter contract |
| `3.1` strategy logic | `done` | gate + manual/observed checks + report | 仍可補更嚴格 strategy spec 模板 |
| `3.2` instrument check | `partial` | telemetry/evidence 已能覆蓋 spread/depth/volume proxy | 缺更直接的 book-capacity regression |
| `3.3` data source | `partial` | timestamps / missing / duplicate / out-of-order evidence 已接線 | 缺 market-timestamp vs receipt-time 全鏈路追蹤 |
| `4.1` market execution | `done` | paper execution + tests | 可再補更細的 liquidity regime calibration |
| `4.2` limit execution | `done` | queue / partial fill / timeout / post-only tests | 可再補 venue-specific queue model |
| `4.3` slippage impact | `partial` | order audit + telemetry + report | live-side market impact calibration 仍不足 |
| `5.1` fee check | `partial` | fee evidence / gross-net metrics 已接入 | derivatives fee path 仍未完整 |
| `5.2` derivatives cost | `missing` | 無完整 futures/options fee model | 需新增 derivatives-specific telemetry |
| `6.1` order state | `done` | order lifecycle audit + tests | 可再補 exchange-specific reject taxonomy |
| `6.2` unknown order state | `done` | simulator + scenario + evidence | 可再接真實 broker replay |
| `7.1` virtual account | `partial` | policy/schema 已有 | 缺多幣別虛擬帳戶長期流水 |
| `7.2` reconciliation | `done` | reconciliation runs + evidence + tests | 可再補 restore playbook evidence |
| `8.1` API rate limits | `partial` | runtime metrics + coverage | 缺真實 provider-specific budget replay |
| `8.2` latency | `done` | telemetry + p95/p99 + UI | 可再補 stage-specific SLA thresholds |
| `8.3` stability | `partial` | runtime metrics + restart/reconnect evidence | 缺長週期 soak test automation |
| `9.1` network abnormality | `done` | scenario harness + tests | 可再補 live connector fault injection |
| `9.2` market abnormality | `done` | scenario harness + tests | 可再補更多 microstructure cases |
| `9.3` program abnormality | `done` | scenario harness + tests | 可再補 DB corruption / disk pressure variants |
| `10.1` risk priority | `done` | execution tests + policy | 目前足夠 |
| `10.2` position risk | `done` | scenario/tests/policy | 目前足夠 |
| `10.3` loss risk | `done` | scenario/tests/policy | 目前足夠 |
| `10.4` kill switch | `done` | scenario/tests/policy/UI | 目前足夠 |
| `11.1` dashboard | `partial` | workspace + observed panel + coverage | 缺獨立即時 dashboard 契約 |
| `11.2` alerting | `done` | alert delivery schema + evidence | 目前足夠 |
| `12.1` performance | `done` | report metrics / workspace / tests | 目前足夠 |
| `12.2` trade quality | `done` | slippage/fill/reject/latency metrics | 目前足夠 |
| `13` behavior deviation | `partial` | backtest-paper / paper-live deviation snapshot | 缺更細的 attribution 與 threshold calibration |
| `14` shadow trading | `partial` | shadow parity trace、policy、workspace、promotion ladder | 缺真正 shared execution/runtime adapter |
| `15` sample size | `done` | regime/liquidity/session coverage matrix + policy + tests | 可再補 strategy-specific market-cycle taxonomy |
| `16` research discipline | `done` | governance events、freeze window、restart-stats、override taxonomy | 可再補多 reviewer workflow |
| `17` API security | `done` | security scan + promotion gate + tests | 目前足夠 |
| `18` capacity scaling | `partial` | capital stage snapshots + UI | 缺真正 capital promotion workflow |
| `19` paper vs small-live | `partial` | deviation snapshots + policy | 缺 live rollout stage policy 與 explainability |
| `20` quantitative thresholds | `partial` | policy / promotion check 已存在 | 門檻多為保守預設，需 strategy-specific calibration |
| `21` prohibit live trading | `done` | prohibition flags + policy + API/tests | 目前足夠 |
| `22` final report | `done` | structured report + markdown + persisted runs + closure summary | 可再補更長篇 narrative markdown 呈現 |
| `23` final principles | `partial` | 整體 framework 大致對齊 | 需用 remaining gaps 收斂到 production-ready 標準 |

## 標準覆蓋矩陣

### A. 已具備骨架，需補資料來源

這些章節已有 gate 與 UI，但 observed evidence 不足：

1. `3.2 Trading Instrument Check`
2. `3.3 Data Source Check`
3. `4.3 Slippage and Market Impact`
4. `5.1 Fee Check`
5. `7.2 Reconciliation Check`
6. `8.1 API Rate Limit Check`
7. `8.2 Latency Check`
8. `8.3 System Stability Check`
9. `11.1 Monitoring Dashboard Check`
10. `11.2 Alerting Mechanism Check`
11. `12.1 / 12.2 Performance & Trade Quality`
12. `13 Strategy Behavior Deviation`
13. `18 Capacity and Capital Scaling`
14. `19 Paper vs Small-Scale Live Comparison`
15. `20 Quantitative Thresholds`
16. `22 Final Acceptance Report`

主因不是規則缺失，而是缺乏持續寫入的 telemetry 與 reconciliation data。

### B. 已有局部實作，但需要 scenario harness

這些章節已有基本執行模型或 gate，但缺少可重複驗證的異常測試框架：

1. `6.2 Unknown Order State`
2. `9.1 Network Abnormality`
3. `9.2 Market Abnormality`
4. `9.3 Program Abnormality`
5. `10.2 Position Risk`
6. `10.3 Loss Risk`
7. `10.4 Kill Switch`

### C. 需要完整資料治理與流程治理

這些章節不是單點功能，而是整套流程控制：

1. `2.2 Shared architecture between paper and live`
2. `14 Shadow Trading`
3. `17 API Security`
4. `21 Conditions That Prohibit Live Trading`

## 開發原則

1. **沿用現有模組，不重寫架構**
   - 核心仍以 `paper_acceptance.py` / `paper_execution.py` / `paper_acceptance_store.py` 為中心。
2. **Observed evidence 優先**
   - 能從系統自動蒐集的，不依賴 manual override。
3. **Scenario 必須可重放**
   - 異常測試不能只靠人點 UI，要能用 pytest 或 fixture 重放。
4. **Workspace 不是假資料填空器**
   - manual override 只補證據與治理訊號，不能掩蓋系統未實作。
5. **先建立資料面，再加更嚴格結論**
   - 沒有可追溯資料時，先標 unavailable / partial，而不是直接誤判 fail/pass。

## 分階段 Roadmap

## Phase 1: Telemetry 與 Observed Evidence 基礎化

### 目標

把目前大量依賴 manual override 的 acceptance gate，改成可由系統自動寫入 evidence。

### 交付

1. 新增 acceptance telemetry schema
   - 建議新增表：
     - `paper_acceptance_runtime_metrics`
     - `paper_acceptance_reconciliation_runs`
     - `paper_acceptance_order_audit`
     - `paper_acceptance_alert_deliveries`
2. 為以下指標建立結構化資料來源：
   - API latency
   - API error rate
   - WebSocket reconnect count
   - reconciliation diff count / severity
   - order timeout / reject / partial fill ratio
   - slippage aggregates
   - runtime days / restart count / major error count
3. `build_smc_acceptance_context(...)` 改為優先讀 telemetry 表，而非只讀 journal summary。

### 涉及檔案

1. `tradingagents/web/paper_acceptance_store.py`
2. `tradingagents/web/app.py`
3. 新增建議：
   - `tradingagents/web/paper_acceptance_metrics.py`

### 驗收

1. `8.1 / 8.2 / 8.3 / 11.2 / 12.2` 至少有 70% check 轉成 observed。
2. 無需手動輸入即可產生 latency / error / reconnect / fill quality 指標。

## Phase 2: Reconciliation 與 Order Audit 完整化

### 目標

把 `6.x` 與 `7.2` 從規則描述，提升為真實可追蹤的 order/account audit flow。

### 交付

1. 建立 order lifecycle audit log
   - order id / client order id / strategy version / parameter version / signal source / submit ts / ack ts / fill ts / cancel ts
2. 建立 reconciliation run model
   - compare local order / position / balance / trade
   - diff severity
   - auto suspend recommendation
   - restoration result
3. 未知訂單狀態處理與 reconciliation event 串接
4. UI 補一個 reconciliation evidence 視圖

### 涉及檔案

1. `tradingagents/web/paper_execution.py`
2. `tradingagents/web/paper_acceptance_store.py`
3. `tradingagents/web/templates/index.html`

### 驗收

1. `6.1 / 6.2 / 7.1 / 7.2` 關鍵 gate 能從真實 audit data 判定。
2. 未知狀態、部分成交、取消失敗、對帳差異都有事件紀錄與修正流程。

## Phase 3: Abnormal Scenario Harness

### 目標

讓 `9.x`、`10.x` 的條款有可重播的 scenario 測試，不再只靠口頭聲明「已測過」。

### 交付

1. 建立 scenario runner
   - network outage
   - REST timeout
   - duplicate data
   - out-of-order data
   - sudden spread widening
   - depth disappearance
   - strategy crash
   - DB write failure
   - bad parameters
   - delisted / non-tradable symbol
2. 每個 scenario 寫入：
   - triggered_at
   - expected behavior
   - actual behavior
   - suspend status
   - reconciliation result
   - regression status
3. 建立 kill switch / loss limit / position limit 的 acceptance test matrix。

### 涉及檔案

建議新增：

1. `tradingagents/web/paper_acceptance_scenarios.py`
2. `tradingagents/tests/test_paper_acceptance_scenarios.py`

### 驗收

1. `9.1 / 9.2 / 9.3 / 10.2 / 10.3 / 10.4` 的重點條款都有可自動重跑的測試。
2. scenario 結果可回寫 acceptance workspace。

## Phase 4: Monitoring / Alerting / Governance Workflow

### 目標

把 acceptance 從單一頁面升級成治理流程。

### 交付

1. SMC acceptance workspace 增加：
   - 篩選 `framework / observed / manual`
   - 僅看 blockers
   - 最近異常事件時間軸
   - 章節完成度趨勢
2. 新增 acceptance report governance metadata
   - reviewer
   - review_status
   - fixed_in_version
   - retest_required
   - can_promote_to_live
3. 將 report 與 abnormal events 關聯化
4. 支援重新生成 acceptance run 時保留歷史審閱紀錄

### 涉及檔案

1. `tradingagents/web/paper_acceptance_store.py`
2. `tradingagents/web/templates/index.html`
3. `tradingagents/web/app.py`

### 驗收

1. `11.1 / 11.2 / 21 / 22.8 / 22.9` 不只出現在報告中，也能成為日常治理流程的一部分。

## Phase 5: Backtest / Paper / Live Deviation Pipeline

### 目標

完整打通 `13 / 14 / 18 / 19 / 20`，讓 acceptance 結論不只看 paper，而是看「research -> paper -> small-live」的偏差。

### 交付

1. backtest baseline normalization
   - 定義可比較的 metrics contract
2. shadow trading evidence contract
3. paper vs live comparison table 自動產生
4. deviation threshold policy
   - win rate delta
   - fill rate delta
   - slippage delta
   - drawdown delta
   - holding time delta
5. capacity stage evidence
   - stage 0 paper
   - stage 1 1%-5%
   - stage 2 10%-20%
   - stage 3 25%-50%
   - stage 4 full

### 涉及檔案

建議新增：

1. `tradingagents/web/paper_acceptance_policy.py`
2. `tradingagents/tests/test_paper_acceptance_policy.py`

### 驗收

1. `13 / 14 / 18 / 19 / 20` 可由資料驅動判定，而不是只靠人工說明。
2. 有明確 deviation thresholds 後，才能真正支持 `conditionally_passed` 與 promotion freeze。

## Phase 6: Security / Research Discipline / Promotion Gate 收斂

### 目標

補齊 `16 / 17 / 21` 這些偏治理性的條款，讓 system 可以拒絕不合格 promotion。

### 交付

1. 策略版本、參數版本、執行模型版本與 report 綁定
2. parameter change / override change event log
3. API key hygiene 自動掃描結果接入 acceptance
   - env only
   - no hardcoded secret
   - test/live separation
   - revocation playbook presence
4. promotion gate endpoint
   - `/api/paper-acceptance/promotion-check`
   - 明確回傳 allow / deny / conditional

### 驗收

1. `16 / 17 / 21` 可被系統正式執行，而不是停留在 README / 文件。

## 測試規劃

## 1. 單元測試

持續擴充：

1. `test_paper_acceptance.py`
2. `test_paper_execution.py`
3. `test_paper_acceptance_store.py`
4. `test_paper_acceptance_api.py`

新增：

1. `test_paper_acceptance_scenarios.py`
2. `test_paper_acceptance_policy.py`
3. `test_paper_acceptance_metrics.py`

## 2. 整合測試

目標是驗證完整資料流：

1. 寫入 journal
2. 寫入 event / reconciliation / telemetry
3. 重建 workspace
4. 產生 report
5. promotion gate 判定

## 3. UI 驗證

每個大階段完成後至少驗：

1. `SMC 前測驗收` 頁面載入
2. section 切換
3. check 覆寫
4. override 儲存
5. report refresh
6. blockers / events / reports 正確更新

## 章節對應的最終完成定義

當以下條件都成立，才可說「完整覆蓋標準文件」：

1. 每個 gate 至少有以下之一：
   - deterministic framework evidence
   - observed telemetry evidence
   - manual evidence with reviewer trail
2. `9.x` 與 `10.x` 都有可重放 scenario test。
3. `19` 有 paper/live comparison contract，而不是 placeholder。
4. `22` 的每個段落都由結構化資料生成，不靠自由文字拼湊。
5. promotion decision 可由系統輸出，且能追溯到具體 blockers。

## 實作順序建議（更新）

目前 Phase 1~5 已有大部分骨架與第一輪閉環，因此後續順序不應再從 telemetry 重新開始，而應改成下面這個收斂順序：

1. `2.2 / 14` shared runtime + shadow parity
2. `15 / 16` sample regime coverage + research discipline taxonomy
3. `18 / 19 / 20` capital promotion workflow + quantitative threshold calibration
4. `22 / 23` final rationale stitching + production acceptance closure

原因很直接：

- telemetry / reconciliation / scenarios / governance 已經有了，現在最大的風險不是沒有資料，而是 live promotion contract 還沒有完全落地。
- 若 `shadow parity` 與 `capital promotion workflow` 沒補齊，`19 / 20 / 22.9 / 23` 仍然只能算 partial。
- acceptance 系統現在能找出缺口，但還需要把「為什麼能升級 / 為什麼不能升級」變成可追溯的 production decision。

## 下一輪開發工作包

### Workstream A: Shared Runtime / Shadow Parity

1. 定義 paper / shadow / live 共用 execution intent schema。
2. 為 shadow trading 增加 parity trace：
   - market data timestamp
   - signal timestamp
   - order intent timestamp
   - risk decision timestamp
   - execution adapter output
3. 新增 `shadow parity summary` 到 acceptance workspace / report / policy。

### Workstream B: Sample Regime Coverage

1. 定義 regime buckets：
   - low vol / high vol
   - trend / range
   - liquid / thin
   - session buckets
2. 從 journal / telemetry 聚合 regime coverage matrix。
3. 將 `15` 的 passing rule 從單純 trade count，提升成 count + regime coverage。

### Workstream C: Capital Promotion Workflow

1. 將現有 `capital stage snapshots` 提升成 `promotion ladder`：
   - current stage
   - required thresholds
   - blocker deltas
   - promotion-ready boolean
2. 為 `19 / 20 / 22.9` 增加 deterministic explanation：
   - which metric failed
   - threshold value
   - compared value
   - source row / snapshot
3. 補 API 與 UI，讓 reviewer 能直接看 promotion rationale。

### Workstream D: Final Acceptance Closure

1. 補 `22.4 / 22.6 / 22.8 / 22.9` 的報告拼接欄位。
2. 將 `23 Final Acceptance Principles` 映射成最終 production gate checklist。
3. 補 end-to-end 測試：research -> paper -> acceptance -> promotion-check -> deny/conditional/allow。

## 近期執行建議（更新）

下一個開發段不該再回頭做 telemetry 基礎建設，而應直接做 **Workstream A + C 的最小閉環**：

1. 定義 shared execution intent schema
2. 補 shadow parity trace table / summary builder
3. 將 parity summary 接入 workspace / report / promotion check
4. 在 capital promotion workflow 顯示 stage threshold 與 blocker delta
5. 新增對應 pytest 與 API 測試

這樣做完，這條分支的 acceptance framework 就會從「有證據的驗收系統」再升級成「能約束實際升級決策的 promotion system」。
