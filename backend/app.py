# --- Local imports ---
from .config import SYMBOLS
from .signals import compute_signals
from .db import (
    connect_async, ensure_schema, insert_finding,
    fetch_findings, list_agents_last_run, pg_conn
)
from .scheduler import agents_loop, AGENTS
from .services.market import market_loop
from . import state as pos_state

# --- Standard library imports ---
import os
import io
import csv
import json
import math
import time
import asyncio
import traceback
from pathlib import Path

# --- Third-party imports ---
import httpx
from typing import Optional
from fastapi import FastAPI, APIRouter, Request, Query, Body
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


# =========================================================
# App setup
# =========================================================

app = FastAPI(title="Opportunity Radar")

# Templates / static dirs
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Optional GitHub webhook router
try:
    from .github_webhook import router as github_router
    app.include_router(github_router)
except Exception:
    pass


# =========================================================
# Helpers
# =========================================================

def _num(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default

def _sigmoid(x: float) -> float:
    try: return 1/(1+math.exp(-x))
    except OverflowError: return 0.0 if x < 0 else 1.0

# =========================================================
# Routes
# =========================================================

@app.get("/version")
async def version():
    return {
        "git_sha": os.getenv("GIT_SHA", ""),
        "build_time": os.getenv("BUILD_TIME", ""),
        "mode": os.getenv("MODE", "realtime"),
    }


@app.on_event("startup")
async def _startup():
    await connect_async()
    ensure_schema()
    asyncio.create_task(market_loop(), name="market_loop")
    asyncio.create_task(agents_loop(), name="agents_loop")


# ---------------------------------------------------------
# Basic pages
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Signal endpoints
# ---------------------------------------------------------

@app.get("/trend_now")
async def trend_now(symbol: str = Query(...)):
    from .bars import build_bars, px_vs_vwap_bps, momentum_bps
    import math
    def sigmoid(x): 
        try: return 1/(1+math.exp(-x))
        except OverflowError: return 0.0 if x < 0 else 1.0

    b1  = build_bars(symbol, "1m", 60)
    b5  = build_bars(symbol, "5m", 240)
    if not b1:
        return {"symbol": symbol, "p_up": 0.0, "p_1m": 0.0, "p_5m": 0.0, "p_15m": 0.0, "live": True}

    mom1 = (momentum_bps(b1, 1) or 0.0)/10
    mom5 = (momentum_bps(b5, 1) or 0.0)/10
    pxv1 = (px_vs_vwap_bps(b1, 20) or 0.0)/10

    p1  = sigmoid(0.08*mom1 + 0.03*pxv1)
    p5  = sigmoid(0.06*mom5 + 0.03*pxv1)
    p15 = sigmoid(0.04*mom5 + 0.02*pxv1)
    p_up = max(0.0, min(1.0, 0.5*p1 + 0.35*p5 + 0.15*p15))
    return {"symbol": symbol, "p_up": round(p_up,4), "p_1m": round(p1,4), "p_5m": round(p5,4), "p_15m": round(p15,4), "live": True}

@app.get("/signals")
async def signals(symbol: str = Query(...)):
    """
    Stable JSON for the header/footer cards. All numeric fields are guaranteed finite.
    Quotes are synthesized from last price if the feed hasn't sent bid/ask yet.
    """
    from .signals import compute_signals
    from .state import get_best_quotes, get_last_price, trades

    sig = compute_signals(symbol) or {}

    out = {
        "symbol": symbol,
        "mom1_bps":       _num(sig.get("mom1_bps")),
        "mom5_bps":       _num(sig.get("mom5_bps")),
        "rvol_vs_recent": _num(sig.get("rvol_vs_recent")),
        "px_vs_vwap_bps": _num(sig.get("px_vs_vwap_bps")),
        "best_bid":       sig.get("best_bid"),
        "best_ask":       sig.get("best_ask"),
        "last_price":     sig.get("last_price"),
        "trades_cached":  len(list(trades.get(symbol, []))),
    }

    # Backfill quotes if missing or non-numeric
    bb = out["best_bid"]; ba = out["best_ask"]
    if not isinstance(bb, (int, float)): bb = None
    if not isinstance(ba, (int, float)): ba = None

    if bb is None or ba is None:
        q = get_best_quotes(symbol)
        if q:
            qb, qa = q
            if isinstance(qb, (int, float)): bb = qb
            if isinstance(qa, (int, float)): ba = qa

    if bb is None or ba is None:
        lp = out["last_price"]
        if not isinstance(lp, (int, float)):
            lp = get_last_price(symbol)
        if isinstance(lp, (int, float)):
            # synthesize a tiny spread so the UI has something useful
            bb = round(lp * 0.9998, 2)
            ba = round(lp * 1.0002, 2)

    out["best_bid"] = bb
    out["best_ask"] = ba

    return out


@app.get("/signals_tf")
async def signals_tf(symbol: str = Query(...)):
    # Hardened: returns numbers even if compute_signals_tf fails
    from .signals import compute_signals_tf
    try:
        tf = compute_signals_tf(symbol, ["1m", "5m", "15m"]) or {}
    except Exception:
        from .bars import build_bars, px_vs_vwap_bps, momentum_bps, atr
        b1  = build_bars(symbol, "1m", 60)
        b5  = build_bars(symbol, "5m", 240)
        b15 = build_bars(symbol, "15m", 480)
        tf = {
            "mom_bps_1m":        momentum_bps(b1, 1),
            "mom_bps_5m":        momentum_bps(b5, 1),
            "mom_bps_15m":       momentum_bps(b15,1),
            "px_vs_vwap_bps_1m": px_vs_vwap_bps(b1, 20),
            "atr_1m":            atr(b1, 14),
        }
    return {
        "symbol": symbol,
        "mom_bps_1m":         _num(tf.get("mom_bps_1m")),
        "mom_bps_5m":         _num(tf.get("mom_bps_5m")),
        "mom_bps_15m":        _num(tf.get("mom_bps_15m")),
        "px_vs_vwap_bps_1m":  _num(tf.get("px_vs_vwap_bps_1m")),
        "px_vs_vwap_bps_5m":  _num(tf.get("px_vs_vwap_bps_5m")),
        "px_vs_vwap_bps_15m": _num(tf.get("px_vs_vwap_bps_15m")),
        "rvol_1m":            _num(tf.get("rvol_1m")),
        "rvol_5m":            _num(tf.get("rvol_5m")),
        "rvol_15m":           _num(tf.get("rvol_15m")),
        "atr_1m":             _num(tf.get("atr_1m")),
        "atr_5m":             _num(tf.get("atr_5m")),
        "atr_15m":            _num(tf.get("atr_15m")),
    }

debug = APIRouter()

@debug.get("/state")
async def debug_state(symbol: str = Query(...)):
    from .state import trades
    from .bars import build_bars
    rows = list(trades.get(symbol, []))
    last = rows[-1] if rows else None
    age  = (time.time() - last[0]) if last else None
    b1   = build_bars(symbol, "1m", 60)
    return {
        "symbol": symbol,
        "trades_cached": len(rows),
        "last_trade": last,                       # [ts, price, size, side]
        "last_trade_age_s": round(age, 3) if age else None,
        "bars_1m_count": len(b1),
        "bars_1m_tail": b1[-3:],
    }

@debug.get("/features")
async def debug_features(symbol: str = Query(...)):
    from .bars import build_bars, px_vs_vwap_bps, momentum_bps, atr
    b1  = build_bars(symbol, "1m", 60)
    b5  = build_bars(symbol, "5m", 240)
    b15 = build_bars(symbol, "15m", 480)
    return {
        "symbol": symbol,
        "bars": {"1m": len(b1), "5m": len(b5), "15m": len(b15)},
        "mom_bps_1m":        _num(momentum_bps(b1, 1)),
        "mom_bps_5m":        _num(momentum_bps(b5, 1)),
        "px_vs_vwap_bps_1m": _num(px_vs_vwap_bps(b1, 20)),
        "atr_1m":            _num(atr(b1, 14)),
    }

app.include_router(debug, prefix="/debug", tags=["debug"])

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # expects backend/templates/index.html
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/debug/state")
async def debug_state(symbol: str = Query(...)):
    from .state import trades
    from .bars import build_bars
    rows = list(trades.get(symbol, []))
    last = rows[-1] if rows else None
    age = (time.time() - last[0]) if last else None
    bars1 = build_bars(symbol, "1m", 60)
    return {
        "symbol": symbol,
        "trades_cached": len(rows),
        "last_trade": last,
        "last_trade_age_s": round(age, 3) if age else None,
        "bars_1m_count": len(bars1),
        "bars_1m_tail": bars1[-3:],
    }


@app.get("/debug/features")
async def debug_features(symbol: str = Query(...)):
    from .bars import build_bars, px_vs_vwap_bps, momentum_bps, atr
    b1 = build_bars(symbol, "1m", 60)
    b5 = build_bars(symbol, "5m", 240)
    b15 = build_bars(symbol, "15m", 480)

    def num(x):
        try:
            v = float(x)
            return v if (v == v and v not in (float("inf"), float("-inf"))) else 0.0
        except:
            return 0.0

    return {
        "symbol": symbol,
        "bars": {"1m": len(b1), "5m": len(b5), "15m": len(b15)},
        "mom_bps_1m":        num(momentum_bps(b1, 1)),
        "mom_bps_5m":        num(momentum_bps(b5, 1)),
        "px_vs_vwap_bps_1m": num(px_vs_vwap_bps(b1, 20)),
        "atr_1m":            num(atr(b1, 14)),
    }

# backend/app.py
@app.get("/llm-health")
async def llm_health():
    import os, httpx
    key = os.getenv("OPENAI_API_KEY","").strip()
    base = os.getenv("OPENAI_BASE_URL","").strip()
    if not key:
        return {"ok": False, "reason": "missing_api_key"}
    url = base.rstrip("/") + "/v1/models" if base else "https://api.openai.com/v1/models"
    try:
        headers = {"Authorization": f"Bearer {key}"}
        with httpx.Client(timeout=5) as cli:
            r = cli.get(url, headers=headers)
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}"}

# ---------------------------------------------------------
# Data access endpoints
# ---------------------------------------------------------

@app.get("/findings")
async def findings(
    symbol: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500)
):
    """
    Returns most recent findings, filtered by symbol and/or agent if provided.
    """
    try:
        from .db import recent_findings  # your DB accessor
        rows = recent_findings(symbol=symbol, agent=agent, limit=limit)
        return {"findings": rows}
    except Exception:
        # if DB is down, fall back to in-memory buffer if you keep one
        from .state import RECENT_FINDINGS  # optional ring buffer [(ts, agent, sym, ...)]
        out = []
        for row in reversed(list(RECENT_FINDINGS)):
            if symbol and row["symbol"] != symbol:
                continue
            if agent and row["agent"] != agent:
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return {"findings": out}


@app.get("/agents")
async def agents():
    return {"agents": list_agents_last_run()}


@app.get("/health")
async def health():
    from .config import SYMBOLS
    from .state import trades
    try:
        from .scheduler import AGENTS_STATE
        agents = AGENTS_STATE()
    except Exception:
        # fallback: show names with null last_run, still return 200
        from .scheduler import AGENTS
        agents = {a.name: {"status": "unknown", "last_run": None} for a in AGENTS}
    return {
        "status": "ok",
        "symbols": SYMBOLS,
        "trades_cached": {sym: len(list(trades.get(sym, []))) for sym in SYMBOLS},
        "agents": agents,
    }



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
    w.writerow(["ts_utc", "agent", "symbol", "score", "label", "details"])
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
            r = await client.get(base + "/models", headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}"})
            return {"ok": r.status_code < 500, "status": r.status_code, "base": base, "snippet": r.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e), "base": base}


# ---------------------------------------------------------
# Position endpoints
# ---------------------------------------------------------

@app.get("/position")
def get_position(symbol: str = Query(...)):
    return pos_state.get_position(symbol)


@app.post("/position")
async def set_position(payload: dict = Body(...)):
    sym = payload.get("symbol")
    st = payload.get("status")
    qty = float(payload.get("qty") or 0)
    ap = payload.get("avg_price")
    if not sym or st not in ("flat", "long", "short"):
        return {"ok": False, "error": "symbol/status required"}
    await pos_state.set_position(sym, st, qty, ap)
    return {"ok": True, "position": pos_state.get_position(sym)}


# ---------------------------------------------------------
# Agent triggers
# ---------------------------------------------------------

from fastapi import Query
from typing import Optional

# ring buffer fallback
from collections import deque
from .state import RECENT_FINDINGS  # define as deque(maxlen=1000) in state.py

@app.post("/agents/run-now")
async def agents_run_now(
    names: Optional[str] = None,
    agent: Optional[str] = None,
    symbol: str = Query(...),
    insert: bool = True,
):
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
                try:
                    insert_finding(a.name, symbol, float(f["score"]), f["label"], f.get("details") or {})
                except Exception:
                    # DB down? Push to in-memory so /findings still shows it
                    RECENT_FINDINGS.append({
                        "ts_utc": f.get("ts_utc"),
                        "agent": a.name,
                        "symbol": symbol,
                        "score": float(f["score"]),
                        "label": f["label"],
                        "details": f.get("details") or {},
                    })
            results.append({"agent": a.name, "finding": f})
        except NotImplementedError as e:
            results.append({"agent": a.name, "error": f"NotImplementedError: {e}"})
        except Exception as e:
            results.append({"agent": a.name, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "ran": [a.name for a in chosen], "results": results}


# ---------------------------------------------------------
# Liquidity
# ---------------------------------------------------------

# backend/app.py
@app.get("/liquidity")
async def liquidity():
    from .state import get_best_quotes, get_last_price
    import math

    score = None
    risk_on = False
    detail = "no quotes"

    # Pull quotes or synthesize from last price
    b,a = (None, None)
    q = get_best_quotes("BTC-USD")  # or loop symbols and return a dict
    if q: b,a = q
    lp = get_last_price("BTC-USD")

    if (isinstance(b,(int,float)) and isinstance(a,(int,float)) and math.isfinite(b) and math.isfinite(a)):
        spread = max(0.0, float(a) - float(b))
        mid = (a + b)/2.0
        spread_bps = (spread / max(mid,1e-9)) * 1e4
        # Simple scoring: tighter spreads => higher score
        # 0–5 bps => 100, 10 bps => 50, 25 bps => 0
        score = max(0, min(100, 100 - (spread_bps*4)))
        risk_on = spread_bps <= 8
        detail = f"spread_bps={spread_bps:.1f}"
    elif isinstance(lp,(int,float)):
        # fallback: no quotes, but we can’t compute spread meaningfully
        detail = "no best bid/ask; using last price only"

    return {"risk_on": bool(risk_on), "liquidity_score": int(score) if score is not None else None, "detail": detail}
