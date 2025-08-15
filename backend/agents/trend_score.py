
# backend/agents/trend_score.py
import math
from .base import Agent
from ..signals import compute_signals
from ..config import TS_INTERVAL

def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)

class TrendScoreAgent(Agent):
    name = "trend_score"

    def __init__(self, interval_sec: int | None = None):
        super().__init__(self.name, interval_sec or TS_INTERVAL)

    async def run_once(self, symbol: str):
        s = compute_signals(symbol)
        mom1 = float(s.get("mom1_bps", 0.0) or 0.0)
        mom5 = float(s.get("mom5_bps", 0.0) or 0.0)
        vwap = float(s.get("px_vs_vwap_bps", 0.0) or 0.0)
        rvol = float(s.get("rvol_vs_recent", 0.0) or 0.0)

        # Simple, bounded components
        p1   = _clamp01(0.5 + mom1/800.0)                      # ±400 bps => ~±0.5
        p5   = _clamp01(0.5 + mom5/1000.0 + (rvol-1.0)*0.1)    # rvol boost
        pv   = _clamp01(0.5 + vwap/120.0)                      # ±60 bps => ±0.5

        p_up = _clamp01((p1 + p5 + pv) / 3.0)
        return {
            "score": round(10.0 * p_up, 2),
            "label": "trend_score",
            "details": {"p_up": p_up, "p_1m": p1, "p_5m": p5, "p_vwap": pv}
        }
