import asyncio
import json
import os
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
import websockets

# --- Simple in-memory state ---
TICKER = os.getenv("SYMBOL", "BTC-USD")
WS_URL = "wss://ws-feed.exchange.coinbase.com"

# rolling windows
MAX_TICKS = 2000  # keep last N trades
trades = deque(maxlen=MAX_TICKS)  # list of (ts, price, size, side)
cvd = 0.0  # cumulative volume delta
rv_window_secs = 300  # 5 minutes rolling
last_5m_volume = 0.0
history_volumes = deque(maxlen=48)  # 48 * 5m = 4 hours baseline for RVOL

# simple order book imbalance metric (level2)
best_bid = None
best_ask = None
bid_size_1 = 0.0
ask_size_1 = 0.0

app = FastAPI()

# Jinja setup for a tiny dashboard page
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape()
)

@app.get("/health")
async def health():
    return {"status": "ok", "symbol": TICKER, "trades_cached": len(trades)}

@app.get("/signals")
async def signals():
    now = time.time()
    # compute rolling 5m volume and RVOL baseline (median of history)
    cutoff = now - rv_window_secs
    vol_5m = sum(size for ts, _, size, _ in trades if ts >= cutoff)
    baseline = sorted(history_volumes)
    baseline_med = baseline[len(baseline)//2] if baseline else 0.0
    rvol = (vol_5m / baseline_med) if baseline_med > 0 else None

    book_imbalance = None
    if (bid_size_1 or ask_size_1) and (bid_size_1 + ask_size_1) > 0:
        book_imbalance = (bid_size_1 - ask_size_1) / (bid_size_1 + ask_size_1)

    payload = {
        "symbol": TICKER,
        "cvd": cvd,
        "volume_5m": vol_5m,
        "rvol_vs_recent": rvol,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "book_imbalance": book_imbalance,
        "trades_cached": len(trades),
        "timestamp": now
    }
    return JSONResponse(payload)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = env.get_template("index.html")
    return template.render(symbol=TICKER)

async def coinbase_ws():
    global cvd, best_bid, best_ask, bid_size_1, ask_size_1

    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                sub_msg = {
                    "type": "subscribe",
                    "product_ids": [TICKER],
                    "channels": ["matches", "level2", "ticker"]
                }
                await ws.send(json.dumps(sub_msg))
                last_period = int(time.time() // rv_window_secs)

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "match":
                        # trade event
                        ts = time.time()
                        size = float(msg.get("size", 0))
                        side = msg.get("side")  # 'buy' means taker buy (aggressive buy)
                        price = float(msg.get("price", 0))
                        trades.append((ts, price, size, side))

                        # update CVD: buys positive, sells negative
                        if side == "buy":
                            cvd += size
                        elif side == "sell":
                            cvd -= size

                        # maintain 5m volume history
                        current_period = int(ts // rv_window_secs)
                        if current_period != last_period:
                            # compute the finished period volume
                            cutoff = ts - rv_window_secs
                            vol_5m = sum(s for t,p,s,sd in trades if t >= cutoff)
                            history_volumes.append(vol_5m)
                            last_period = current_period

                    elif msg.get("type") == "l2update":
                        # compute best bid/ask and size at top
                        changes = msg.get("changes", [])
                        # We don't maintain full book for simplicity; track NBBO approximation
                        # Coinbase l2update provides [side, price, size] changes
                        # We'll estimate best from last 'ticker' when available; here we only refresh sizes if price equals best
                        pass

                    elif msg.get("type") == "ticker":
                        # best bid/ask from ticker
                        try:
                            best_bid = float(msg.get("best_bid")) if msg.get("best_bid") is not None else best_bid
                            best_ask = float(msg.get("best_ask")) if msg.get("best_ask") is not None else best_ask
                        except Exception:
                            pass

        except Exception as e:
            # simple backoff on disconnects
            await asyncio.sleep(2)

@app.on_event("startup")
async def startup_event():
    # create templates dir at runtime if not present
    tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
    os.makedirs(tpl_dir, exist_ok=True)

    # start WS consumer
    asyncio.create_task(coinbase_ws())
