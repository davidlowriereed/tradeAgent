
import time, math
from typing import List
from .state import trades, best_px
# backend/signals.py
from .state import trades, cvd, best_bid, best_ask
import math

def _json_finite(x):
    return (x if isinstance(x, (int, float)) and math.isfinite(x) else None)

def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return _json_finite(obj)
    return obj
    
def _mom_bps(win: list) -> float:
    if not win: return 0.0
    p0 = win[0][1]; p1 = win[-1][1]
    if not p0: return 0.0
    return (p1 - p0)/p0 * 1e4

def _dcvd(win: list) -> float:
    tot = 0.0
    for _,_,sz,sd in win:
        if sd == "buy":
            tot += (sz or 0.0)
        elif sd == "sell":
            tot -= (sz or 0.0)
    return tot

def _rvol(w5: list, w15: list) -> float | None:
    vol5 = sum((r[2] or 0.0) for r in w5)
    base = (sum((r[2] or 0.0) for r in w15)/3.0) if w15 else 0.0
    return (vol5/base) if base>0 else None

def compute_signals(symbol: str) -> dict:
    now = time.time()
    buf = trades[symbol]
    w1  = [r for r in buf if r[0] >= now - 60]
    w5  = [r for r in buf if r[0] >= now - 300]
    w15 = [r for r in buf if r[0] >= now - 900]

    bid, ask = best_px.get(symbol, (None, None))
    sig = {
    "cvd": _json_finite(cvd[symbol]),
    "volume_5m": _json_finite(vol5),
    "rvol_vs_recent": _json_finite(rvol),
    "best_bid": _json_finite(best_bid[symbol]),
    "best_ask": _json_finite(best_ask[symbol]),
    "trades_cached": len(trades[symbol]),
    }
    return _sanitize(sig)
