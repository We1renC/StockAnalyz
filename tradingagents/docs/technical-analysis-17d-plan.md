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
2. `/api/technical-matrix/{symbol}`
3. `/api/technical-matrix/{symbol}/snapshot`
4. K 線圖 marker 繪製與維度篩選
5. 17 維矩陣詳情面板
6. Obsidian 技術矩陣快照與索引
7. 單元測試
8. 設計文件與 skill 管理入口

仍需下一階段建置：

1. 外部資料 feed 實際接入：options/GEX、breadth、event calendar、order-flow/order-book 目前已保留 payload 入口，尚未接供應商。
2. 更嚴格 harmonic classifier 與 Elliott/Wyckoff 狀態機。
3. Intraday opening range 與 session box 繪製。
4. VPVR 改用真實 intrabar volume distribution。
