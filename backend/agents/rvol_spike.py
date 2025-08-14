from typing import Optional
from ..bars import build_bars, rvol_ratio
from ..signals import compute_signals_tf
from ..config import ALERT_MIN_RVOL as _AMR
from .base import Agent

class RVOLSpikeAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("rvol_spike", interval_sec or 30)
        self.min_rvol = float(_AMR)  # <= enforce float

    async def run_once(self, symbol: str) -> Optional[dict]:
        bars_1m = build_bars(symbol, tf="1m", lookback_min=120)
        if len(bars_1m) < 30:
            return None
        rvr = float(rvol_ratio(bars_1m, win=5, baseline=20) or 0.0)  # <= enforce float
        if rvr >= self.min_rvol:
            tf = compute_signals_tf(symbol, ["1m","5m"])
            return {
                "score": min(10.0, round(5.0 + (rvr - self.min_rvol), 2)),
                "label": "rvol_spike",
                "details": {
                    "rvol_1m5_vs20": round(rvr, 2),
                    "min_rvol": float(self.min_rvol),
                    "mom_bps_1m": round(float(tf.get("mom_bps_1m", 0.0) or 0.0), 1),
                    "px_vs_vwap_bps_1m": round(float(tf.get("px_vs_vwap_bps_1m", 0.0) or 0.0), 1),
                },
            }
        return None
