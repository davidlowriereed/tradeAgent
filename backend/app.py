# backend/app.py
from __future__ import annotations
import os, json, time, asyncio
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import SYMBOLS
from .scheduler import agents_loop, list_agents_last_run, AGENTS
from .state import RECENT_FINDINGS
from .db import db_health as db_status, connect_pool, insert_finding_row, fetch_recent_findings, get_posture, set_posture, record_trade, equity_curve

app = FastAPI(title="Opportunity Radar", default_response_class=JSONResponse)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

_last_step = {}

@app.get("/health")
async def health():
    try:
        agents_map = list_agents_last_run()
    except Exception:
        agents_map = {}
    return {"status":"ok","symbols":SYMBOLS,"agents":agents_map}

@app.get("/db-health")
async def db_health_route():
    try:
        res = await db_status()
        return res if isinstance(res, dict) else {"ok": bool(res)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/findings")
async def findings(symbol: Optional[str] = None, limit: int = 20):
    rows = await fetch_recent_findings(symbol, limit)
    for f in rows:
        if isinstance(f, dict):
            if isinstance(f.get("details"), str):
                try:
                    f["details"] = json.loads(f["details"])
                except: f["details"] = {"raw": f["details"]}
    return {"findings": rows}

AGENT_BY_NAME = {a.name: a for a in AGENTS}

@app.post("/agents/run-now")
async def run_now(names: str, symbol: str, insert: bool = False):
    out = {"ok": True, "ran": [], "results": []}
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        agent = AGENT_BY_NAME.get(name)
        if not agent:
            out["results"].append({"agent": name, "error": "unknown"})
            continue
        try:
            finding = await agent.run_once(symbol)
            if finding and insert:
                await insert_finding_row({
                    "agent": name,
                    "symbol": symbol,
                    "score": float(finding.get("score") or 0.0),
                    "label": str(finding.get("label") or name),
                    "details": finding.get("details") or {},
                })
            out["ran"].append(agent.name)
            out["results"].append({"agent": agent.name, "finding": finding})
        except Exception as e:
            out["results"].append({"agent": agent.name, "error": str(e)})
    return out

@app.post("/simulate/reset")
async def sim_reset(symbol: str):
    await set_posture(symbol, "NO_POSITION", 0, None, "RESET")
    return {"ok": True}

@app.get("/simulate/equity")
async def sim_equity(symbol: str):
    return await equity_curve(symbol)
