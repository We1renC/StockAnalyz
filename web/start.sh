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

# 啟動服務
echo ""
echo "🚀 啟動 TradingAgents Dashboard"
echo "─────────────────────────────────────"
echo "  網址：http://localhost:8765"
echo "  停止：Ctrl+C"
echo "─────────────────────────────────────"
echo ""

cd "$DIR"
"$VENV/bin/python" app.py
