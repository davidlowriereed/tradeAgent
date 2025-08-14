# backend/signals.py
import time, math, statistics
from typing import List, Tuple, Optional
from .state import trades, best_px, get_best_quote

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

def _last_n_minutes(symbol: str, seconds: int):
    now = time.time()
    return [t for t in trades[symbol] if now - t[0] <= seconds]

def compute_signals(symbol: str) -> dict:
    now = time.time()
    recent5 = _last_n_minutes(symbol, 300)   # 5 min
    recent1 = _last_n_minutes(symbol, 60)    # 1 min
    recent2 = _last_n_minutes(symbol, 120)   # 2 min
    rows = trades.get(symbol, [])
    cvd = 0.0
    vol5 = 0.0
    # 5m volume
    vol5 = sum(sz for _, _, sz, _, _, _ in recent5)

    # RVOL vs mean of last 12 rolling 5m buckets (fallback 3.0 when very little data)
    buckets = []
    # compute simple rolling 5m buckets back ~60 minutes
    for i in range(12):
        lo = now - (i+1)*300
        hi = now - i*300
        v = sum(sz for ts,_,sz,_,_,_ in trades[symbol] if lo < ts <= hi)
        buckets.append(v)
    rvol = (vol5 / (statistics.fmean(buckets) or 1.0)) if any(buckets) else 3.0

    # Momentum: last price change over last 60s in bps
    p_first = next((p for ts,p,_,_,_,_ in recent1[:1]), None)
    p_last  = next((p for ts,p,_,_,_,_ in recent1[-1:]), None)
    if p_first and p_last and p_first > 0:
        mom1_bps = ((p_last - p_first) / p_first) * 10_000.0
    else:
        mom1_bps = 0.0

    # dCVD over 2m (signed size buy-sell)
    def signed_sum(rows):
        s = 0.0
        for _,_,sz,side,_,_ in rows:
            if side == "buy":  s += sz
            elif side == "sell": s -= sz
        return s
    dcvd_2m = signed_sum(recent2)

    # CVD over the whole cache (visible to the dashboard card)
    cvd_total = signed_sum(trades[symbol])

    best_bid, best_ask = get_best_quote(symbol)
    return {
        "cvd": cvd,
        "volume_5m": vol5,
        "rvol_vs_recent": rvol,        # your existing variable
        "best_bid": best_bid,          # <- not None once quotes arrive
        "best_ask": best_ask,
        "trades_cached": len(rows),
        "mom1_bps": mom1_bps,
        "dcvd_2m": dcvd_2m,
    }
    return _sanitize(sig)


from .bars import build_bars, atr as bars_atr, px_vs_vwap_bps, momentum_bps, rvol_ratio

def compute_signals_tf(symbol: str, tfs: list[str]=["1m","5m","15m"]) -> dict:
    """
    Return per-timeframe features computed on OHLCV bars.
    Keys: mom_bps_{tf}, px_vs_vwap_bps_{tf}, rvol_{tf}, atr_{tf}
    """
    out = {}
    for tf in tfs:
        bars = build_bars(symbol, tf=tf, lookback_min=120)
        out[f"mom_bps_{tf}"] = momentum_bps(bars, lookback=1)
        out[f"px_vs_vwap_bps_{tf}"] = px_vs_vwap_bps(bars, window=20)
        out[f"rvol_{tf}"] = rvol_ratio(bars, win=5, baseline=20)
        out[f"atr_{tf}"] = bars_atr(bars, period=14)
    return out
