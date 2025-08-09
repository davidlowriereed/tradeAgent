import asyncio
import csv
import io
import json
import os
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
import websockets

DB_URL = os.getenv("DATABASE_URL", None)

# Optional Postgres (lazy import so app runs without it)
pg_conn = None

async def pg_connect():
    """Connect and create table if needed."""
    global pg_conn
    if DB_URL is None:
        return None
    try:
        import psycopg2
        pg_conn = psycopg2.connect(DB_URL)
        pg_conn.autocommit = True
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trades_crypto (
                    id BIGSERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL,
                    price DOUBLE PRECISION,
                    size DOUBLE PRECISION,
                    side TEXT
                );
                """
            )
        return pg_conn
    except Exception:
        pg_conn = None
        return None

def pg_insert_many(rows):
    if pg_conn is None or not rows:
        return
    try:
        from psycopg2.extras import execute_values
        with pg_conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO trades_crypto (symbol, ts_utc, price, size, side) VALUES %s",
                rows,
            )
    except Exception:
        # best-effort logging in alpha
        pass

# ---------------- In-memory state ----------------
TICKER = os.getenv("SYMBOL", "BTC-USD")
WS_URL = "wss://ws-feed.exchange.coinbase.com"

MAX_TICKS = 5000
trades = deque(maxlen=MAX_TICKS)      # (ts_epoch, price, size, side)
cvd = 0.0
rv_window_secs = 300                   # 5 minutes
history_volumes = deque(maxlen=48)     # 4 hours of 5-min vols

best_bid = None
best_ask = None

# buffer for DB inserts
db_buffer = deque(maxlen=20000)

app = FastAPI()

# tiny HTML dashboard with Jinja
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATE_DIR),
                  autoescape=select_autoescape())

@app.get("/health")
async def health():
    return {"status": "ok", "symbol": TICKER, "trades_cached": len(trades)}

@app.get("/db-health")
async def db_health():
    if DB_URL is None:
        return {"db": "not-configured"}
    ok = False
    try:
        if pg_conn is None:
            await pg_connect()
        if pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
                ok = True
    except Exception:
        ok = False
    return {"db": "ok" if ok else "error"}

@app.get("/signals")
async def signals():
    now = time.time()
    cutoff = now - rv_window_secs
    vol_5m = sum(size for ts, _, size, _ in trades if ts >= cutoff)
    baseline = sorted(history_volumes)
    baseline_med = baseline[len(baseline)//2] if baseline else 0.0
    rvol = (vol_5m / baseline_med) if baseline_med > 0 else None

    payload = {
        "symbol": TICKER,
        "cvd": cvd,
        "volume_5m": vol_5m,
        "rvol_vs_recent": rvol,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "trades_cached": len(trades),
        "timestamp": now
    }
    return JSONResponse(payload)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = env.get_template("index.html")
    return template.render(symbol=TICKER)

@app.get("/export-csv")
async def export_csv(n: int = 2000):
    n = max(1, min(n, len(trades)))
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["ts_epoch","price","size","side"])
    for row in list(trades)[-n:]:
        w.writerow(row)
    output.seek(0)
    return StreamingResponse(output, media_type="text/csv",
                             headers={"Content-Disposition":"attachment; filename=trades.csv"})

async def coinbase_ws():
    global cvd, best_bid, best_ask
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                sub = {"type":"subscribe","product_ids":[TICKER],"channels":["matches","ticker"]}
                await ws.send(json.dumps(sub))
                last_period = int(time.time() // rv_window_secs)

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "match":
                        ts = time.time()
                        size = float(msg.get("size", 0) or 0)
                        side = msg.get("side")
                        price = float(msg.get("price", 0) or 0)
                        trades.append((ts, price, size, side))

                        # CVD
                        if side == "buy":
                            cvd += size
                        elif side == "sell":
                            cvd -= size

                        # DB buffer row (ISO UTC)
                        if DB_URL:
                            db_buffer.append((TICKER, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts)), price, size, side))

                        # roll 5-min volume median history
                        current_period = int(ts // rv_window_secs)
                        if current_period != last_period:
                            cutoff = ts - rv_window_secs
                            vol_5m = sum(s for t,_,s,_ in trades if t >= cutoff)
                            history_volumes.append(vol_5m)
                            last_period = current_period

                    elif msg.get("type") == "ticker":
                        try:
                            if msg.get("best_bid") is not None:
                                best_bid = float(msg["best_bid"])
                            if msg.get("best_ask") is not None:
                                best_ask = float(msg["best_ask"])
                        except Exception:
                            pass
        except Exception:
            await asyncio.sleep(2)

async def db_flush_loop():
    while True:
        try:
            if DB_URL and pg_conn is None:
                await pg_connect()
            if DB_URL and pg_conn:
                batch = []
                while db_buffer and len(batch) < 1000:
                    batch.append(db_buffer.popleft())
                if batch:
                    pg_insert_many(batch)
        except Exception:
            pass
        await asyncio.sleep(3)

@app.on_event("startup")
async def startup_event():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    asyncio.create_task(coinbase_ws())
    asyncio.create_task(db_flush_loop())
