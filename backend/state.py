
from __future__ import annotations
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple, Optional, Any
import time

# In-memory realtime market state
trades: Dict[str, Deque[tuple]] = defaultdict(lambda: deque(maxlen=50_000))
_best_quotes: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
_last_price: Dict[str, float] = {}

# Findings buffer (DB fallback)
from collections import deque as _deque
RECENT_FINDINGS = _deque(maxlen=1000)

# Position / posture state (in-memory cache)
POSTURE_STATE: Dict[str, Dict[str, Any]] = defaultdict(
    lambda: {
        "status": "flat",
        "qty": 0.0,
        "avg_price": None,
        "last_action": None,
        "last_conf": None,
        "updated_at": None,
        "_persist": 0,
    }
)

def record_trade(symbol: str, ts, price, size, side, bid=None, ask=None) -> None:
    """Append a trade and optionally update best bid/ask. ts may be ms."""
    try:
        ts = float(ts); price = float(price); size = float(size)
    except Exception:
        return
    if ts > 1e12:
        ts /= 1000.0
    dq = trades.setdefault(symbol, deque(maxlen=50_000))
    dq.append((ts, price, size, side))
    _last_price[symbol] = price
    b, a = _best_quotes.get(symbol, (None, None))
    if bid is not None:
        try: b = float(bid)
        except: pass
    if ask is not None:
        try: a = float(ask)
        except: pass
    if (bid is not None) or (ask is not None):
        _best_quotes[symbol] = (b, a)

def get_best_quotes(symbol: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    return _best_quotes.get(symbol)

def get_last_price(symbol: str) -> Optional[float]:
    return _last_price.get(symbol)
