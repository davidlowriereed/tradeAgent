# backend/app.py
import os, io, csv, json, asyncio, httpx
from fastapi import FastAPI, Query, Body
from fastapi.responses import HTMLResponse, StreamingResponse
from .config import SYMBOLS
from .signals import compute_signals
from .db import (
    connect_async, ensure_schema, insert_finding,
    fetch_findings, list_agents_last_run, pg_conn
)
from .scheduler import agents_loop, AGENTS
from .services.market import market_loop
from . import state as pos_state

# --- CREATE APP FIRST ---
app = FastAPI()

# Optional: include GitHub webhook router if present
try:
    from .github_webhook import router as github_router
    app.include_router(github_router)
except Exception:
    # Fine if you haven't added github_webhook.py yet
    pass

# Ensure DB schema on startup (safer to do it here than at import time)
@app.on_event("startup")
async def _startup():
    await connect_async()
    ensure_schema()
    asyncio.create_task(market_loop(), name="market_loop")
    asyncio.create_task(agents_loop(), name="agents_loop")

# ----- Routes -----

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

def _num(x, default=0.0):
    try:
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):  # NaN/inf check
            return default
        return v
    except Exception:
        return default

@app.get("/signals")
async def signals(symbol: str = Query(...)):
    from .signals import compute_signals
    from .state import trades
    sig = compute_signals(symbol) or {}
    out = {
        "symbol": symbol,
        "mom1_bps":       _num(sig.get("mom1_bps")),
        "mom5_bps":       _num(sig.get("mom5_bps")),
        "rvol_vs_recent": _num(sig.get("rvol_vs_recent")),
        "px_vs_vwap_bps": _num(sig.get("px_vs_vwap_bps")),
        "best_bid": sig.get("best_bid"),
        "best_ask": sig.get("best_ask"),
        "trades_cached": len(list(trades.get(symbol, []))),
    }
    # synthesize quote from last_price if bid/ask missing
    if out["best_bid"] is None and out["best_ask"] is None:
        try:
            lp = float(sig.get("last_price"))
            out["best_bid"] = round(lp * 0.9998, 2)
            out["best_ask"] = round(lp * 1.0002, 2)
        except Exception:
            pass
    return out

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
    w = csv.writer(out)
    w.writerow(["ts_utc","agent","symbol","score","label","details"])
    for row in fetch_findings(symbol, limit):
        w.writerow([row["ts_utc"], row["agent"], row["symbol"], row["score"], row["label"], json.dumps(row["details"])])
    out.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="findings_{symbol}.csv"'}
    return StreamingResponse(iter([out.read()]), media_type="text/csv", headers=headers)

@app.get("/llm/netcheck")
async def llm_netcheck():
    base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(base + "/models", headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY','')}" })
            return {"ok": r.status_code < 500, "status": r.status_code, "base": base, "snippet": r.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e), "base": base}

# Position endpoints (single POST to avoid duplicate route definitions)
@app.get("/position")
def get_position(symbol: str = Query(...)):
    return pos_state.get_position(symbol)

@app.post("/position")
async def set_position(
    payload: dict = Body(...),
):
    # payload = {"symbol":"BTC-USD","status":"long","qty":1.2,"avg_price":123.45}
    sym = payload.get("symbol")
    st  = payload.get("status")
    qty = float(payload.get("qty") or 0)
    ap  = payload.get("avg_price")
    if not sym or st not in ("flat","long","short"):
        return {"ok": False, "error": "symbol/status required"}
    await pos_state.set_position(sym, st, qty, ap)
    return {"ok": True, "position": pos_state.get_position(sym)}

# Manual triggers
@app.post("/agents/run-now")
async def run_now(names: str | None = None, agent: str | None = None, symbol: str = Query(default=SYMBOLS[0]), insert: bool = True):
    lookup = {a.name: a for a in AGENTS}
    if names:
        chosen = [lookup[n.strip()] for n in names.split(",") if n.strip() in lookup]
    elif agent and agent in lookup:
        chosen = [lookup[agent]]
    else:
        chosen = AGENTS

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


# --- Added lightweight data endpoints for dashboard ---
@app.get("/signals_tf")
async def signals_tf(symbol: str = Query(...)):
    from .signals import compute_signals_tf
    tf = compute_signals_tf(symbol, ["1m","5m","15m"]) or {}
    return {
        "symbol": symbol,
        "mom_bps_1m":        _num(tf.get("mom_bps_1m")),
        "mom_bps_5m":        _num(tf.get("mom_bps_5m")),
        "mom_bps_15m":       _num(tf.get("mom_bps_15m")),
        "px_vs_vwap_bps_1m": _num(tf.get("px_vs_vwap_bps_1m")),
        "px_vs_vwap_bps_5m": _num(tf.get("px_vs_vwap_bps_5m")),
        "px_vs_vwap_bps_15m":_num(tf.get("px_vs_vwap_bps_15m")),
        "rvol_1m":           _num(tf.get("rvol_1m")),
        "rvol_5m":           _num(tf.get("rvol_5m")),
        "rvol_15m":          _num(tf.get("rvol_15m")),
        "atr_1m":            _num(tf.get("atr_1m")),
        "atr_5m":            _num(tf.get("atr_5m")),
        "atr_15m":           _num(tf.get("atr_15m")),
    }

@app.get("/liquidity")
async def liquidity_state():
    try:
        from .liquidity import get_liquidity_state
        risk_on, score = get_liquidity_state()
        return {"risk_on": bool(risk_on), "liquidity_score": float(score)}
    except Exception as e:
        return {"risk_on": False, "liquidity_score": 0.0, "error": f"{type(e).__name__}: {e}"}

