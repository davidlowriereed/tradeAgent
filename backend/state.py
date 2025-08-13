# backend/state.py
from __future__ import annotations
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple, Any
from .db import pg_exec, pg_fetchone

# ------------------------------
# Realtime market state (feeds)
# ------------------------------
trades: Dict[str, Deque[tuple]] = defaultdict(lambda: deque(maxlen=10_000))

best_bid: Dict[str, Optional[float]] = {}
best_ask: Dict[str, Optional[float]] = {}

def best_px(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    return best_bid.get(symbol), best_ask.get(symbol)

# ---------------------------------------------------------
# Position / posture state  (DB + in-memory cache)
# ---------------------------------------------------------
# In-memory cache keyed by symbol; used for quick reads
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

# Back-compat name expected by older modules (e.g., scheduler.py)
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
    "trades", "best_bid", "best_ask", "best_px",
    "get_position", "set_position",
    "POSTURE_STATE",
]
