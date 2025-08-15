
import math
from typing import Dict, Any, List
from .state import trades, get_best_quotes, get_last_price
from .bars import build_bars, momentum_bps, px_vs_vwap_bps, rvol_ratio, atr as bars_atr


# --- compatibility helpers for legacy imports ---
import time as _time
from .state import trades as _trades

def _dcvd(symbol: str, minutes: int = 5) -> float:
    """Best-effort CVD over recent minutes: sum(buy sizes) - sum(sell sizes)."""
    now = _time.time()
    cutoff = now - minutes * 60.0
    pos = 0.0
    for (ts, price, size, side, bid, ask) in list(_trades.get(symbol, []))[-5000:]:
        if ts >= cutoff:
            if (side or '').lower().startswith('b'):
                pos += float(size or 0.0)
            elif (side or '').lower().startswith('s'):
                pos -= float(size or 0.0)
    return float(pos)

def _mom_bps(symbol: str, tf: str = "5m", lookback: int = 1) -> float:
    bars = build_bars(symbol, tf=tf, lookback_min=30)
    try:
        return float(momentum_bps(bars, lookback=lookback))
    except Exception:
        return 0.0
# --- end compatibility helpers ---

def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default

def compute_signals(symbol: str) -> Dict[str, Any]:
    b1 = build_bars(symbol, "1m", 60)
    b5 = build_bars(symbol, "5m", 240)

    mom1 = _num(momentum_bps(b1, lookback=1))
    mom5 = _num(momentum_bps(b5, lookback=1))
    pxvv = _num(px_vs_vwap_bps(b1, window=20))
    rvol = _num(rvol_ratio(b1, win=5, baseline=20))

    bid, ask = (None, None)
    q = get_best_quotes(symbol)
    if q and all(isinstance(x, (int, float)) for x in q):
        qb, qa = q
        if math.isfinite(qb): bid = qb
        if math.isfinite(qa): ask = qa

    lp = get_last_price(symbol)
    lp = float(lp) if isinstance(lp, (int, float)) and math.isfinite(lp) else None

    return {
        "mom1_bps":       mom1,
        "mom5_bps":       mom5,
        "rvol_vs_recent": rvol,
        "px_vs_vwap_bps": pxvv,
        "best_bid":       bid,
        "best_ask":       ask,
        "last_price":     lp,
        "trades_cached":  len(list(trades.get(symbol, []))),
    }

def compute_signals_tf(symbol: str, tfs: List[str] = ["1m", "5m", "15m"]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for tf in tfs:
        bars = build_bars(symbol, tf=tf, lookback_min=120)
        out[f"mom_bps_{tf}"]        = _num(momentum_bps(bars, lookback=1))
        out[f"px_vs_vwap_bps_{tf}"] = _num(px_vs_vwap_bps(bars, window=20))
        out[f"rvol_{tf}"]           = _num(rvol_ratio(bars, win=5, baseline=20))
        out[f"atr_{tf}"]            = _num(bars_atr(bars, period=14))
    return out