#!/usr/bin/env bash
# SMC Dashboard daemon wrapper — launchd entry point (Phase-1 ops hardening).
#
# 與 start.sh 的差異：
#   • 不裝依賴、不 seed（daemon 假設環境已就緒，失敗快、launchd 快重啟）
#   • caffeinate -i 包住 process：運行期間阻止 idle sleep（合蓋仍會睡）
#   • 由 launchd KeepAlive 負責 crash 自動重啟與開機自啟
#
# 安裝（一次性）：
#   cp web/launchd/com.smc.dashboard.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.smc.dashboard.plist
# 停用：
#   launchctl unload ~/Library/LaunchAgents/com.smc.dashboard.plist
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/../.venv"

# SMC runtime 旋鈕（與 start.sh 一致；外部可覆寫）
: "${SMC_AUTOLEARN_ENABLED:=1}"
: "${SMC_AUTOLEARN_SYMBOLS:=BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT}"
export SMC_AUTOLEARN_ENABLED SMC_AUTOLEARN_SYMBOLS

cd "$DIR"
# caffeinate -i: prevent idle sleep while the trading process runs.
exec /usr/bin/caffeinate -i "$VENV/bin/python" app.py
