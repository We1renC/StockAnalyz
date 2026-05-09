# TradingAgents Dashboard

整合即時股價、技術指標、自動警報、雙 LLM 分析（Analyst → Reviewer 工作流）的個人投資儀表板。

## 功能

- 持倉即時追蹤（5 分鐘自動刷新）損益、RSI、Beta、規則式建議
- 觀察清單按優先度自動排序
- 7 種自動警報規則（進場、停損、超買、跌破均線等）
- K 線圖（蠟燭 + MA20/MA60 + 成交量）
- 多 LLM 工作流：Anthropic Claude / OpenAI GPT-5 / Google Gemini，支援 CLI 與 API 雙模式
- SSE 串流：LLM 分析過程即時顯示進度

## 啟動

```bash
cd web
./start.sh
# 開瀏覽器 http://localhost:8765
```

## 設定您的個人資料

### 1. API Keys（LLM）

複製範例檔並填入 keys：

```bash
cp web/settings.json.example web/settings.json
# 編輯 web/settings.json 填入您的 anthropic / openai / google keys
```

或在儀表板右上角「設定」按鈕透過 UI 填入（推薦）。

> 💚 推薦使用 **CLI 模式**走 Claude Pro / ChatGPT Pro / Gemini Advanced 訂閱，**不另收 API 費**。
> 需先安裝對應 CLI：
> ```bash
> npm install -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli
> ```

### 2. 個人持倉

建立 `web/seed_data_private.py`（已被 .gitignore 排除）：

```python
POSITIONS = [
    # (symbol, name, category, shares, cost, currency)
    ("2330.TW", "台積電", "半導體", 100, 1000.0, "TWD"),
    ("AAPL",    "Apple",  "科技",     5, 180.0,  "USD"),
]
```

或進入儀表板後用「新增持倉」按鈕互動加入（資料只存本地 SQLite）。

## 安全性

### 不會被 git 追蹤的敏感檔（已配置 .gitignore）

| 檔案 | 為何敏感 |
|------|---------|
| `web/settings.json` | 含您的 API Keys |
| `web/portfolio.db` | 含您的真實持倉、成本、警報歷史 |
| `web/seed_data_private.py` | 含您的真實持倉成本 |
| `.agent-handoff.md` | 工作筆記（可能含 context） |
| `.env*`, `*.key`, `*.pem` | 通用機密格式 |
| `.venv/`, `__pycache__/`, `*.log` | 執行時產生 |

### 部署提醒

- 預設只 bind `127.0.0.1:8765`，**沒做認證**
- 不要直接 expose 到公網（會洩漏您的持倉與 API Key）
- 如需遠端使用，建議用 Cloudflare Tunnel + Access 加上身份驗證

## 架構

```
tradingagents/
├── server.py              # MCP server（Claude Code 用）
├── web/                   # ← 主要的 Web 儀表板
│   ├── app.py             # FastAPI 後端
│   ├── llm_providers.py   # 多 LLM 抽象層
│   ├── seed_data.py       # 範例種子資料（公開）
│   ├── seed_data_private.py  # 個人覆蓋（gitignored）
│   ├── settings.json      # API keys（gitignored）
│   ├── portfolio.db       # SQLite（gitignored）
│   └── templates/index.html  # 單頁前端
└── README.md
```

## License

MIT — 但請注意 yfinance 的使用條款。LLM 呼叫費用自負。
