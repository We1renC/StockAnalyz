#!/usr/bin/env python3
"""MCP server wrapping the TauricResearch/TradingAgents framework."""

import json
import sys
from datetime import date, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "TradingAgents",
    instructions=(
        "Multi-agent LLM trading analysis framework. "
        "Use analyze_stock to run a full fundamental + technical + sentiment + news analysis "
        "and get a Buy/Hold/Sell decision. Analysis may take 1-3 minutes."
    ),
)

ANALYST_DESCRIPTIONS = {
    "market": "Technical analysis — price patterns, moving averages, RSI, MACD",
    "social": "Sentiment analysis — social media signals and market mood",
    "news": "News analysis — macroeconomic events and company news impact",
    "fundamentals": "Fundamental analysis — financials, valuation, earnings",
}

ALL_ANALYSTS = list(ANALYST_DESCRIPTIONS.keys())


def _build_config(
    llm_provider: str,
    deep_think_llm: str,
    quick_think_llm: str,
    max_debate_rounds: int,
    max_risk_discuss_rounds: int,
    output_language: str,
    data_vendor: str,
) -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "llm_provider": llm_provider,
            "deep_think_llm": deep_think_llm,
            "quick_think_llm": quick_think_llm,
            "max_debate_rounds": max_debate_rounds,
            "max_risk_discuss_rounds": max_risk_discuss_rounds,
            "output_language": output_language,
            "data_vendors": {
                "core_stock_apis": data_vendor,
                "technical_indicators": data_vendor,
                "fundamental_data": data_vendor,
                "news_data": data_vendor,
            },
        }
    )
    return config


def _extract_state_summary(final_state: dict) -> dict:
    """Pull key sections out of the LangGraph state dict."""
    summary = {}
    keys_of_interest = [
        "final_trade_decision",
        "trader_proposal",
        "risk_debate_state",
        "investment_debate_state",
        "market_report",
        "sentiment_report",
        "news_report",
        "fundamentals_report",
    ]
    for key in keys_of_interest:
        val = final_state.get(key) if isinstance(final_state, dict) else getattr(final_state, key, None)
        if val:
            summary[key] = str(val)[:2000]  # cap per section
    return summary


@mcp.tool()
def analyze_stock(
    ticker: str,
    trade_date: Optional[str] = None,
    analysts: Optional[list] = None,
    llm_provider: str = "anthropic",
    deep_think_llm: str = "claude-opus-4-7",
    quick_think_llm: str = "claude-haiku-4-5-20251001",
    max_debate_rounds: int = 1,
    max_risk_discuss_rounds: int = 1,
    output_language: str = "English",
    data_vendor: str = "yfinance",
) -> str:
    """Run a full multi-agent trading analysis and return a Buy/Hold/Sell recommendation.

    Spawns a team of specialized AI analysts (technical, sentiment, news, fundamentals),
    a bull/bear researcher debate, a trader proposal, and a risk-manager portfolio decision.

    Args:
        ticker: Stock ticker symbol, e.g. NVDA, AAPL, TSLA, BTC-USD
        trade_date: Analysis date as YYYY-MM-DD. Defaults to yesterday.
        analysts: Subset of analysts to run. Options: market, social, news, fundamentals.
                  Defaults to all four.
        llm_provider: LLM backend — anthropic, openai, google, deepseek, xai. Default: anthropic
        deep_think_llm: Model for deep reasoning (debate, final decision).
                        Default: claude-opus-4-7
        quick_think_llm: Model for fast data extraction tasks.
                         Default: claude-haiku-4-5-20251001
        max_debate_rounds: Bull/bear debate rounds (1–3 recommended). Default: 1
        max_risk_discuss_rounds: Risk committee discussion rounds. Default: 1
        output_language: Language for all reports, e.g. English, Chinese, Spanish. Default: English
        data_vendor: Market data source — yfinance or alpha_vantage. Default: yfinance
    """
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError:
        return json.dumps(
            {
                "error": (
                    "TradingAgents is not installed. "
                    "Run: pip install git+https://github.com/TauricResearch/TradingAgents.git"
                )
            }
        )

    if analysts is None:
        analysts = ALL_ANALYSTS

    invalid = [a for a in analysts if a not in ANALYST_DESCRIPTIONS]
    if invalid:
        return json.dumps(
            {
                "error": f"Unknown analysts: {invalid}. Valid options: {ALL_ANALYSTS}"
            }
        )

    if trade_date is None:
        trade_date = str(date.today() - timedelta(days=1))

    config = _build_config(
        llm_provider=llm_provider,
        deep_think_llm=deep_think_llm,
        quick_think_llm=quick_think_llm,
        max_debate_rounds=max_debate_rounds,
        max_risk_discuss_rounds=max_risk_discuss_rounds,
        output_language=output_language,
        data_vendor=data_vendor,
    )

    try:
        ta = TradingAgentsGraph(
            selected_analysts=analysts,
            debug=False,
            config=config,
        )
        final_state, decision = ta.propagate(ticker, trade_date)
    except Exception as exc:
        return json.dumps({"error": str(exc), "ticker": ticker, "date": trade_date})

    result = {
        "ticker": ticker.upper(),
        "date": trade_date,
        "decision": decision,
        "analysts_used": analysts,
        "llm_provider": llm_provider,
        "models": {
            "deep_think": deep_think_llm,
            "quick_think": quick_think_llm,
        },
        "analysis": _extract_state_summary(final_state),
    }
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def list_analysts() -> str:
    """List all available analyst types and their roles."""
    return json.dumps(
        {
            "analysts": [
                {"name": name, "description": desc}
                for name, desc in ANALYST_DESCRIPTIONS.items()
            ]
        },
        indent=2,
    )


@mcp.tool()
def get_default_config() -> str:
    """Return the current TradingAgents default configuration."""
    try:
        from tradingagents.default_config import DEFAULT_CONFIG

        safe_config = {
            k: v
            for k, v in DEFAULT_CONFIG.items()
            if not k.endswith("_dir") and k != "project_dir"
        }
        return json.dumps(safe_config, indent=2, default=str)
    except ImportError:
        return json.dumps(
            {
                "error": (
                    "TradingAgents not installed. "
                    "Run: pip install git+https://github.com/TauricResearch/TradingAgents.git"
                )
            }
        )


@mcp.tool()
def quick_analyze(ticker: str, trade_date: Optional[str] = None) -> str:
    """Fast analysis using only technical (market) and fundamentals analysts with Claude.

    Skips social/news analysts for speed. Good for a quick directional signal.

    Args:
        ticker: Stock ticker symbol, e.g. NVDA, AAPL
        trade_date: Date as YYYY-MM-DD. Defaults to yesterday.
    """
    return analyze_stock(
        ticker=ticker,
        trade_date=trade_date,
        analysts=["market", "fundamentals"],
        llm_provider="anthropic",
        deep_think_llm="claude-sonnet-4-6",
        quick_think_llm="claude-haiku-4-5-20251001",
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
