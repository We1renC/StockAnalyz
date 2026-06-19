from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class SMCConfig:
    swing_length: int = 5
    internal_swing_length: int = 3
    close_break: bool = True
    liquidity_range_percent: float = 0.01
    displacement_atr_mult: float = 1.2
    displacement_body_ratio: float = 0.7
    min_rr: float = 1.5
    entry_threshold: int = 8


DEFAULT_CONFLUENCE_WEIGHTS = {
    "htf_bias_alignment": 2,
    "premium_discount_alignment": 2,
    "unmitigated_ob": 2,
    "unfilled_fvg": 1,
    "liquidity_sweep": 2,
    "ltf_choch": 2,
    "ote_zone": 1,
    "killzone": 1,
    "displacement": 1,
    "unicorn_pattern": 2,
    "smt_divergence_pattern": 2,
    "silver_bullet_pattern": 1,
    "power_of_three_pattern": 1,
    # Crypto-Specific Confluence Factors
    "liquidation_cluster_sweep": 2,
    "oi_squeeze_confirm": 2,
    "cvd_divergence_confirm": 2,
    "extreme_funding_rate": 1,
    "coinbase_premium_alignment": 1,
    "alt_align_btc_bias": 2,
    "cme_gap_hit": 1,
}


def load_yaml_config(filename: str, *, search_paths: Optional[list[str]] = None) -> dict:
    """§M0.3 — load YAML config from one of ``config/markets.yaml`` /
    ``config/strategy.yaml`` and return parsed dict; missing file or
    missing pyyaml → ``{}`` so the in-code defaults still drive behaviour.
    """
    import os
    paths = search_paths or [
        os.path.join(os.path.dirname(__file__), "..", "config", filename),
        os.path.join(os.path.dirname(__file__), "config", filename),
        os.path.join(os.getcwd(), "config", filename),
    ]
    for p in paths:
        ap = os.path.abspath(p)
        if not os.path.exists(ap):
            continue
        try:
            import yaml
        except Exception:
            return {}
        try:
            with open(ap, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def _merge_market_configs(base: dict, overrides: dict) -> dict:
    out = {k: dict(v) for k, v in base.items()}
    for market, cfg in (overrides or {}).items():
        if not isinstance(cfg, dict):
            continue
        merged = dict(out.get(market, {}))
        merged.update(cfg)
        out[market] = merged
    return out


MARKET_CONFIGS = {
    "tw": {
        "timezone": "Asia/Taipei",
        "session": "09:00-13:30",
        "primary_killzone": "09:00-10:00",
        "tick_size": 0.01,
        "daily_price_limit_pct": 10,
        "commission_pct": 0.001425,
        "transaction_tax_pct": 0.003,
        "default_timeframes": {"htf": "1y", "mtf": "6mo", "ltf": "1mo"},
    },
    "us": {
        "timezone": "America/New_York",
        "session": "09:30-16:00",
        "primary_killzone": "09:30-10:00",
        "tick_size": 0.01,
        "daily_price_limit_pct": None,
        "commission_pct": 0.0,
        "transaction_tax_pct": 0.0,
        "default_timeframes": {"htf": "1y", "mtf": "6mo", "ltf": "1mo"},
    },
    "crypto": {
        "timezone": "UTC",
        "session": "24/7",
        "primary_killzone": "London/NY",
        "tick_size": 0.01,
        "daily_price_limit_pct": None,
        "commission_pct": 0.0006,
        "funding_sensitive": True,
        "default_timeframes": {"htf": "1d", "mtf": "4h", "ltf": "15m"},
        "max_leverage": 3,
    },
}


def infer_market(symbol: str) -> str:
    upper = (symbol or "").upper()
    if upper.endswith((".TW", ".TWO")):
        return "tw"
    if any(x in upper for x in ("BTC", "ETH", "USDT", "USD-")) or "/" in upper:
        return "crypto"
    return "us"


def market_config(symbol: str) -> dict:
    return deepcopy(MARKET_CONFIGS[infer_market(symbol)])


def confluence_weights(overrides: Optional[dict[str, int]] = None) -> dict[str, int]:
    weights = dict(DEFAULT_CONFLUENCE_WEIGHTS)
    for key, value in (overrides or {}).items():
        if key in weights:
            weights[key] = int(value)
    return weights
