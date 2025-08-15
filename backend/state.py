# backend/state.py
from __future__ import annotations
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple, Optional, Any
from .db import pg_exec, pg_fetchone

# ---------------------------------------------------------------------
# In-memory scratchpads used across the app (no FastAPI routes here)
# ---------------------------------------------------------------------

# Recent Findings ring buffer (fallback when DB is unavailable)
RECENT_FINDINGS: deque = deque(maxlen=1000)

# Trades: (ts_sec: float, price: float, size: float, side: "buy"|"sell")
Trade = Tuple[float, float, float, str]
trades: Dict[str, Deque[Trade]] = defaultdict(lambda: deque(maxlen=50_000))

# Quotes & last price cache
_best_quotes: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
_last_price: Dict[str, float] = {}

def record_trade(
    symbol: str,
    ts,
    price,
    size,
    side,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
) -> None:
    """Append a trade and optionally update best bid/ask. Normalizes ts to seconds."""
    try:
        ts_f = float(ts)
        px_f = float(price)
        sz_f = float(size)
    except Exception:
        return

    if ts_f > 1e12:  # ms -> s
        ts_f /= 1000.0

    dq = trades.setdefault(symbol, deque(maxlen=50_000))
    dq.append((ts_f, px_f, sz_f, str(side)))

    _last_price[symbol] = px_f

    if bid is not None or ask is not None:
        b, a = _best_quotes.get(symbol, (None, None))
        if bid is not None:
            try: b = float(bid)
            except: pass
        if ask is not None:
            try: a = float(ask)
            except: pass
        _best_quotes[symbol] = (b, a)

def get_best_quotes(symbol: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    return _best_quotes.get(symbol)

def get_last_price(symbol: str) -> Optional[float]:
    return _last_price.get(symbol)

def best_px(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """Back-compat alias used in a few places."""
    return _best_quotes.get(symbol, (None, None))

# ---------------------------------------------------------------------
# Position / posture state (DB-backed + in-memory cache)
# ---------------------------------------------------------------------

_POSTURE_CACHE: Dict[str, Dict[str, Any]] = defaultdict(
    lambda: {
        "status": "flat",
        "qty": 0.0,
        "avg_price": None,
        "last_action": None,
        "last_conf": None,
        "updated_at": None,
    }
)

# Back-compat alias (older code imports this)
POSTURE_STATE = _POSTURE_CACHE

def _row_to_state(row: Optional[dict], symbol: str) -> Dict[str, Any]:
    if not row:
        return _POSTURE_CACHE[symbol]
    s = {
        "status": row.get("status", "flat"),
        "qty": float(row.get("qty") or 0.0),
        "avg_price": row.get("avg_price"),
        "last_action": row.get("last_action"),
        "last_conf": row.get("last_conf"),
        "updated_at": row.get("updated_at"),
    }
    _POSTURE_CACHE[symbol] = s
    return s

def get_position(symbol: str) -> Dict[str, Any]:
    row = pg_fetchone(
        "SELECT symbol,status,qty,avg_price,updated_at,last_action,last_conf "
        "FROM position_state WHERE symbol=%s",
        (symbol,),
    )
    return _row_to_state(row, symbol)

def set_position(
    symbol: str,
    status: str,
    qty: float = 0.0,
    avg_price: Optional[float] = None,
    last_action: Optional[str] = None,
    last_conf: Optional[float] = None,
) -> Dict[str, Any]:
    pg_exec(
        "INSERT INTO position_state(symbol,status,qty,avg_price,last_action,last_conf,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,NOW()) "
        "ON CONFLICT(symbol) DO UPDATE SET "
        "status=EXCLUDED.status, "
        "qty=EXCLUDED.qty, "
        "avg_price=EXCLUDED.avg_price, "
        "last_action=EXCLUDED.last_action, "
        "last_conf=EXCLUDED.last_conf, "
        "updated_at=NOW()",
        (symbol, status, qty, avg_price, last_action, last_conf),
    )
    return get_position(symbol)

__all__ = [
    # caches
    "RECENT_FINDINGS", "trades",
    # quotes/price helpers
    "record_trade", "get_best_quotes", "get_last_price", "best_px",
    # posture
    "get_position", "set_position", "POSTURE_STATE",
]
