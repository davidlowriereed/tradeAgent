import asyncio
import csv
import io
import json
import os
import time
from collections import defaultdict, deque
from typing import Dict, Deque, Tuple, Optional
import httpx

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
import websockets

DB_URL = os.getenv("DATABASE_URL", None)
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", os.getenv("SYMBOL", "BTC-USD")).split(",") if s.strip()]
WS_URL = "wss://ws-feed.exchange.coinbase.com"


# ---------- Optional Postgres ----------
pg_conn = None
async def pg_connect():
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS findings (
                    id BIGSERIAL PRIMARY KEY,
                    agent TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    score DOUBLE PRECISION NOT NULL,
                    label TEXT NOT NULL,
                    details JSONB DEFAULT '{}'::jsonb
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id BIGSERIAL PRIMARY KEY,
                    agent TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    status TEXT NOT NULL,
                    note TEXT
                );
                """
            )
        return pg_conn
    except Exception:
        pg_conn = None
        return None
        
class Agent:
    name: str
    interval_sec: int

    def __init__(self, name, interval_sec=10):
        self.name = name
        self.interval_sec = interval_sec

    async def run_once(self, symbol) -> Optional[dict]:
        """Return a finding dict or None."""
        raise NotImplementedError

class RVOLSpikeAgent(Agent):
    """Manipulation Hunter v0 — flags high relative volume bursts."""
    def __init__(self, min_rvol=2.5, interval_sec=10):
        super().__init__("rvol_spike", interval_sec)
        self.min_rvol = float(os.getenv("AGENT_RVOL_MIN", str(min_rvol)))

    async def run_once(self, symbol):
        # reuse compute_signals() for per-symbol features
        sig = compute_signals(symbol)
        rvol = sig.get("rvol_vs_recent") or 0.0
        if rvol >= self.min_rvol and sig["volume_5m"] > 0:
            score = min(10.0, rvol)  # simple score
            label = "rvol_spike"
            details = {
                "rvol": rvol,
                "volume_5m": sig["volume_5m"],
                "best_bid": sig["best_bid"],
                "best_ask": sig["best_ask"],
            }
            return {"score": score, "label": label, "details": details}
        return None

class CVDDivergenceAgent(Agent):
    """Trap Spotter v0 — price up while CVD down (or reverse) over ~2 minutes."""
    def __init__(self, window_sec=120, min_abs_cvd=25.0, min_price_bps=10, interval_sec=10):
        super().__init__("cvd_divergence", interval_sec)
        self.win = int(os.getenv("AGENT_CVD_WIN_SEC", str(window_sec)))
        self.min_abs_cvd = float(os.getenv("AGENT_CVD_MIN", str(min_abs_cvd)))
        self.min_price_bps = float(os.getenv("AGENT_CVD_MIN_BPS", str(min_price_bps)))

    async def run_once(self, symbol):
        # snapshot window
        now = time.time()
        window = [row for row in trades[symbol] if row[0] >= now - self.win]
        if len(window) < 10:
            return None

        p0 = window[0][1]
        p1 = window[-1][1]
        price_change_bps = (p1 - p0) / p0 * 1e4  # basis points

        # CVD delta over the window
        # (approx: recompute CVD delta from subset instead of whole deque)
        cvd_delta = 0.0
        for _, _, size, side in window:
            if side == "buy":
                cvd_delta += size
            elif side == "sell":
                cvd_delta -= size

        # Divergence: price ↑ & CVD ↓  OR price ↓ & CVD ↑
        diverges = (price_change_bps > self.min_price_bps and cvd_delta < -self.min_abs_cvd) or \
                   (price_change_bps < -self.min_price_bps and cvd_delta > self.min_abs_cvd)

        if diverges:
            score = min(10.0, abs(price_change_bps) / self.min_price_bps + abs(cvd_delta) / self.min_abs_cvd)
            label = "cvd_divergence"
            details = {
                "price_bps": price_change_bps,
                "cvd_delta": cvd_delta,
                "window_sec": self.win
            }
            return {"score": score, "label": label, "details": details}
        return None

class MacroWatcher(Agent):
    def __init__(self, interval_sec=600):
        super().__init__("macro_watcher", interval_sec)
        self.url = os.getenv("MACRO_FEED_URL")

    async def run_once(self, symbol):
        if not self.url: 
            return None
        try:
            import httpx, re, datetime
            r = await httpx.AsyncClient(timeout=10).get(self.url)
            text = r.text[:100000]
            # naive parse for items (works for many RSS feeds)
            items = []
            for m in re.finditer(r"<item>.*?<title>(.*?)</title>.*?</item>", text, re.S):
                title = re.sub("<.*?>","",m.group(1))
                items.append(title.strip())
            if items:
                return {"score": 1.0, "label": "macro_update", "details": {"items": items[:5]}}
        except Exception:
            pass
        return None

AGENTS = [
    RVOLSpikeAgent(), 
    CVDDivergenceAgent(),
    MacroWatcher(),          # harmless if MACRO_FEED_URL isn’t set
]

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
        pass
def pg_insert_finding(agent, symbol, score, label, details):
    if pg_conn is None:
        return
    try:
        import json as _json
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO findings (agent, symbol, score, label, details) VALUES (%s,%s,%s,%s,%s)",
                (agent, symbol, score, label, _json.dumps(details or {})),
            )
    except Exception:
        pass

def pg_agent_heartbeat(agent, status="ok", note=None):
    if pg_conn is None:
        return
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_runs (agent, status, note) VALUES (%s,%s,%s)",
                (agent, status, note),
            )
    except Exception:
        pass
        
# ---------- In-memory state (per symbol) ----------
MAX_TICKS = 5000
RV_SECS = 300  # 5 minutes

trades: Dict[str, Deque[Tuple[float,float,float,Optional[str]]]] = defaultdict(lambda: deque(maxlen=MAX_TICKS))
cvd: Dict[str, float] = defaultdict(float)
hist_5m: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=48))
best_bid: Dict[str, Optional[float]] = defaultdict(lambda: None)
best_ask: Dict[str, Optional[float]] = defaultdict(lambda: None)
db_buffer: Dict[str, Deque[Tuple[str,str,float,float,Optional[str]]]] = defaultdict(lambda: deque(maxlen=20000))

# ---------- Alerts (webhook is optional) ----------
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")  # Slack/Discord webhook
ALERT_MIN_RVOL = float(os.getenv("ALERT_MIN_RVOL", "2.0"))      # e.g., 2.0x
ALERT_CVD_DELTA = float(os.getenv("ALERT_CVD_DELTA", "50"))     # abs change in CVD within 5m window
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "180"))# per symbol

last_alert_ts: Dict[str, float] = defaultdict(lambda: 0.0)

app = FastAPI()

# tiny HTML page
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape())

async def agents_loop():
    while True:
        try:
            if DB_URL and pg_conn is None:
                await pg_connect()

            for agent in AGENTS:
                for sym in SYMBOLS:
                    try:
                        finding = await agent.run_once(sym)
                        pg_agent_heartbeat(agent.name, "ok")
                        if finding:
                            pg_insert_finding(agent.name, sym, finding["score"], finding["label"], finding["details"])

                            # also push an alert if score is high
                            if ALERT_WEBHOOK_URL and finding["score"] >= 2.0:  # tune this
                                txt = f"🤖 {agent.name.upper()} | {sym} | {finding['label']} | score={finding['score']:.2f}"
                                try:
                                    await httpx.AsyncClient(timeout=10).post(ALERT_WEBHOOK_URL, json={"text": txt, "content": txt})
                                except Exception:
                                    pass
                    except Exception as e:
                        pg_agent_heartbeat(agent.name, "error", note=str(e)[:150])
            await asyncio.sleep(5)
        except Exception:
            await asyncio.sleep(5)

@app.get("/test-alert")
async def test_alert():
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return {"ok": False, "reason": "ALERT_WEBHOOK_URL not set"}
    msg = {"text": "✅ Test alert from Opportunity Radar", "content": "✅ Test alert from Opportunity Radar"}
    try:
        await httpx.AsyncClient(timeout=10).post(url, json=msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok", "symbols": SYMBOLS, "trades_cached": {s: len(trades[s]) for s in SYMBOLS}}

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

def compute_signals(sym: str):
    now = time.time()
    cutoff = now - RV_SECS
    vol_5m = sum(sz for ts, _, sz, _ in trades[sym] if ts >= cutoff)
    baseline = sorted(hist_5m[sym])
    med = baseline[len(baseline)//2] if baseline else 0.0
    rvol = (vol_5m / med) if med > 0 else None
    return {
        "symbol": sym,
        "cvd": cvd[sym],
        "volume_5m": vol_5m,
        "rvol_vs_recent": rvol,
        "best_bid": best_bid[sym],
        "best_ask": best_ask[sym],
        "trades_cached": len(trades[sym]),
        "timestamp": now
    }

@app.get("/signals")
async def signals(symbol: str = Query(None)):
    sym = symbol or SYMBOLS[0]
    return JSONResponse(compute_signals(sym))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = env.get_template("index.html")
    return template.render(symbols=SYMBOLS)

@app.get("/agents")
async def agents_status():
    # latest run per agent
    if pg_conn is None:
        await pg_connect()
    rows = []
    try:
        with pg_conn.cursor() as cur:
            cur.execute("""
                SELECT agent, max(ts_utc) AS last_run
                FROM agent_runs
                GROUP BY agent
                ORDER BY agent
            """)
            rows = cur.fetchall()
    except Exception:
        pass
    return {"agents": [{"agent": a, "last_run": str(t)} for a,t in rows]}

@app.get("/findings")
async def get_findings(symbol: Optional[str]=None, agent: Optional[str]=None, limit: int=50):
    if pg_conn is None:
        await pg_connect()
    result = []
    try:
        q = "SELECT ts_utc, agent, symbol, score, label, details FROM findings"
        cond, vals = [], []
        if symbol:
            cond.append("symbol=%s"); vals.append(symbol)
        if agent:
            cond.append("agent=%s"); vals.append(agent)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY ts_utc DESC LIMIT %s"; vals.append(max(1, min(limit, 200)))
        with pg_conn.cursor() as cur:
            cur.execute(q, vals)
            for ts, a, s, sc, lb, det in cur.fetchall():
                result.append({
                    "ts_utc": str(ts), "agent": a, "symbol": s,
                    "score": float(sc), "label": lb, "details": det
                })
    except Exception:
        pass
    return {"findings": result}

@app.get("/export-csv")
async def export_csv(symbol: str = Query(None), n: int = 2000):
    sym = symbol or SYMBOLS[0]
    n = max(1, min(n, len(trades[sym])))
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["ts_epoch","price","size","side"])
    for row in list(trades[sym])[-n:]:
        w.writerow(row)
    output.seek(0)
    return StreamingResponse(output, media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={sym}-trades.csv"})

async def coinbase_ws(sym: str):
    """One websocket consumer per symbol."""
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"type":"subscribe","product_ids":[sym],"channels":["matches","ticker"]}))
                last_period = int(time.time() // RV_SECS)
                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "match":
                        ts = time.time()
                        size = float(msg.get("size", 0) or 0.0)
                        side = msg.get("side")
                        price = float(msg.get("price", 0) or 0.0)
                        trades[sym].append((ts, price, size, side))
                        if side == "buy":
                            cvd[sym] += size
                        elif side == "sell":
                            cvd[sym] -= size
                        if DB_URL:
                            db_buffer[sym].append((sym, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts)), price, size, side))

                        # roll 5-min baseline
                        period = int(ts // RV_SECS)
                        if period != last_period:
                            cutoff = ts - RV_SECS
                            vol = sum(s for t,_,s,_ in trades[sym] if t >= cutoff)
                            hist_5m[sym].append(vol)
                            last_period = period

                    elif msg.get("type") == "ticker":
                        try:
                            if msg.get("best_bid") is not None:
                                best_bid[sym] = float(msg["best_bid"])
                            if msg.get("best_ask") is not None:
                                best_ask[sym] = float(msg["best_ask"])
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
                # flush up to 1000 rows per symbol
                for sym in SYMBOLS:
                    batch = []
                    while db_buffer[sym] and len(batch) < 1000:
                        batch.append(db_buffer[sym].popleft())
                    if batch:
                        pg_insert_many(batch)
        except Exception:
            pass
        await asyncio.sleep(3)

async def alert_loop():
    """Simple alert: RVOL spike OR fast CVD change."""
    if not ALERT_WEBHOOK_URL:
        return  # alerts disabled
    import httpx
    last_cvd_sample: Dict[str, Tuple[float,float]] = {}  # sym -> (ts, cvd)

    while True:
        try:
            now = time.time()
            for sym in SYMBOLS:
                sig = compute_signals(sym)
                rv = sig["rvol_vs_recent"] or 0.0

                # CVD delta over ~5m
                prev = last_cvd_sample.get(sym)
                cvd_delta = 0.0
                if prev and now - prev[0] >= RV_SECS:
                    cvd_delta = abs(sig["cvd"] - prev[1])
                    last_cvd_sample[sym] = (now, sig["cvd"])
                elif not prev:
                    last_cvd_sample[sym] = (now, sig["cvd"])

                should_alert = (rv >= ALERT_MIN_RVOL) or (cvd_delta >= ALERT_CVD_DELTA)
                if should_alert and (now - last_alert_ts[sym] >= ALERT_COOLDOWN_SEC):
                    text = f"🔔 {sym} | RVOL: {rv:.2f}x | CVDΔ(5m): {cvd_delta:.2f} | Bid/Ask: {sig['best_bid']} / {sig['best_ask']}"
                    try:
                        # Slack & Discord both accept simple JSON webhooks
                        await httpx.AsyncClient(timeout=10).post(ALERT_WEBHOOK_URL, json={"text": text, "content": text})
                        last_alert_ts[sym] = now
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup_event():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    for sym in SYMBOLS:
        asyncio.create_task(coinbase_ws(sym))
    asyncio.create_task(db_flush_loop())
    asyncio.create_task(alert_loop())
    asyncio.create_task(agents_loop())

    
