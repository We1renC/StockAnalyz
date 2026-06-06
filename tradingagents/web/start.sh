#!/usr/bin/env bash
# 啟動 TradingAgents Dashboard
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/../.venv"

# 安裝額外依賴 (FastAPI + Uvicorn + Jinja2)
echo "🔧 確認依賴..."
"$VENV/bin/pip" install -q fastapi uvicorn jinja2 python-multipart

# 初始化資料
if [ ! -f "$DIR/portfolio.db" ]; then
  echo "📦 初始化資料庫..."
  "$VENV/bin/python" "$DIR/seed_data.py"
else
  echo "♻️  使用現有資料庫 ($DIR/portfolio.db)"
  echo "    如需重置請刪除後重跑：rm $DIR/portfolio.db"
fi

# ── SMC 學習子系統 runtime 設定（詳見 web/SMC_OPS.md）──────────────
# 用 := 預設，但尊重外部已設的值（export VAR=... ./start.sh 可覆寫）。
: "${SMC_AUTOLEARN_ENABLED:=1}"          # server-side headless 學習（預設開）
export SMC_AUTOLEARN_ENABLED
# 自動學習的幣種（對齊 dashboard 下拉的 5 個），可外部覆寫。
: "${SMC_AUTOLEARN_SYMBOLS:=BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT}"
export SMC_AUTOLEARN_SYMBOLS
# 以下為選用旋鈕；要啟用就取消註解或在呼叫前 export：
# export SMC_MAINTENANCE_INTERVAL=21600   # 自動維護週期（秒，預設 6h）
# export LOG_LEVEL=INFO
# export DASHBOARD_API_TOKEN=...          # 設定後 API 需帶 X-API-Token（敏感，勿入版控）

# 啟動服務
echo ""
echo "🚀 啟動 TradingAgents Dashboard"
echo "─────────────────────────────────────"
echo "  網址：http://localhost:6500"
echo "  停止：Ctrl+C"
echo "  自動學習 (SMC_AUTOLEARN_ENABLED)：$SMC_AUTOLEARN_ENABLED"
echo "  學習幣種 (SMC_AUTOLEARN_SYMBOLS)：$SMC_AUTOLEARN_SYMBOLS"
echo "─────────────────────────────────────"
echo ""

cd "$DIR"
"$VENV/bin/python" app.py
