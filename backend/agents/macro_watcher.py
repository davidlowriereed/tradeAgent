import os
import time
import datetime as dt
from typing import List, Tuple, Optional

from backend.agents.base import Agent
from backend.signals import compute_signals  # uses your existing signal calc


def _parse_hhmm_list(raw: str) -> List[Tuple[int, int]]:
    """
    Parse '13:30,14:00,18:00' -> [(13,30),(14,0),(18,0)].
    Ignores malformed entries.
    """
    out: List[Tuple[int, int]] = []
    for part in (raw or "").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            hh, mm = p.split(":")
            h = int(hh)
            m = int(mm)
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
        except Exception:
            continue
    return out


def _is_within_window_utc(now_ts: float, windows_hhmm: List[Tuple[int, int]], radius_min: int) -> Optional[str]:
    """
    Return the matching HH:MM (as 'HH:MMZ') if now is within ±radius_min of a configured window (UTC), else None.
    """
    if not windows_hhmm:
        return None
    now = dt.datetime.utcfromtimestamp(now_ts)
    for hh, mm in windows_hhmm:
        anchor = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # handle crossing midnight edges: check yesterday/today/tomorrow anchors
        for day_offset in (-1, 0, 1):
            a = anchor + dt.timedelta(days=day_offset)
            if abs((now - a).total_seconds()) <= radius_min * 60:
                return f"{hh:02d}:{mm:02d}Z"
    return None


class MacroWatcherAgent(Agent):
    """
    Lightweight macro-time-window watcher.

    - Reads UTC HH:MM windows from env MACRO_WINDOWS (e.g. '12:30,14:00,18:00').
      Defaults include common release slots: '12:30,14:00' (CPI/NFP/FOMC windows vary by DST; tune as you like).
    - Marks a 'macro_window' finding when now is inside ±MACRO_WINDOW_MINUTES (default 10) AND
      rvol_vs_recent >= MACRO_RVOL_MIN (default 1.3).
    - Always okay to run; returns None when nothing notable, so it won't spam.
    """

    def __init__(self, interval_sec: Optional[int] = None):
        poll = int(os.getenv("MACRO_INTERVAL_SEC", "60"))
        super().__init__("macro_watcher", interval_sec or poll)

        # Configurable windows & thresholds
        default_windows = os.getenv("MACRO_WINDOWS", "12:30,14:00")
        self.windows_hhmm = _parse_hhmm_list(default_windows)
        self.window_radius_min = int(os.getenv("MACRO_WINDOW_MINUTES", "10"))
        self.min_rvol = float(os.getenv("MACRO_RVOL_MIN", "1.3"))

    async def run_once(self, symbol: str) -> Optional[dict]:
        now_ts = time.time()

        # Check macro window proximity (UTC)
        hit = _is_within_window_utc(now_ts, self.windows_hhmm, self.window_radius_min)

        # Always compute signals; we gate on rvol when in a window
        sig = compute_signals(symbol)
        rvol = float(sig.get("rvol_vs_recent") or 0.0)
        vol_5m = float(sig.get("volume_5m") or 0.0)
        bid = sig.get("best_bid")
        ask = sig.get("best_ask")

        if hit and rvol >= self.min_rvol:
            # Simple score: emphasize RVOL; cap at 10
            score = max(0.0, min(10.0, (rvol - 1.0) * 6.0))
            return {
                "score": score,
                "label": "macro_window",
                "details": {
                    "symbol": symbol,
                    "window_utc": hit,
                    "rvol_vs_recent": rvol,
                    "volume_5m": vol_5m,
                    "best_bid": bid,
                    "best_ask": ask,
                    "window_radius_min": self.window_radius_min,
                    "min_rvol": self.min_rvol,
                },
            }

        # No notable macro condition -> no finding (scheduler still records heartbeat)
        return None
