
import time, json, asyncio
import websockets
from typing import Optional
from ..config import SYMBOLS
from ..state import trades, best_px, record_trade

record_trade(
    symbol=symbol,
    price=trade_price,
    size=trade_size,
    side=("buy" if is_buy else "sell"),
    bid=best_bid_value,      # pass None if you don’t have it on this event
    ask=best_ask_value,      # pass None if you don’t have it on this event
    ts=event_ts_in_seconds   # optional
)

def _f(x):
    try: return float(x)
    except: return None

async def market_loop():
    while True:
        try:
            uri = "wss://ws-feed.exchange.coinbase.com"
            sub = {"type":"subscribe","channels":[{"name":"ticker","product_ids":SYMBOLS}]}
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(sub))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "ticker": continue
                    sym = msg.get("product_id")
                    if sym not in trades: continue

                    ts = time.time()
                    px = _f(msg.get("price")) or _f(msg.get("best_bid")) or _f(msg.get("best_ask"))
                    size = _f(msg.get("last_size")) or 0.0
                    side = msg.get("side")

                    if px: trades[sym].append((ts, px, size, side))
                    bb, ba = _f(msg.get("best_bid")), _f(msg.get("best_ask"))
                    if bb or ba:
                        obb, oba = best_px.get(sym, (None, None))
                        best_px[sym] = (bb or obb, ba or oba)
        except Exception:
            await asyncio.sleep(2.0)
