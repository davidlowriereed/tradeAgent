# backend/state.py
from __future__ import annotations
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple, Any
from .db import pg_exec, pg_fetchone
import time
# ------------------------------
# Realtime market state (feeds)
# ------------------------------
# Each trade tuple: (ts_epoch: float, price: float, size: float, side: "buy"|"sell")
trades: Dict[str, Deque[tuple]] = defaultdict(lambda: deque(maxlen=10_000))

# Running CVD and top-of-book
cvd: Dict[str, float] = defaultdict(float)
best_bid: Dict[str, Optional[float]] = defaultdict(lambda: None)
best_ask: Dict[str, Optional[float]] = defaultdict(lambda: None)
# Recent trades per symbol: (ts, price, size, side, bid, ask)
trades = defaultdict(lambda: deque(maxlen=50_000))

# Latest quoted bests
_best_px = defaultdict(lambda: {"bid": None, "ask": None})

def record_trade(*, symbol: str, price: float, size: float, side: str,
                 bid: float|None = None, ask: float|None = None,
                 ts: float|None = None):
    """Append trade and (optionally) update best bid/ask."""
    lst = trades[symbol]
    lst.append({"ts": ts or time.time(), "price": float(price), "size": float(size), "side": side})
    # keep a reasonable cap so memory doesn’t balloon
    if len(lst) > 5000:
        del lst[:len(lst)-4000]

    # <- this is the critical line for quotes
    if bid is not None and ask is not None:
        _best_quotes[symbol] = (float(bid), float(ask))

def get_best_quote(symbol: str) -> tuple[float|None, float|None]:
    return _best_quotes.get(symbol, (None, None))
def best_px(symbol: str) -> tuple[float | None, float | None]:
    b = _best_px[symbol]
    return b["bid"], b["ask"]

def best_px(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    return best_bid.get(symbol), best_ask.get(symbol)

# ---------------------------------------------------------
# Position / posture state  (DB + in-memory cache)
# ---------------------------------------------------------
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
    "trades", "cvd", "best_bid", "best_ask", "best_px",
    "get_position", "set_position",
    "POSTURE_STATE",
]
