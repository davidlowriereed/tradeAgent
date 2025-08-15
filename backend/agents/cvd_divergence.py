
from __future__ import annotations
from typing import Optional
from ..state import trades
from .base import Agent
import time

class CvdDivergenceAgent(Agent):
    name = "cvd_divergence"
    def __init__(self, interval_sec: int = 10):
        super().__init__(self.name, interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        now = time.time()
        w2 = [t for t in trades.get(symbol, []) if t[0] >= now - 120]
        w5 = [t for t in trades.get(symbol, []) if t[0] >= now - 300]
        if not w2 or not w5:
            return None
        def cvd(ws):
            d=0.0
            for ts, px, sz, side in ws:
                d += float(sz) * (1.0 if side == "buy" else -1.0)
            return d
        d2 = cvd(w2)
        d5 = cvd(w5)
        # Simple divergence: opposite signs
        if d2 * d5 < 0:
            score = min(10.0, abs(d2)/max(1.0, abs(d5)) * 5.0 + 5.0)
            return {"score": round(score,2), "label": "cvd_divergence", "details": {"cvd_2m": d2, "cvd_5m": d5}}
        return None
