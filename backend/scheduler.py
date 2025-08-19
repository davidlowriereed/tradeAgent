from __future__ import annotations
import asyncio, os, time
from typing import Dict, List
from datetime import datetime, timezone

from .config import SYMBOLS
from .state import RECENT_FINDINGS
from .db import insert_finding_row, heartbeat

# --- Agents ---
from .agents.rvol_spike import RvolSpikeAgent
from .agents.cvd_divergence import CvdDivergenceAgent
from .agents.trend_score import TrendScoreAgent
from .agents.llm_analyst import LLMAnalystAgent

# Construct agent objects with intervals pulled from env (fallbacks sane)
AGENTS: List = [
    RvolSpikeAgent(interval_sec=int(os.getenv("RVOL_INTERVAL", "30"))),
    CvdDivergenceAgent(interval_sec=int(os.getenv("CVD_INTERVAL", "30"))),
    TrendScoreAgent(interval_sec=int(os.getenv("TS_INTERVAL", "60"))),
    LLMAnalystAgent(interval_sec=int(os.getenv("LLM_INTERVAL", "90"))),
]

# Internal state
_last_run: Dict[str, float] = {}            # f"{agent}:{symbol}" -> epoch seconds
_status:   Dict[str, Dict[str, str]] = {}   # agent -> {status,last_run}

def list_agents_last_run() -> Dict[str, Dict[str, str]]:
    """Used by /health for quick UI diagnostics."""
    return _status.copy()

async def _run_agent_once(agent, symbol: str) -> bool:
    key = f"{agent.name}:{symbol}"
    try:
        finding = await agent.run_once(symbol)

        # update status
        _status.setdefault(agent.name, {})["status"]   = "ok"
        _status[agent.name]["last_run"] = datetime.now(timezone.utc).isoformat()

        if finding:
            row = {
                "agent":   agent.name,
                "symbol":  symbol,
                "score":   float(finding.get("score", 0.0)),
                "label":   str(finding.get("label", agent.name)),
                "details": finding.get("details") or {},
            }
            try:
                await insert_finding_row(row)
            except Exception:
                # if DB hiccups, keep it visible for the dashboard
                RECENT_FINDINGS.append({**row, "ts_utc": None})
        return True

    except Exception:
        _status.setdefault(agent.name, {})["status"]   = "error"
        _status[agent.name]["last_run"] = datetime.now(timezone.utc).isoformat()
        return False

    finally:
        _last_run[key] = time.time()
        try:
            await heartbeat(agent.name, _status.get(agent.name, {}).get("status", "ok"))
        except Exception:
            pass

async def agents_loop():
    """Cooperative scheduler. Ticks ~1s; runs agents at their configured cadences per symbol."""
    TICK_SEC = 1.0
    while True:
        started = time.time()
        for agent in AGENTS:
            for symbol in SYMBOLS:
                key = f"{agent.name}:{symbol}"
                last = _last_run.get(key, 0.0)
                if time.time() - last >= getattr(agent, "interval_sec", 60):
                    await _run_agent_once(agent, symbol)
        # tiny, bounded idle between scans
        elapsed = time.time() - started
        await asyncio.sleep(max(TICK_SEC - elapsed, 0.0))
