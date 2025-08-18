# backend/app.py
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
try:
    from fastapi.responses import ORJSONResponse  # type: ignore
    default_resp = ORJSONResponse
except Exception:
    from fastapi.responses import JSONResponse
    default_resp = JSONResponse

from fastapi.staticfiles import StaticFiles
from typing import Optional
import asyncio, os
import inspect  # for maybe_await
import json
import time

from .config import SYMBOLS, ALERT_WEBHOOK_URL, SLACK_ANALYSIS_ONLY
from .signals import compute_signals, compute_signals_tf
from .scheduler import agents_loop, list_agents_last_run, AGENTS
from .state import trades, RECENT_FINDINGS
from .db import db_health as db_status, connect_pool, ensure_schema, insert_finding, fetch_recent_findings, db_supervisor_loop
from .services.market import market_loop

# helper: works whether a function is sync or async
async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x

app = FastAPI(title="Opportunity Radar", default_response_class=default_resp)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

@app.get("/debug/env")
async def debug_env():
    def present(name: str) -> bool:
        return bool(os.getenv(name))
    def present_len(name: str) -> int:
        v = os.getenv(name)
        return len(v) if v else 0
    return {
        "LLM_ENABLE": os.getenv("LLM_ENABLE"),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL"),
        "OPENAI_API_KEY_present": present("OPENAI_API_KEY"),
        "LLM_ANALYST_ENABLED": os.getenv("LLM_ANALYST_ENABLED"),
        "DATABASE_URL_present": present("DATABASE_URL"),
        "DATABASE_CA_CERT_present": present("DATABASE_CA_CERT"),
        "DATABASE_CA_CERT_len": present_len("DATABASE_CA_CERT"),
        "DATABASE_CA_CERT_B64_present": present("DATABASE_CA_CERT_B64"),
        "DB_TLS_INSECURE": os.getenv("DB_TLS_INSECURE"),
    }

@app.get("/debug/llm")
async def debug_llm(symbol: str = "BTC-USD"):
    try:
        from openai import OpenAI
        client = OpenAI()
        rsp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": f'ping for {symbol}. Reply with {{"pong": true}}'}],
            temperature=0, max_tokens=10,
        )
        return {"ok": True, "model": rsp.model, "first": rsp.choices[0].message.content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("startup")
async def _startup():
    # Start a supervisor to keep DB healthy even if TLS/env changes later
    asyncio.create_task(db_supervisor_loop())
    asyncio.create_task(agents_loop())
    asyncio.create_task(market_loop())

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
async def db_health_route():
    # db_status may be sync or async in your codebase
    try:
        res = await _maybe_await(db_status())
        # allow either {"ok": ...} or bare bool
        return res if isinstance(res, dict) else {"ok": bool(res)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.get("/signals")
async def signals(symbol: str):
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"unknown symbol: {symbol}")
    return compute_signals(symbol)

@app.get("/signals_tf")
async def signals_tf(symbol: str):
    if symbol not in SYMBOLS:
        raise HTTPException(400, f"unknown symbol: {symbol}")
    # compute_signals_tf is async elsewhere in your code, so await it here
    return compute_signals_tf(symbol)

@app.get("/findings")
async def findings(symbol: Optional[str] = None, limit: int = 20):
    # tolerate sync/async fetch_recent_findings
    rows = await _maybe_await(fetch_recent_findings(symbol, limit))
    if rows and isinstance(rows, list) and isinstance(rows[0], dict) and "ts_utc" in rows[0]:
        return {"findings": rows}
    # fallback to legacy in-mem shape
    out = []
    for f in list(RECENT_FINDINGS)[-limit:][::-1]:
        if symbol and f.get("symbol") != symbol:
            continue
        out.append({"ts_utc": None, **f})
    return {"findings": out}

@app.get("/agents")
async def agents():
    m = list_agents_last_run()
    return {"agents": [{"agent": k, "last_run": v.get("last_run")} for k, v in m.items()]}

AGENT_BY_NAME = {a.name: a for a in AGENTS}

@app.post("/agents/run-now")
async def run_now(names: str, symbol: str, insert: bool = False):
    out = {"ok": True, "ran": [], "results": []}
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        agent = AGENT_BY_NAME.get(name)
        if not agent:
            out["results"].append({"agent": name, "error": "unknown agent"})
            continue
        try:
            finding = await agent.run_once(symbol)
            if finding and insert:
                await _maybe_await(insert_finding({
                    "agent": agent.name,
                    "symbol": symbol,
                    "score": float(finding.get("score", 0.0)),
                    "label": finding.get("label", agent.name),
                    "details": finding.get("details") or {},
                }))
            out["ran"].append(agent.name)
            out["results"].append(
                {"agent": agent.name, "finding": finding} if finding
                else {"agent": agent.name, "finding": None}
            )
        except Exception as e:
            out["results"].append({"agent": agent.name, "error": f"{type(e).__name__}: {e}"})
    return out
