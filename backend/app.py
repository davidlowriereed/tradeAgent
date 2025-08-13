
import os, io, csv, json, asyncio, httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse
from .config import SYMBOLS
from .signals import compute_signals
from .db import connect_async, fetch_findings, list_agents_last_run, pg_conn
from backend.services.market import market_loop
from .scheduler import agents_loop, AGENTS
from fastapi import Body, Query
from .state import get_position, set_position

app = FastAPI()

@app.post("/position")
async def set_pos(
    payload: dict = Body(...),
):
    # payload = {"symbol":"BTC-USD","status":"long","qty":1.2,"avg_price":123.45}
    sym = payload.get("symbol")
    st  = payload.get("status")
    qty = float(payload.get("qty") or 0)
    ap  = payload.get("avg_price")
    if not sym or st not in ("flat","long","short"):
        return {"ok": False, "error": "symbol/status required"}
    await set_position(sym, st, qty, ap)
    return {"ok": True, "position": get_position(sym)}

@app.get("/", response_class=HTMLResponse)
async def root():
    here = os.path.dirname(__file__)
    index_path = os.path.join(here, "templates", "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception as e:
        return HTMLResponse(
            "<h3>Opportunity Radar (Alpha)</h3>"
            "<p>See <a href='/signals'>/signals</a>, "
            "<a href='/findings'>/findings</a>, "
            "<a href='/health'>/health</a></p>"
            f"<pre style='color:#b00'>index error: {e}</pre>"
        )

@app.on_event("startup")
async def _startup():
    # start feed + agents
    asyncio.create_task(market_loop(), name="market_loop")
    asyncio.create_task(agents_loop(), name="agents_loop")

@app.get("/signals")
async def signals(symbol: str = Query(default=SYMBOLS[0])):
    sig = compute_signals(symbol)
    sig["symbol"] = symbol
    return sig

@app.get("/findings")
async def findings(symbol: str | None = None, limit: int = 50):
    return {"findings": fetch_findings(symbol, limit)}

@app.get("/agents")
async def agents():
    return {"agents": list_agents_last_run()}

@app.get("/health")
async def health():
    try:
        agents = {a["agent"]: {"status": "ok", "last_run": a["last_run"]} for a in list_agents_last_run()}
    except Exception:
        agents = {}
    from .state import trades
    return {"status":"ok","symbols":SYMBOLS,"trades_cached":{s:len(trades[s]) for s in SYMBOLS},"agents":agents}

@app.get("/db-health")
async def db_health():
    try:
        if pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT NOW()")
                cur.fetchone()
            return {"ok": True}
        return {"ok": False, "error": "no_connection"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/export-csv")
async def export_csv(symbol: str = Query(default=SYMBOLS[0]), limit: int = 500):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ts_utc","agent","symbol","score","label","details"])
    for row in fetch_findings(symbol, limit):
        writer.writerow([row["ts_utc"], row["agent"], row["symbol"], row["score"], row["label"], json.dumps(row["details"])])
    out.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="findings_{symbol}.csv"'}
    return StreamingResponse(iter([out.read()]), media_type="text/csv", headers=headers)

@app.get("/llm/netcheck")
async def llm_netcheck():
    base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(base + "/models", headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY','')}" })
            snip = r.text[:300]
            return {"ok": r.status_code < 500, "status": r.status_code, "base": base, "snippet": snip}
    except Exception as e:
        return {"ok": False, "error": str(e), "base": base}

# Manual triggers
@app.post("/agents/run-now")
async def run_now(names: str | None = None, agent: str | None = None, symbol: str = Query(default=SYMBOLS[0]), insert: bool = True):
    lookup = {a.name: a for a in AGENTS}
    chosen = []
    if names:
        for n in [x.strip() for x in names.split(",")]:
            if n in lookup: chosen.append(lookup[n])
    elif agent and agent in lookup:
        chosen.append(lookup[agent])
    else:
        chosen = AGENTS

    from .db import insert_finding
    results = []
    for a in chosen:
        try:
            f = await a.run_once(symbol)
            if f and insert:
                insert_finding(a.name, symbol, float(f["score"]), f["label"], f.get("details") or {})
            results.append({"agent": a.name, "finding": f})
        except NotImplementedError as e:
            results.append({"agent": a.name, "error": f"NotImplementedError: {e}"})
        except Exception as e:
            results.append({"agent": a.name, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "ran": [a.name for a in chosen], "results": results}
