from __future__ import annotations
from typing import Dict, List
import time
from .state import trades

_SEC_PER = {"1m": 60, "5m": 300, "15m": 900}

def _bucket(ts: float, sec: int) -> int:
    return int(ts // sec) * sec

def build_bars(symbol: str, tf: str = "1m", lookback_min: int = 60) -> List[dict]:
    """
    Build simple OHLCV bars from the in-memory trades deque.
    Returns ascending list of dicts:
    {"t": epoch_sec, "o": open, "h": high, "l": low, "c": close, "v": volume, "vwap": vwap}
    """
    sec = _SEC_PER.get(tf, 60)
    now = time.time()
    start = now - lookback_min * 60

    buckets: Dict[int, dict] = {}
    rows = list(trades.get(symbol, []))

    for ts, price, size, side in rows:
        if ts < start:                 # <-- was start_ts (undefined)
            continue
        # normalize incoming values; skip malformed ticks
        try:
            price = float(price)
            size = float(size)
        except (TypeError, ValueError):
            continue

        b = _bucket(ts, sec)
        row = buckets.get(b)
        if row is None:
            row = buckets[b] = {"t": b, "o": price, "h": price, "l": price, "c": price, "v": 0.0, "pv": 0.0}
        else:
            row["h"] = max(row["h"], price)
            row["l"] = min(row["l"], price)
            row["c"] = price

        v = float(size or 0.0)
        row["v"] += v
        row["pv"] += v * float(price or 0.0)

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
    vol = sum((b["v"] or 0.0) for b in w)
    pv = sum((b["v"] or 0.0) * ((b.get("vwap") if b.get("vwap") is not None else b["c"]) or 0.0) for b in w)
    vwap = (pv / vol) if vol else None
    c = bars[-1]["c"]
    if vwap and c:
        try:
            return ((c - vwap) / vwap) * 1e4
        except ZeroDivisionError:
            return 0.0
    return 0.0

def momentum_bps(bars: List[dict], lookback: int = 1) -> float:
    if len(bars) < lookback + 1:
        return 0.0
    c_now = bars[-1]["c"]
    c_prev = bars[-1 - lookback]["c"]
    if not (c_now and c_prev):
        return 0.0
    try:
        return ((c_now - c_prev) / c_prev) * 1e4
    except ZeroDivisionError:
        return 0.0

def rvol_ratio(bars: List[dict], win: int = 5, baseline: int = 20) -> float:
    if len(bars) < max(win, baseline) + win:
        # need at least win + baseline bars to compare
        return 0.0
    v_recent = sum((b["v"] or 0.0) for b in bars[-win:])
    v_base   = sum((b["v"] or 0.0) for b in bars[-(win + baseline):-win])
    return (v_recent / v_base) if v_base else 0.0
