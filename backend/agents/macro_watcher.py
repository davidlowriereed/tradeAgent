import os, time, datetime as dt
from typing import List, Tuple, Optional
from .base import Agent
from ..signals import compute_signals
from ..config import ALERT_MIN_RVOL
from ..liquidity import get_liquidity_state

def _parse_hhmm_list(raw: str) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            out.append((int(hh), int(mm)))
        except Exception:
            continue
    return out

def _is_within_window_utc(now_ts: float, windows_hhmm: List[Tuple[int, int]], radius_min: int) -> Optional[str]:
    if not windows_hhmm:
        return None
    now = dt.datetime.utcfromtimestamp(now_ts)
    for hh, mm in windows_hhmm:
        anchor = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = abs((now - anchor).total_seconds()) / 60.0
        if delta <= radius_min:
            return f"{hh:02d}:{mm:02d}Z"
    return None

class MacroWatcherAgent(Agent):
    """
    Watches env-driven UTC time windows around macro events and annotates activity.
    Adds liquidity context (risk_on / liquidity_score) to details.
    Env:
      MACRO_WINDOWS_UTC="12:30,14:00"   # HH:MM, comma-separated (UTC)
      MACRO_WINDOW_RADIUS_MIN=10
      MACRO_MIN_RVOL=1.5
    """
    def __init__(self, interval_sec: int | None = None):
        super().__init__("macro_watcher", interval_sec or 60)
        self.windows = _parse_hhmm_list(os.getenv("MACRO_WINDOWS_UTC", ""))
        self.window_radius_min = int(os.getenv("MACRO_WINDOW_RADIUS_MIN", "10"))
        self.min_rvol = float(os.getenv("MACRO_MIN_RVOL", str(ALERT_MIN_RVOL)))

    async def run_once(self, symbol: str) -> Optional[dict]:
        now = time.time()
        hit = _is_within_window_utc(now, self.windows, self.window_radius_min)
        sig = compute_signals(symbol)
        rvol = sig.get("rvol_vs_recent") or 0.0
        vol_5m = sig.get("volume_5m") or 0.0
        bid, ask = sig.get("best_bid"), sig.get("best_ask")
        risk_on, liq_score = get_liquidity_state()

        if hit and rvol >= self.min_rvol:
            return {
                "score": 6.0,
                "label": "macro_window_activity",
                "details": {
                    "symbol": symbol,
                    "window_utc": hit,
                    "rvol_vs_recent": round(rvol,2),
                    "volume_5m": round(vol_5m,2),
                    "best_bid": bid,
                    "best_ask": ask,
                    "window_radius_min": self.window_radius_min,
                    "min_rvol": self.min_rvol,
                    "risk_on": risk_on,
                    "liquidity_score": round(liq_score,2),
                },
            }
        return None
