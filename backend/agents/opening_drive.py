
# backend/agents/opening_drive.py
from typing import Optional
from ..bars import build_bars, atr
from .base import Agent

def _find_opening_range(bars, start_idx=0, length=45):
    # Assumes bars aligned to 1m and stream provides session clipping elsewhere
    return bars[start_idx:start_idx+length]

class OpeningDriveReversalAgent(Agent):
    def __init__(self, name="OpeningDrive", interval_sec=60):
        super().__init__(name=name, interval_sec=interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        # Placeholder: implement session clock for equities later
        bars = build_bars(symbol, tf="1m", lookback_min=120)
        if len(bars) < 60: 
            return None
        # Dummy opening range using first 15 bars in window
        or_bars = _find_opening_range(bars, start_idx=max(0,len(bars)-60), length=15)
        if not or_bars:
            return None
        # Simple failure condition placeholder
        a = atr(bars[-60:], period=14)
        if a > 0 and abs(bars[-1]["c"] - bars[-2]["c"]) > 1.2*a:
            return {"score": 6.0, "label": "open_drive_reversal", "details": {"note": "impulse vs OR; prototype"}}
        return None
