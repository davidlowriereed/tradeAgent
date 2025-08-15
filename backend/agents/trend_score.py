
from __future__ import annotations
import math
from typing import Optional
from ..signals import compute_signals
from .base import Agent

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

class TrendScoreAgent(Agent):
    name = "trend_score"
    def __init__(self, interval_sec: int = 15):
        super().__init__(self.name, interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        s = compute_signals(symbol)
        mom1 = float(s.get("mom1_bps", 0.0))
        rvol = float(s.get("rvol_vs_recent", 1.0))
        vwap = float(s.get("px_vs_vwap_bps", 0.0))

        # simple blended probability from features
        p1 = _clamp01(0.5 + mom1/800.0)
        p2 = _clamp01(0.5 + (rvol - 1.0) * 0.15)
        p3 = _clamp01(0.5 + vwap/400.0)
        p_up = _clamp01((p1 + p2 + p3) / 3.0)

        return {
            "score": round(10.0 * p_up, 2),
            "label": "trend_score",
            "details": {"p_up": p_up, "p_mom": p1, "p_rvol": p2, "p_vwap": p3}
        }
