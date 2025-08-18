from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any
import time

@dataclass
class Order:
    id: int
    ts: float
    symbol: str
    side: str    # 'buy'|'sell'
    qty: float
    px: float
    fee: float

@dataclass
class PaperBook:
    equity: float = 10000.0
    orders: List[Order] = field(default_factory=list)
    next_id: int = 1

    def fill(self, symbol: str, side: str, qty: float, px: float, fee_bps: float = 10.0):
        fee = abs(qty*px) * (fee_bps/10000.0)
        self.orders.append(Order(self.next_id, time.time(), symbol, side, qty, px, fee))
        self.next_id += 1
        # no position accounting here; the FSM controls posture and we track PnL on exit
        return self.orders[-1]

BOOKS: Dict[str, PaperBook] = {}
def book_for(symbol: str) -> PaperBook:
    if symbol not in BOOKS: BOOKS[symbol] = PaperBook()
    return BOOKS[symbol]
