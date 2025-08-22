# backend/app.py
from __future__ import annotations

from pathlib import Path
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi import Query
from .startup_checks import verify_db_data, warm_caches
import logging

import os, json, asyncio
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import SYMBOLS

from .scheduler import agents_loop, list_agents_last_run, AGENTS
from .boot import BootOrchestrator, Stage
from .db import (
    run_migrations_idempotent,
    db_health as db_status,            # <-- alias used by the /db-health route
    connect_pool,
    insert_finding_row,                # <-- used by /agents/run-now when insert=true
    fetch_recent_findings,
    set_posture,
    equity_curve,
)

from .scheduler import AGENTS
from .agents import REGISTRY

agents_task: asyncio.Task | None = None
log = logging.getLogger("startup")

app = FastAPI(title="Opportunity Radar", default_response_class=JSONResponse)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

boot: BootOrchestrator | None = None
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

_last_step = {}

@app.on_event("startup")
async def startup():
    # BootOrchestrator now *requires* these three keyword-only callbacks:
    boot = BootOrchestrator(
        db_connect=connect_pool,                   # create asyncpg pool
        run_migrations=run_migrations_idempotent, # run idempotent migrations
        start_agents=_start_agents,               # kick off agents loop
    )

    # If your class exposes a `run()` method, use it; otherwise fall back to explicit steps.
    if hasattr(boot, "run") and callable(getattr(boot, "run")):
        await boot.run()
    else:
        # Fallback for older BootOrchestrator API
        await boot.step(Stage.DB_POOL, connect_pool)
        await boot.step(Stage.DB_MIGRATIONS, run_migrations_idempotent)
        await boot.step(Stage.AGENTS, _start_agents)
        boot.ready = True
        if hasattr(boot, "set_stage"):
            boot.set_stage(Stage.READY)
        if hasattr(Stage, "DB_VERIFY"):
            await boot.step(Stage.DB_VERIFY, lambda: verify_db_data(min_total_rows=0))
        if hasattr(Stage, "CACHE_WARMUP"):
            await boot.step(Stage.CACHE_WARMUP, warm_caches)

async def _start_agents():
    """Start (or restart) the background agents loop."""
    global agents_task
    if agents_task and not agents_task.done():
        agents_task.cancel()
        try:
            await agents_task
        except Exception:
            pass
    agents_task = asyncio.create_task(agents_loop())
    return "agents_started"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
    
@app.get("/health")
async def health():
    return {"status": "up"}

@app.get("/status")
async def status():
    st = boot.state if boot else None
    return {
        "status": "up",
        "ready": bool(boot and boot.ready),
        "stage": st.stage.value if st else "BOOTING",
        "errors": st.errors if st else {},
        "attempts": st.attempts if st else {},
    }

@app.get("/db-health")
async def db_health():
    ok, mode, info = await db_status()         # db_status is the alias above
    return {"ok": ok, "mode": mode, **(info or {})}


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

def resolve_agents(agents):
    # If we already have a mapping name -> agent, just use it
    if isinstance(agents, dict):
        return agents

    # If we got a list/tuple of strings, map them through the registry
    if isinstance(agents, (list, tuple)) and all(isinstance(a, str) for a in agents):
        names = [n.strip() for n in agents if n and n.strip()]
        missing = [n for n in names if n not in REGISTRY]
        if missing:
            raise RuntimeError(
                f"Unknown agent names: {missing}. "
                f"Known agents: {sorted(REGISTRY.keys())}"
            )
        return {n: REGISTRY[n] for n in names}

    # Otherwise assume these are agent objects with .name
    return {a.name: a for a in agents}


def _agent_names_from_env() -> list[str]:
    raw = os.getenv("AGENTS") or os.getenv("AGENT_NAMES")
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    # default set – use the names that exist in agents/__init__.py REGISTRY
    return [
        "trend_score",
        "opening_drive",
        "session_reversal",
        "rvol_spike",
        "cvd_divergence",
        "llm_analyst",
        "macro_watcher",
        "posture_guard",
    ]

# Create instances from the registry
AGENTS = [REGISTRY[name]() for name in _agent_names_from_env()]

# Fast lookup for the /agents/run-now endpoint
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

@app.get("/signals")
async def signals(symbol: str = Query(..., description="e.g. BTC-USD")):
    # TODO: replace with real query once features_1m populated
    return {"symbol": symbol, "last_price": None, "trend_p_up": None}

@app.get("/signals_tf")
async def signals_tf(symbol: str = Query(...)):
    # TODO: replace with latest features_1m snapshot
    return {
        "symbol": symbol,
        "mom_bps_1m": 0, "px_vs_vwap_bps_1m": 0,
        "rvol_1m": 1.0, "atr_1m": 0
    }

@app.get("/ready")
async def ready():
    st = boot.state if boot else None
    agents_map = {}
    try:
        agents_map = list_agents_last_run()
    except Exception:
        pass
    return {
        "ready": bool(boot and boot.ready),
        "stage": st.stage.value if st else "BOOTING",
        "errors": st.errors if st else {},
        "attempts": st.attempts if st else {},
        "agents": agents_map,
    }

@app.on_event("shutdown")
async def shutdown():
    global agents_task
    if agents_task:
        agents_task.cancel()
        try:
            await agents_task
        except Exception:
            pass
