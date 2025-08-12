
from typing import Optional
import time
from ..state import trades
from ..signals import _dcvd
from ..config import ALERT_CVD_DELTA
from .base import Agent

class CvdDivergenceAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("cvd_divergence", interval_sec or 30)
        self.min_delta = ALERT_CVD_DELTA

    async def run_once(self, symbol: str) -> Optional[dict]:
        now = time.time()
        w5 = [r for r in trades[symbol] if r[0] >= now - 300]
        d5 = _dcvd(w5)
        sc = abs(d5) / max(1.0, self.min_delta)
        if sc >= 1.0:
            return {"score": min(10.0, 10.0*sc), "label": "cvd_divergence", "details": {"delta_5m": d5}}
        return None
