
from __future__ import annotations
from typing import Optional
from ..signals import compute_signals
from ..config import ALERT_MIN_RVOL
from .base import Agent

class RVOLSpikeAgent(Agent):
    name = "rvol_spike"
    def __init__(self, interval_sec: int = 5):
        super().__init__(self.name, interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        s = compute_signals(symbol)
        rvol = float(s.get("rvol_vs_recent", 1.0))
        if rvol >= float(ALERT_MIN_RVOL):
            return {
                "score": round(rvol, 2),
                "label": "rvol_spike",
                "details": {
                    "rvol": rvol,
                    "best_bid": s.get("best_bid"),
                    "best_ask": s.get("best_ask"),
                    "volume_5m": None
                }
            }
        return None
