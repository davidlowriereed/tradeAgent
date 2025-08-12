
from typing import Optional
from ..signals import compute_signals
from ..config import ALERT_MIN_RVOL
from .base import Agent

class RVOLSpikeAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("rvol_spike", interval_sec or 30)
        self.min_rvol = ALERT_MIN_RVOL

    async def run_once(self, symbol: str) -> Optional[dict]:
        sig = compute_signals(symbol)
        rvol = sig.get("rvol_vs_recent") or 0.0
        if rvol >= self.min_rvol:
            return {
                "score": min(10.0, rvol),
                "label": "rvol_spike",
                "details": {
                    "rvol": rvol,
                    "volume_5m": sig.get("volume_5m"),
                    "best_bid": sig.get("best_bid"),
                    "best_ask": sig.get("best_ask"),
                },
            }
        return None
