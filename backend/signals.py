# --- Imports ---
import time
import math
import statistics
from typing import List, Tuple, Optional, Dict, Any

from .state import trades, best_px, get_best_quote
from .bars import build_bars, momentum_bps, px_vs_vwap_bps, rvol_ratio
from .bars import atr as bars_atr  # for compute_signals_tf


# --- API Route ---
@app.get("/signals")
async def signals(symbol: str = Query(...)):
    from .signals import compute_signals
    import math

    def _num(x, default=0.0):
        try:
            v = float(x)
            return v if math.isfinite(v) else default
        except Exception:
            return default

    sig = compute_signals(symbol) or {}
    return {
        "symbol": symbol,
        "mom1_bps":       _num(sig.get("mom1_bps")),
        "mom5_bps":       _num(sig.get("mom5_bps")),
        "rvol_vs_recent": _num(sig.get("rvol_vs_recent")),
        "px_vs_vwap_bps": _num(sig.get("px_vs_vwap_bps")),
        "best_bid":       sig.get("best_bid"),
        "best_ask":       sig.get("best_ask"),
        "last_price":     sig.get("last_price"),
        "trades_cached":  int(sig.get("trades_cached") or 0),
    }


# --- Utility Functions ---
def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _finite(x: Optional[float]):
    return (x if isinstance(x, (int, float)) and math.isfinite(x) else None)


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        return _finite(obj)
    return obj


def _mom_bps(win: List[Tuple[float, float, float, str]]) -> float:
    if not win:
        return 0.0
    p0 = win[0][1]
    p1 = win[-1][1]
    if not p0:
        return 0.0
    return (p1 - p0) / p0 * 1e4  # basis points


def _dcvd(win: List[Tuple[float, float, float, str]]) -> float:
    tot = 0.0
    for _, _, sz, sd in win:
        if sd == "buy":
            tot += (sz or 0.0)
        elif sd == "sell":
            tot -= (sz or 0.0)
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


# --- Signal Computation Functions ---

# First version of compute_signals
def compute_signals(symbol: str) -> Dict[str, Any]:
    """
    Lightweight signals for the header/footer tiles.
    """
    try:
        b1 = build_bars(symbol, "1m", 60)
        b5 = build_bars(symbol, "5m", 240)

        return {
            "mom1_bps":       _num(momentum_bps(b1, lookback=1)),
            "mom5_bps":       _num(momentum_bps(b5, lookback=1)),
            "rvol_vs_recent": _num(rvol_ratio(b1, win=5, baseline=20)),
            "px_vs_vwap_bps": _num(px_vs_vwap_bps(b1, window=20)),
            "best_bid":       (get_best_quotes(symbol) or (None, None))[0],
            "best_ask":       (get_best_quotes(symbol) or (None, None))[1],
            "last_price":     get_last_price(symbol),
            "trades_cached":  len(list(trades.get(symbol, []))),
        }
    except Exception:
        return {
            "mom1_bps": 0.0, "mom5_bps": 0.0, "rvol_vs_recent": 0.0, "px_vs_vwap_bps": 0.0,
            "best_bid": None, "best_ask": None, "last_price": None, "trades_cached": 0,
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


# Second version of compute_signals (duplicate)
def compute_signals(symbol: str) -> Dict[str, Any]:
    """
    Compute lightweight per-symbol signals for the dashboard.
    Always returns a dict with finite floats (or None for quotes).
    """
    try:
        from .bars import build_bars, momentum_bps, px_vs_vwap_bps, rvol_ratio
        from .state import get_best_quotes, get_last_price, trades

        b1  = build_bars(symbol, "1m", 60)
        b5  = build_bars(symbol, "5m", 240)

        mom1 = _num(momentum_bps(b1, 1))
        mom5 = _num(momentum_bps(b5, 1))
        pxvv = _num(px_vs_vwap_bps(b1, 20))
        rvol = _num(rvol_ratio(b1, lookback=20))

        bid, ask = (None, None)
        q = get_best_quotes(symbol)
        if q:
            qb, qa = q
            bid = qb if isinstance(qb, (int, float)) and math.isfinite(qb) else None
            ask = qa if isinstance(qa, (int, float)) and math.isfinite(qa) else None

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
    except Exception:
        return {
            "mom1_bps":       0.0,
            "mom5_bps":       0.0,
            "rvol_vs_recent": 0.0,
            "px_vs_vwap_bps": 0.0,
            "best_bid":       None,
            "best_ask":       None,
            "last_price":     None,
            "trades_cached":  0,
        }


# Second version of compute_signals_tf (duplicate)
def compute_signals_tf(symbol: str, tfs: list[str] = ["1m", "5m", "15m"]) -> dict:
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
