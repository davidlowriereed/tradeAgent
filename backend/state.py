# backend/state.py
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple, Optional
from .db import pg_exec, pg_fetchone

# ---- realtime in-memory state ----
# tick/trade ring-buffers by symbol (producer: market_loop)
trades: Dict[str, Deque[tuple]] = defaultdict(lambda: deque(maxlen=10_000))

# best bid/ask by symbol (producer: market_loop)
best_bid: Dict[str, Optional[float]] = {}
best_ask: Dict[str, Optional[float]] = {}

def best_px(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (best_bid, best_ask) for a symbol, or (None, None) if unknown."""
    return best_bid.get(symbol), best_ask.get(symbol)

# ---- position state helpers (keep yours; shown here for completeness) ----
def get_position(symbol: str):
    row = pg_fetchone(
        "SELECT symbol,status,qty,avg_price,updated_at,last_action,last_conf "
        "FROM position_state WHERE symbol=%s",
        (symbol,),
    )
    if not row:
        return {"symbol": symbol, "status": "flat", "qty": 0.0, "avg_price": None}
    return row

def set_position(symbol: str, status: str, qty: float = 0.0, avg_price: Optional[float] = None):
    pg_exec(
        "INSERT INTO position_state(symbol,status,qty,avg_price,updated_at) "
        "VALUES (%s,%s,%s,%s,NOW()) "
        "ON CONFLICT(symbol) DO UPDATE SET "
        "status=EXCLUDED.status,"
        "qty=EXCLUDED.qty,"
        "avg_price=EXCLUDED.avg_price,"
        "updated_at=NOW()",
        (symbol, status, qty, avg_price),
    )
    return get_position(symbol)
