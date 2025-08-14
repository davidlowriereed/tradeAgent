
import time, math
from typing import List
from .state import trades, best_px
# backend/signals.py
from .state import trades, cvd, best_bid, best_ask

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
    return {
        "cvd": _dcvd(w5),
        "volume_5m": sum((r[2] or 0.0) for r in w5),
        "rvol_vs_recent": _rvol(w5, w15),
        "best_bid": bid,
        "best_ask": ask,
        "trades_cached": len(buf),
        "mom1_bps": _mom_bps(w1),
        "mom5_bps": _mom_bps(w5),
    }
