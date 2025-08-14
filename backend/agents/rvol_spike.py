from typing import Optional
from ..signals import compute_signals_tf
from ..config import ALERT_MIN_RVOL
from ..bars import build_bars, rvol_ratio
from .base import Agent

class RVOLSpikeAgent(Agent):
    """
    Flags unusual activity when short-window volume is elevated relative to a baseline.
    Now uses bar-built OHLCV (1m/5m) instead of raw trade windows for stability.
    """
    def __init__(self, interval_sec: int | None = None):
        super().__init__("rvol_spike", interval_sec or 30)
        self.min_rvol = ALERT_MIN_RVOL

    async def run_once(self, symbol: str) -> Optional[dict]:
        # Use 1m bars for a 5-bar window vs previous 20 bars as baseline
        bars_1m = build_bars(symbol, tf="1m", lookback_min=120)
        if len(bars_1m) < 30:
            return None
        rvr = rvol_ratio(bars_1m, win=5, baseline=20)
        if rvr >= self.min_rvol:
            tf = compute_signals_tf(symbol, ["1m","5m"])
            return {
                "score": min(10.0, round(5.0 + (rvr - self.min_rvol) * 1.0, 2)),
                "label": "rvol_spike",
                "details": {
                    "rvol_1m5_vs20": round(rvr, 2),
                    "min_rvol": self.min_rvol,
                    "mom_bps_1m": round(tf.get("mom_bps_1m", 0.0), 1),
                    "px_vs_vwap_bps_1m": round(tf.get("px_vs_vwap_bps_1m", 0.0), 1)
                },
            }
        return None
