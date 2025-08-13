from typing import Optional, Dict, Any
import asyncio, time
from .db import pg_query, pg_execute

_state_lock = asyncio.Lock()
_positions: Dict[str, Dict[str, Any]] = {}   # { "BTC-USD": {"status":"flat","qty":0,"avg_price":None,...}, ... }

async def load_positions(symbols):
    rows = await pg_query("SELECT symbol, status, qty, avg_price, updated_at, last_action, last_conf FROM position_state WHERE symbol = ANY($1)", [symbols])
    async with _state_lock:
        _positions.clear()
        for r in rows:
            _positions[r["symbol"]] = dict(r)
        # ensure defaults for missing rows
        for s in symbols:
            _positions.setdefault(s, {"symbol": s, "status": "flat", "qty": 0.0, "avg_price": None, "last_action": None, "last_conf": None})

def get_position(symbol: str) -> Dict[str, Any]:
    return _positions.get(symbol) or {"symbol": symbol, "status": "flat", "qty": 0.0, "avg_price": None}

async def set_position(symbol: str, status: str, qty: float = 0.0, avg_price: Optional[float] = None, last_action: Optional[str] = None, last_conf: Optional[float] = None):
    assert status in ("flat","long","short")
    async with _state_lock:
        _positions[symbol] = {
            "symbol": symbol,
            "status": status,
            "qty": qty or 0.0,
            "avg_price": avg_price,
            "last_action": last_action,
            "last_conf": last_conf,
        }
    await pg_execute("""
        INSERT INTO position_state(symbol,status,qty,avg_price,updated_at,last_action,last_conf)
        VALUES($1,$2,$3,$4,NOW(),$5,$6)
        ON CONFLICT(symbol) DO UPDATE SET
          status=EXCLUDED.status,
          qty=EXCLUDED.qty,
          avg_price=EXCLUDED.avg_price,
          updated_at=NOW(),
          last_action=EXCLUDED.last_action,
          last_conf=EXCLUDED.last_conf
    """, [symbol, status, qty or 0.0, avg_price, last_action, last_conf])
