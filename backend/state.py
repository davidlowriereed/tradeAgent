# backend/state.py

from collections import defaultdict, deque
from typing import Dict, Deque, Tuple, Any, Optional
from .db import pg_exec, pg_fetchone

# ---- live trades ring-buffers (epoch_s, price, size, side) per symbol
trades: Dict[str, Deque[Tuple[float, float, float, str]]] = defaultdict(lambda: deque(maxlen=6000))

# ---- posture guard (short-lived, “session” level)
POSTURE_STATE: Dict[str, Dict[str, Any]] = {}

# ---- persisted “am I in a trade?” position state
_position_cache: Dict[str, Dict[str, Any]] = {}

def _ensure_row(symbol: str) -> None:
    pg_exec(
        "INSERT INTO position_state(symbol,status,qty,avg_price) VALUES (%s,'flat',0,NULL) "
        "ON CONFLICT (symbol) DO NOTHING",
        (symbol,),
    )

def get_position(symbol: str) -> Dict[str, Any]:
    """Return {'symbol','status','qty','avg_price','last_action','last_conf','updated_at'}; default 'flat'."""
    if symbol in _position_cache:
        return _position_cache[symbol]

    _ensure_row(symbol)
    row = pg_fetchone(
        "SELECT symbol,status,qty,avg_price,last_action,last_conf,updated_at "
        "FROM position_state WHERE symbol=%s",
        (symbol,),
    )
    if not row:
        out = {"symbol": symbol, "status": "flat", "qty": 0.0, "avg_price": None,
               "last_action": None, "last_conf": None, "updated_at": None}
    else:
        out = {
            "symbol": row["symbol"],
            "status": row["status"],
            "qty": float(row["qty"] or 0),
            "avg_price": (float(row["avg_price"]) if row["avg_price"] is not None else None),
            "last_action": row.get("last_action"),
            "last_conf": (float(row["last_conf"]) if row.get("last_conf") is not None else None),
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }
    _position_cache[symbol] = out
    return out

def set_position(symbol: str, status: str, qty: float = 0.0, avg_price: Optional[float] = None,
                 last_action: Optional[str] = None, last_conf: Optional[float] = None) -> Dict[str, Any]:
    """Upsert current position state, update cache, and return it."""
    _ensure_row(symbol)
    pg_exec(
        """
        INSERT INTO position_state(symbol,status,qty,avg_price,last_action,last_conf,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (symbol) DO UPDATE
          SET status=EXCLUDED.status,
              qty=EXCLUDED.qty,
              avg_price=EXCLUDED.avg_price,
              last_action=EXCLUDED.last_action,
              last_conf=EXCLUDED.last_conf,
              updated_at=NOW()
        """,
        (symbol, status, qty, avg_price, last_action, last_conf),
    )
    out = {
        "symbol": symbol, "status": status,
        "qty": float(qty or 0), "avg_price": (float(avg_price) if avg_price is not None else None),
        "last_action": last_action, "last_conf": (float(last_conf) if last_conf is not None else None),
        "updated_at": None,
    }
    _position_cache[symbol] = out
    return out
