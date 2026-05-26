#!/usr/bin/env bash
# Install TradingAgents MCP server dependencies
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Creating virtual environment..."
python3 -m venv "$SCRIPT_DIR/.venv"

echo "Installing dependencies..."
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip -q
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q

echo "Done. Set your API keys in the environment:"
echo "  export ANTHROPIC_API_KEY=sk-ant-..."
echo "  export ALPHA_VANTAGE_API_KEY=...   # optional, only if using alpha_vantage data vendor"
