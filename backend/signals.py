# backend/signals.py
import time, math
from typing import List, Tuple, Optional
from .state import trades, best_px

def _finite(x: Optional[float]):
    return (x if isinstance(x, (int, float)) and math.isfinite(x) else None)

def _sanitize(obj):
    if isinstance(obj, dict):  return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [_sanitize(v) for v in obj]
    if isinstance(obj, float): return _finite(obj)
    return obj

def _mom_bps(win: List[Tuple[float, float, float, str]]) -> float:
    if not win: return 0.0
    p0 = win[0][1]; p1 = win[-1][1]
    if not p0: return 0.0
    return (p1 - p0) / p0 * 1e4  # basis points

def _dcvd(win: List[Tuple[float, float, float, str]]) -> float:
    tot = 0.0
    for _, _, sz, sd in win:
        if sd == "buy":  tot += (sz or 0.0)
        elif sd == "sell": tot -= (sz or 0.0)
    return tot

def _cvd_total(buf: List[Tuple[float, float, float, str]]) -> float:
    """Signed sum over the whole buffer as a running CVD proxy."""
    return _dcvd(buf)

def _rvol(w5: List[tuple], w15: List[tuple]) -> Optional[float]:
    vol5 = sum((r[2] or 0.0) for r in w5)
    base = (sum((r[2] or 0.0) for r in w15) / 3.0) if w15 else 0.0
    return (vol5 / base) if base > 0 else None

def compute_signals(symbol: str) -> dict:
    """
    Snapshot for dashboard & agents.
    Fields: cvd, volume_5m, rvol_vs_recent, best_bid, best_ask, trades_cached,
            mom1_bps, dcvd_2m, last_price, symbol
    """
    now = time.time()
    buf = list(trades[symbol])  # deque[(ts, price, size, side)]

    # windows
    w1  = [r for r in buf if r[0] >= now - 60]
    w2  = [r for r in buf if r[0] >= now - 120]
    w5  = [r for r in buf if r[0] >= now - 300]
    w15 = [r for r in buf if r[0] >= now - 900]

    last_px = (w1[-1][1] if w1 else (buf[-1][1] if buf else None))

    # metrics
    cvd_val   = _cvd_total(buf)
    vol5      = sum((r[2] or 0.0) for r in w5)
    rvol_val  = _rvol(w5, w15)
    mom1      = _mom_bps(w1)
    dcvd2     = _dcvd(w2)

    bid, ask  = best_px(symbol)
    # fallback for top-of-book if feed hasn’t filled it yet
    if bid is None and ask is None and last_px is not None:
        bid = ask = last_px

    sig = {
        "cvd": _finite(cvd_val),
        "volume_5m": _finite(vol5),
        "rvol_vs_recent": _finite(rvol_val),
        "best_bid": _finite(bid),
        "best_ask": _finite(ask),
        "trades_cached": len(buf),
        "mom1_bps": _finite(mom1),
        "dcvd_2m": _finite(dcvd2),
        "last_price": _finite(last_px),
        "symbol": symbol,
    }
    return _sanitize(sig)
