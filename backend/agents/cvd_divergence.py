import time
from typing import Optional, List, Dict
from ..bars import build_bars
from ..state import trades as STATE_TRADES
from .base import Agent

def _bucket_trades_to_minutes(rows: List[tuple], lookback_min: int=60) -> Dict[int, float]:
    """
    Return dict minute_bucket -> signed volume (buy as +, sell as -)
    """
    if not rows:
        return {}
    now = time.time()
    start = now - lookback_min*60
    out: Dict[int, float] = {}
    for ts, price, size, side in rows:
        if ts < start:
            continue
        b = int(ts // 60) * 60
        sign = 1.0 if (side or "").lower() == "buy" else -1.0
        out[b] = out.get(b, 0.0) + sign * float(size or 0.0)
    return out

def _cvd_series(rows: List[tuple], lookback_min: int=60) -> List[tuple]:
    by_min = _bucket_trades_to_minutes(rows, lookback_min)
    keys = sorted(by_min.keys())
    cvd = 0.0
    out = []
    for k in keys:
        cvd += by_min[k]
        out.append((k, cvd))
    return out

class CvdDivergenceAgent(Agent):
    """
    Detects simple price/CVD divergences over the last ~30 minutes:
      - Bearish: price makes a higher high while CVD fails to make a higher high
      - Bullish: price makes a lower low while CVD fails to make a lower low
    """
    def __init__(self, interval_sec: int | None = None):
        super().__init__("cvd_divergence", interval_sec or 60)

    async def run_once(self, symbol: str) -> Optional[dict]:
        bars = build_bars(symbol, tf="1m", lookback_min=60)
        if len(bars) < 20:
            return None
        rows = list(STATE_TRADES.get(symbol, []))
        cvd = _cvd_series(rows, lookback_min=60)
        if len(cvd) < 5:
            return None

        # Compare last 15 minutes vs prior 15
        w = bars[-30:]
        b1, b2 = w[:15], w[15:]
        if not b1 or not b2:
            return None

        px_hh_prev, px_hh_now = max(b["h"] for b in b1), max(b["h"] for b in b2)
        px_ll_prev, px_ll_now = min(b["l"] for b in b1), min(b["l"] for b in b2)

        # CVD peaks over similar indices
        cvd_vals = [v for (_, v) in cvd if v is not None]
        if len(cvd_vals) < 5:
            return None
        # Split roughly in half
        mid = len(cvd_vals) // 2
        cvd_prev_max, cvd_now_max = max(cvd_vals[:mid]), max(cvd_vals[mid:])
        cvd_prev_min, cvd_now_min = min(cvd_vals[:mid]), min(cvd_vals[mid:])

        bearish = (px_hh_now > px_hh_prev) and (cvd_now_max <= cvd_prev_max)
        bullish = (px_ll_now < px_ll_prev) and (cvd_now_min >= cvd_prev_min)

        if bearish or bullish:
            label = "cvd_div_bearish" if bearish else "cvd_div_bullish"
            score = 6.5
            return {
                "score": score,
                "label": label,
                "details": {
                    "px_hh_prev": round(px_hh_prev, 5),
                    "px_hh_now": round(px_hh_now, 5),
                    "px_ll_prev": round(px_ll_prev, 5),
                    "px_ll_now": round(px_ll_now, 5),
                    "cvd_prev_max": round(cvd_prev_max, 3),
                    "cvd_now_max": round(cvd_now_max, 3),
                    "cvd_prev_min": round(cvd_prev_min, 3),
                    "cvd_now_min": round(cvd_now_min, 3),
                },
            }
        return None
