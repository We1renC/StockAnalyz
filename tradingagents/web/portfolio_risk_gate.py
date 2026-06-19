import logging
from typing import List, Dict, Tuple

_logger = logging.getLogger(__name__)

CORRELATION_MATRIX = {
    frozenset(["BTC-USDT", "ETH-USDT"]): 0.90,
    frozenset(["ETH-USDT", "SOL-USDT"]): 0.75,
    frozenset(["BTC-USDT", "SOL-USDT"]): 0.70,
}

class PortfolioRiskGate:
    MAX_TOTAL_NOTIONAL_PCT = 0.30       # 總持倉 ≤ 淨值 30%
    MAX_SAME_DIRECTION_CORR_PCT = 0.15   # 高相關同向 ≤ 15%
    CORRELATION_THRESHOLD = 0.70         # 視為高相關的閾值

    def get_correlation(self, sym1: str, sym2: str) -> float:
        if sym1 == sym2:
            return 1.0
        # Normalize keys (e.g. BTCUSDT -> BTC-USDT or keep as is)
        s1 = sym1.upper().replace("USDT", "-USDT") if "-" not in sym1 else sym1.upper()
        s2 = sym2.upper().replace("USDT", "-USDT") if "-" not in sym2 else sym2.upper()
        key = frozenset([s1, s2])
        return CORRELATION_MATRIX.get(key, 0.0)

    def check_new_position(
        self,
        new_symbol: str,
        new_direction: int, # 1 for long, -1 for short
        new_notional: float,
        open_positions: List[Dict], # List of dict: {"symbol": str, "direction": int, "notional": float}
        equity: float
    ) -> Tuple[bool, str]:
        if equity <= 0:
            return False, "Equity is zero or negative"

        # 1. Total exposure check
        current_total_notional = sum(p["notional"] for p in open_positions)
        proposed_total_notional = current_total_notional + new_notional
        total_ratio = proposed_total_notional / equity
        if total_ratio > self.MAX_TOTAL_NOTIONAL_PCT:
            return False, f"Total exposure {total_ratio:.1%} exceeds limit of {self.MAX_TOTAL_NOTIONAL_PCT:.1%} (notional: {proposed_total_notional:.2f} / equity: {equity:.2f})"

        # 2. Correlation exposure check (high correlation same direction)
        # Find all open positions in the same direction that are highly correlated with the new symbol
        correlated_notional = new_notional
        for p in open_positions:
            if p["direction"] == new_direction:
                corr = self.get_correlation(new_symbol, p["symbol"])
                if corr >= self.CORRELATION_THRESHOLD:
                    correlated_notional += p["notional"]

        corr_ratio = correlated_notional / equity
        if corr_ratio > self.MAX_SAME_DIRECTION_CORR_PCT:
            return False, f"Correlated exposure in direction {new_direction} for {new_symbol} is {corr_ratio:.1%}, exceeding limit of {self.MAX_SAME_DIRECTION_CORR_PCT:.1%} (notional: {correlated_notional:.2f} / equity: {equity:.2f})"

        return True, "ok"
