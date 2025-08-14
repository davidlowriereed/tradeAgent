# backend/bars.py
from __future__ import annotations
from typing import Dict, List
import time
from .state import trades

_SEC_PER = {"1m": 60, "5m": 300, "15m": 900}

def _bucket(ts: float, sec: int) -> int:
    return int(ts // sec) * sec

def build_bars(symbol: str, tf: str = "1m", lookback_min: int = 60) -> List[dict]:
    sec = _SEC_PER.get(tf, 60)
    now = time.time()
    start = now - lookback_min * 60

    buckets: Dict[int, dict] = {}
    rows = list(trades.get(symbol, []))

    for ts, price, size, side in rows:
        # convert ms → sec if needed
        if ts and ts > 1e12:
            ts = ts / 1000.0

        if ts < start:
            continue

        try:
            p = float(price)
            s = float(size)
        except (TypeError, ValueError):
            continue

        b = _bucket(ts, sec)
        row = buckets.get(b)
        if row is None:
            row = buckets[b] = {"t": b, "o": p, "h": p, "l": p, "c": p, "v": 0.0, "pv": 0.0}
        else:
            row["h"] = max(row["h"], p)
            row["l"] = min(row["l"], p)
            row["c"] = p

        row["v"] += s
        row["pv"] += s * p

    bars = [buckets[k] for k in sorted(buckets.keys())]
    for b in bars:
        b["vwap"] = (b["pv"] / b["v"]) if b["v"] else None
        b.pop("pv", None)

    return bars



def atr(bars: List[dict], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: List[float] = []
    prev_c = bars[0]["c"]
    for b in bars[1:]:
        h, l, c = b["h"], b["l"], b["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    if not trs:
        return 0.0
    p = min(period, len(trs))
    return sum(trs[-p:]) / float(p)

def px_vs_vwap_bps(bars: List[dict], window: int = 20) -> float:
    if not bars:
        return 0.0
    w = bars[-window:]
    vol = sum(b.get("v", 0.0) for b in w)
    # Use vwap if present, otherwise fall back to close
    pv = sum((b.get("v", 0.0)) * (b.get("vwap") if b.get("vwap") is not None else b.get("c", 0.0)) for b in w)
    vwap = (pv / vol) if vol else None
    c = bars[-1].get("c")
    if vwap is not None and c is not None:
        try:
            return ((c - vwap) / vwap) * 1e4
        except ZeroDivisionError:
            return 0.0
    return 0.0

def momentum_bps(bars: List[dict], lookback: int = 1) -> float:
    if len(bars) < lookback + 1:
        return 0.0
    c_now = bars[-1].get("c")
    c_prev = bars[-1 - lookback].get("c")
    if c_now is None or c_prev is None:
        return 0.0
    try:
        return ((c_now - c_prev) / c_prev) * 1e4
    except ZeroDivisionError:
        return 0.0

def _f(x, default=0.0):
    try:
        v = float(x); 
        return v if math.isfinite(v) else default
    except Exception:
        return default

def rvol_ratio(bars: List[Dict], win: int = 5, baseline: int = 20) -> float:
    n = len(bars)
    if win <= 0 or baseline <= 0 or n < 2:
        return 0.0

    # Use as much history as we have; require at least 2 bars in each slice
    base_n = max(2, min(baseline, max(0, n - win)))
    if base_n < 2:
        return 0.0

    recent = bars[-win:] if n >= win else bars[:]  # partial recent if needed
    base   = bars[-(win + base_n):-win] if n >= (win + 2) else bars[:-len(recent)]

    def vol(slice_):
        return sum(max(0.0, _f(b.get("v", 0))) for b in slice_) or 0.0

    v_recent = vol(recent)
    v_base   = vol(base)
    return float(v_recent / max(v_base, 1e-9))
