# backend/signals.py
import time, math
from typing import List, Tuple, Optional
from .state import trades, cvd, best_px, best_bid, best_ask

def _json_finite(x: Optional[float]):
    return (x if isinstance(x, (int, float)) and math.isfinite(x) else None)

def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return _json_finite(obj)
    return obj

def _mom_bps(win: List[Tuple[float, float, float, str]]) -> float:
    if not win: 
        return 0.0
    p0 = win[0][1]; p1 = win[-1][1]
    if not p0:
        return 0.0
    return (p1 - p0) / p0 * 1e4

def _dcvd(win: List[Tuple[float, float, float, str]]) -> float:
    tot = 0.0
    for _, _, sz, sd in win:
        if sd == "buy":
            tot += (sz or 0.0)
        elif sd == "sell":
            tot -= (sz or 0.0)
    return tot

def _rvol(w5: List[tuple], w15: List[tuple]) -> Optional[float]:
    vol5 = sum((r[2] or 0.0) for r in w5)
    base = (sum((r[2] or 0.0) for r in w15) / 3.0) if w15 else 0.0
    return (vol5 / base) if base > 0 else None

def compute_signals(symbol: str) -> dict:
    """
    Returns JSON-safe snapshot for the dashboard & agents.
    Keys:
      cvd, volume_5m, rvol_vs_recent, best_bid, best_ask, trades_cached,
      mom1_bps, dcvd_2m
    """
    now = time.time()
    buf = trades[symbol]  # deque of (ts, price, size, side)

    # Time windows
    w1  = [r for r in buf if r[0] >= now - 60]
    w2  = [r for r in buf if r[0] >= now - 120]
    w5  = [r for r in buf if r[0] >= now - 300]
    w15 = [r for r in buf if r[0] >= now - 900]

    # Metrics
    vol5 = sum((r[2] or 0.0) for r in w5)
    rvol_val = _rvol(w5, w15)
    mom1 = _mom_bps(w1)
    dcvd2 = _dcvd(w2)

    bid, ask = best_px(symbol)

    sig = {
        "cvd": _json_finite(cvd[symbol]),
        "volume_5m": _json_finite(vol5),
        "rvol_vs_recent": _json_finite(rvol_val),
        "best_bid": _json_finite(bid),
        "best_ask": _json_finite(ask),
        "trades_cached": len(buf),
        "mom1_bps": _json_finite(mom1),
        "dcvd_2m": _json_finite(dcvd2),
    }
    return _sanitize(sig)
