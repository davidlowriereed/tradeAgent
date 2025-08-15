
from __future__ import annotations
from typing import List, Dict, Optional
from .state import trades
import math, time

TF_SEC = {"1m": 60, "5m": 300, "15m": 900}

def _vwap(px_sum: float, vol_sum: float) -> float:
    return (px_sum / vol_sum) if vol_sum > 0 else 0.0

def build_bars(symbol: str, tf: str = "1m", lookback_min: int = 120) -> List[Dict]:
    """Simple time-bucket bars from trades: o,h,l,c,v,vwap."""
    sec = TF_SEC.get(tf, 60)
    now = time.time()
    start = now - lookback_min * 60
    ticks = [t for t in trades.get(symbol, []) if t[0] >= start]
    if not ticks:
        return []
    # bucket by floor(ts / sec)*sec
    buckets = {}
    for ts, px, sz, side in ticks:
        bucket = int(ts // sec) * sec
        bar = buckets.get(bucket)
        if not bar:
            buckets[bucket] = {
                "t": bucket, "o": px, "h": px, "l": px, "c": px,
                "v": float(sz),
                "_pv": px * sz, "_vv": float(sz),
            }
        else:
            bar["h"] = max(bar["h"], px)
            bar["l"] = min(bar["l"], px)
            bar["c"] = px
            bar["v"] += float(sz)
            bar["_pv"] += px * sz
            bar["_vv"] += float(sz)
    bars = [buckets[k] for k in sorted(buckets.keys())]
    for b in bars:
        b["vwap"] = _vwap(b["_pv"], b["_vv"])
        b.pop("_pv", None); b.pop("_vv", None)
    return bars

def momentum_bps(bars: List[dict], lookback: int = 1) -> float:
    if not bars or len(bars) <= lookback:
        return 0.0
    last = bars[-1]["c"]
    prev = bars[-1 - lookback]["c"]
    if prev == 0:
        return 0.0
    return (last / prev - 1.0) * 10000.0

def px_vs_vwap_bps(bars: List[dict], window: int = 20) -> float:
    if not bars:
        return 0.0
    tail = bars[-window:] if len(bars) >= window else bars
    avg_vwap = sum(b.get("vwap", b["c"]) for b in tail) / max(1, len(tail))
    last = bars[-1]["c"]
    if avg_vwap == 0:
        return 0.0
    return (last / avg_vwap - 1.0) * 10000.0

def rvol_ratio(bars: List[dict], win: int = 5, baseline: int = 20) -> float:
    if len(bars) < (win + baseline):
        return 1.0  # neutral
    v_recent = sum(b.get("v", 0.0) for b in bars[-win:])
    v_base = sum(b.get("v", 0.0) for b in bars[-(win + baseline):-win])
    return (v_recent / v_base) if v_base else 1.0

def atr(bars: List[dict], period: int = 14) -> float:
    if not bars or len(bars) < 2:
        return 0.0
    trs = []
    prev_close = bars[0]["c"]
    for b in bars[1:]:
        h, l, c = b["h"], b["l"], b["c"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if not trs:
        return 0.0
    tail = trs[-period:] if len(trs) >= period else trs
    return sum(tail) / len(tail)
