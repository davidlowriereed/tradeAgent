
from __future__ import annotations
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Dict, Any, List, Optional
import asyncio, time, json, os

from .config import SYMBOLS, ALERT_WEBHOOK_URL, SLACK_ANALYSIS_ONLY
from .signals import compute_signals, compute_signals_tf
from .scheduler import agents_loop, list_agents_last_run, AGENTS
from .state import trades, RECENT_FINDINGS
from .db import connect_async, pg_conn

app = FastAPI(title="Opportunity Radar")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

@app.on_event("startup")
async def _startup():
    # Start agents loop
    asyncio.create_task(agents_loop())

@app.get("/", response_class=HTMLResponse)
async def root():
    try:
        index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception:
        return HTMLResponse("<h1>Dashboard missing</h1>", status_code=200)

@app.get("/health")
async def health():
    try:
        agents_map = list_agents_last_run()
    except Exception:
        agents_map = {}
    return {
        "status": "ok",
        "symbols": SYMBOLS,
        "trades_cached": {sym: len(list(trades.get(sym, []))) for sym in SYMBOLS},
        "agents": agents_map,
    }

@app.get("/db-health")
async def db_health():
    try:
        await connect_async()
        return {"ok": pg_conn is not None}
    except Exception:
        return {"ok": False, "error": "no_connection"}

@app.get("/signals")
async def signals(symbol: str):
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"unknown symbol: {symbol}")
    return compute_signals(symbol)

@app.get("/signals_tf")
async def signals_tf(symbol: str):
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"unknown symbol: {symbol}")
    return compute_signals_tf(symbol)

@app.get("/findings")
async def findings(symbol: Optional[str] = None, limit: int = 20):
    out = []
    # Try in-memory buffer first (DB wiring is optional here)
    for f in list(RECENT_FINDINGS)[-limit:][::-1]:
        if symbol and f.get("symbol") != symbol:
            continue
        out.append({
            "ts_utc": None,
            "agent": f.get("agent"),
            "symbol": f.get("symbol"),
            "score": f.get("score"),
            "label": f.get("label"),
            "details": f.get("details") or {},
        })
        if len(out) >= limit:
            break
    return {"findings": out}

@app.get("/agents")
async def agents():
    m = list_agents_last_run()
    return {"agents": [{"agent": k, "last_run": v.get("last_run")} for k, v in m.items()]}

@app.post("/agents/run-now")
async def agents_run_now(names: str, symbol: str, insert: bool = True):
    # Minimal synchronous trigger: find agents and call run_once
    names_set = {n.strip() for n in names.split(",") if n.strip()}
    ran = []
    results = []
    for agent in AGENTS:
        if agent.name in names_set:
            try:
                finding = await agent.run_once(symbol)
                ran.append(agent.name)
                if finding:
                    results.append({"agent": agent.name, "finding": finding})
                    if insert:
                        from .db import insert_finding
                        insert_finding(agent.name, symbol, float(finding.get("score", 0.0)), finding.get("label", agent.name), finding.get("details") or {})
            except Exception as e:
                results.append({"agent": agent.name, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "ran": ran, "results": results}
