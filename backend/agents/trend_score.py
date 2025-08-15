
# backend/agents/trend_score.py
from __future__ import annotations
import math, time
from .base import Agent
from ..signals import compute_signals

def _clamp01(x: float) -> float:
    if x < 0.0: 
        return 0.0
    if x > 1.0:
        return 1.0
    return x

class TrendScoreAgent(Agent):
    name = "trend_score"

    def __init__(self, interval_sec: int | None = None):
        # default to 15s unless overridden by caller/config
        super().__init__("trend_score", interval_sec or 15)

    async def run_once(self, symbol: str):
        # Use only public signals; avoid importing private helpers.
        s = compute_signals(symbol)

        mom1 = float(s.get("mom1_bps", 0.0))
        mom5 = float(s.get("mom5_bps", 0.0))
        pxvv = float(s.get("px_vs_vwap_bps", 0.0))
        rvol = float(s.get("rvol_vs_recent", 0.0))

        # Simple bounded probabilities from lightweight features
        # Keep contributions small; all terms are dimensionless and bounded.
        p1 = 0.5 + (mom1 / 800.0) + (0.02 if pxvv > 0 else -0.02)
        p5 = 0.5 + (mom5 / 1000.0) + (0.10 * (rvol - 1.0))

        p_up = _clamp01((p1 + p5) / 2.0)
        details = {
            "p_up": p_up,
            "p_1m": _clamp01(p1),
            "p_5m": _clamp01(p5),
        }
        return {"score": round(10 * p_up, 2), "label": "trend_score", "details": details}
