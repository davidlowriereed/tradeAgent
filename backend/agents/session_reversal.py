
# backend/agents/session_reversal.py
import time
from typing import Optional
from ..bars import build_bars, atr, px_vs_vwap_bps
from ..config import TS_INTERVAL
from .base import Agent

def _impulse_failed_ohlc(bars, atr_mult=1.2, fail_closes=3):
    if len(bars) < 20:
        return False
    recent = bars[-20:]
    a = atr(recent, period=14)
    if a <= 0:
        return False
    # impulse = last move vs prior close exceeds threshold
    c_now = recent[-1]["c"]; c_prev = recent[-2]["c"]
    impulse = abs(c_now - c_prev) >= atr_mult * a
    if not impulse:
        return False
    # failure: last N closes fail to make new extremes
    highs = [b["c"] for b in recent]
    if c_now >= max(highs):
        return False
    # simplistic failure test
    return True

class SessionReversalAgent(Agent):
    def __init__(self, name="SessionReversal", interval_sec=60):
        super().__init__(name=name, interval_sec=interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        bars = build_bars(symbol, tf="1m", lookback_min=120)
        if not bars:
            return None
        if _impulse_failed_ohlc(bars):
            score = 6.5
            return {"score": score, "label": "session_reversal",
                    "details": {"note": "1m impulse failed; VWAP confluence preferred"}}
        return None
