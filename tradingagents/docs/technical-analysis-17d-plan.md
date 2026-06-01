# 17 維全景技術分析矩陣建置規劃

## 目標

將 `Institutional-Grade Technical Analysis: The 17-Dimensional Panoramic Observation & Interpretation Matrix.md` 定義的方法落地為可計算、可繪圖、可追蹤、可逐步擴充的技術分析系統。

原則：

1. 17 個維度完整保留，不簡化、不合併。
2. 每個維度都要回傳獨立狀態：`computed`、`partial`、`unavailable`。
3. 不可取得的資料不得用錯誤代理資料偽裝，必須明確列為 `data_gaps`。
4. 圖表只繪製 marker，不在前端重新判斷技術條件。
5. Obsidian 保存方法論、設計決策、資料缺口與後續迭代記錄。
6. `.skill` 保存代理人執行此方法時的標準流程。

## 17 維資料與實作表

| # | 維度 | 現階段資料 | 現階段實作 | 後續資料需求 |
|---|------|------------|------------|--------------|
| I | Price Action & Candlestick | OHLCV | 實體/影線比例、吞噬、長影線 rejection、gap、liquidity sweep | 更細 tick 以驗證 stop hunt |
| II | Trend & MA Systems | OHLCV | MA5/10/20/50/60/200/240、cross、Bollinger squeeze/walk | 無 |
| III | Volume & Market Profile | OHLCV | Volume MA、量能異常、effort/result、近似 VPVR POC/VAH/VAL | 真實 intrabar volume profile |
| IV | Momentum & Oscillators | OHLCV | RSI、MACD、Stochastic、regular divergence、embedding | 無 |
| V | Market Structure & Geometry | OHLCV | swing pivot、support/resistance、BOS、Fib retracement/extension、golden pocket | 更完整 trendline/channel fitting |
| VI | Volatility & Risk | OHLCV | ATR14、volatility expansion/compression、2/3 ATR stop | 無 |
| VII | Multi-Timeframe & Derivatives | OHLCV + optional derivatives | 日/週趨勢對齊；衍生品缺口明示 | OI、funding、IV |
| VIII | Microstructure & Order Flow | optional order flow | 無資料時輸出 unavailable | footprint、CVD、liquidation heatmap |
| IX | Intermarket & Correlation | benchmark close | 20D alpha、60D correlation | sector/ratio universe |
| X | Breadth & Internals | optional breadth | 無資料時輸出 unavailable | A/D line、above 50MA/200MA |
| XI | Time & Cyclical | OHLCV + optional anchors | range-low AVWAP、anchor marker | intraday opening range、session calendar |
| XII | Advanced Geometries | OHLCV | FVG、harmonic pivot candidate | strict harmonic classifier |
| XIII | Options & GEX | optional options profile | 無資料時輸出 unavailable | gamma by strike、expiration chain |
| XIV | Depth of Market | optional order book | 無資料時輸出 unavailable | Level 2/3、bookmap snapshots |
| XV | Statistical Mechanics | OHLCV | regression channel、z-score、tail-risk marker | regime-aware regression windows |
| XVI | Macro Wave | OHLCV | Wyckoff phase heuristic、pivot count | deterministic Elliott labeling |
| XVII | Event Timeline | optional events | 事件 marker payload | earnings/macro calendar feed |

## 工作流

1. **Data Ingestion**
   - 主要資料：`fetch_history(symbol, period)`。
   - 基準資料：台股用 `^TWII`，美股用 `^GSPC`。
   - 外部資料：options、order book、breadth、events 先以 optional payload 形式保留入口。

2. **Normalization**
   - 清理 OHLC 必要欄位。
   - 補 `Volume=0` 但不把缺量能視為真實量能。
   - index 統一為 timezone-naive datetime。

3. **Dimension Calculation**
   - `technical_matrix.build_technical_matrix(...)` 順序執行 17 維。
   - 每一維輸出 `observations`、`metrics`、`levels`、`markers`、`data_gaps`。
   - 每一維額外輸出 `score`、`bias`、`confidence`、`severity`、`signals`，由後端統一產生，不交給前端推論。

4. **Interaction Layer**
   - Confluence Zone：structure/Fib/FVG/VPVR/AVWAP/statistical levels 在 1% 內聚合。
   - Trend-Momentum Confirmation：MA alignment 需被 RSI/MACD 狀態確認。
   - Effort-Result Reversal Risk：量能異常與小實體 K 線可以否定價格突破。
   - Macro-to-Execution Alignment：週線/日線方向未對齊時，不提升短線 marker 權重。
   - Data Availability Guardrail：資料不足維度明示缺口，不用替代資料假裝完成。

5. **API**
   - `GET /api/technical-matrix/{symbol}?period=1y`
   - 回傳完整矩陣與 Lightweight Charts marker payload。
   - `POST /api/technical-matrix/{symbol}/snapshot?period=1y`
   - 將同一份矩陣保存為 Obsidian 快照，並更新個股技術矩陣入口與總覽。

6. **Chart Rendering**
   - 診斷視窗 K 線圖仍由 `/api/history/{symbol}` 提供 OHLC/MA/volume。
   - 圖表 marker 從 `/api/technical-matrix/{symbol}` 取得。
   - 前端不重新計算技術條件。
   - 診斷視窗提供 17 維面板、維度 marker 篩選、矩陣摘要、執行計畫候選與 Obsidian 快照保存。

7. **Knowledge Management**
   - 專案設計文件：`tradingagents/docs/technical-analysis-17d-plan.md`。
   - Obsidian 方法入口：`TechnicalAnalysis/17維全景技術分析建置規劃.md`。
   - `.skill`：`institutional-technical-analysis`，規範代理人使用流程。

## 當前落地範圍

已建置：

1. `technical_matrix.py`
2. `/api/technical-matrix/{symbol}`（HTTP 4xx/5xx 錯誤碼 + 5 分鐘 TTL cache）
3. `/api/technical-matrix/{symbol}/snapshot`（snapshot 強制 bypass cache）
4. K 線圖 marker 繪製與維度篩選
5. 17 維矩陣詳情面板（執行計畫進場/停損/停利分組顯示）
6. Obsidian 技術矩陣快照與索引
7. 單元測試（含 LVN/ChoCh/Gap/Wyckoff/Marker 5 項新行為驗證）
8. 設計文件與 skill 管理入口

方法論增補（對齊設計檔）：

- **I Price Action**：Gap 細分 Breakaway / Runaway / Exhaustion；Hammer 與 Shooting Star/Pin Bar 從泛 rejection 中拆出。
- **II Trend & MA**：補 EMA12/20/26/50 與 EMA12/26 cross 觀察，與 SMA 並列出 metrics + levels。
- **III Volume Profile**：LVN 取值域內最低量箱（包含 0 量箱）並以「離 busy bin 距離」做 tie-break，真正反映價格真空。
- **V Structure**：BOS 與 ChoCh 分流（前一段同向結構被首次反向破壞時 ChoCh）；補平行通道（取支撐/壓力平均斜率）；補 Head & Shoulders、Inverse H&S、Double Top/Bottom 命名形態辨識。
- **VII MTF**：接 yfinance 1H/15M intraday，輸出 `intraday_1h_direction`、`intraday_15m_direction` 與 macro-vs-micro 對齊觀察；當 derivatives payload 提供時偵測 Short Squeeze（3D +6% + funding ≤ -2%）與 OI Accumulation（OI ≥ +20% + ATR ≤ 30th percentile）。
- **VIII Microstructure**：當 payload 提供 `price_series` + `cvd_series` 時，比對半段 HH/LL 偵測 CVD 發散。
- **IX Intermarket**：抓多 benchmark（^TWII/^GSPC/DX-Y.NYB/^TNX/^VIX/XLK/XLV），輸出每一檔的 20D alpha；XLK/XLV ratio 解析 Risk-On/Risk-Off；VIX 提供 macro stress 觀察。
- **XI Time**：用當日 1m/5m intraday 計算 30 分鐘 Opening Range high/low 與 cleared direction。
- **XII Advanced**：harmonic 容差收緊到 5%、ABCD 全四 ratio 同時驗證；FVG fill 在現價回到歷史 FVG 區間時觸發 marker + observation。
- **XV Statistical**：±2σ marker 保留 + 補 ±3σ 嚴重 tail-risk marker / observation。
- **XVI Macro Wave**：Wyckoff 區分 accumulation / distribution；新增 Wave 3 Extension 候選辨識（5 樞紐中最大幅 leg 達次幅 1.6× 且方向與 phase 一致）。
- **XVII Event**：當日 5m intraday 可用時，偵測 Data-Driven Whipsaw（intraday range ≥ 3× 日平均 + 至少 3 次方向反轉），事件 payload 缺也能 fallback 報出 untagged whipsaw。
- **Enrichment**：Marker 方向計分箭頭形狀走 position-based；圓形/方形回退到顯式 text bias 對照表；Wave3 候選注入 macro_wave score。

仍需外部 feed（已誠實標 unavailable + data_gaps）：

1. **Microstructure**：tick prints / aggressive buy-sell classification / CVD 完整序列 / liquidation heatmap
2. **Breadth**：A/D Line、% above 50MA/200MA 真實計算需要 index 成分股
3. **Options & GEX**：完整選擇權鏈、gamma by strike、expiration calendar
4. **Order Book**：Level 2/3 snapshots、historical resting liquidity
5. **Event Calendar**：earnings dates、CPI/FOMC/NFP 時間戳
6. **True intrabar VPVR**（用 close-price approximation 中）
