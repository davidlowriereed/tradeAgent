# backend/profile.py
from collections import defaultdict
from .state import trades

TICK = 0.25  # e.g., ES/NQ; for crypto choose a sensible tick
def _bucket(px): return round(px / TICK) * TICK

def build_profile(symbol: str, lookback_s: int = 90*60) -> dict:
    vol = defaultdict(float)
    now = __import__("time").time()
    for ts, px, sz, side in trades.get(symbol, []):
        if ts < now - lookback_s: continue
        vol[_bucket(px)] += float(sz)
    if not vol: return {}

    # Value area via 70% rule
    items = sorted(vol.items())
    poc_p, poc_v = max(items, key=lambda kv: kv[1])
    total = sum(v for _, v in items)
    # expand around POC until ~70% covered
    # ... (keep simple here)
    # Identify LVNs as local minima between high‑volume peaks
    lvns = [p for i,(p,v) in enumerate(items[1:-1],1) if v < items[i-1][1] and v < items[i+1][1]]
    return {"poc": poc_p, "lvns": lvns, "dist": items}
