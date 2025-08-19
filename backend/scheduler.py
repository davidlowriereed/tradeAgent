from __future__ import annotations
import asyncio
from typing import Dict, List
from datetime import datetime, timezone

from .config import SYMBOLS
from .db import insert_finding_row, heartbeat
from .state import RECENT_FINDINGS

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

async def _run_agent_once(agent, symbol: str, insert: bool = True) -> bool:
    """
    Run a single agent once on the given symbol.
    Persist finding into DB and in-memory buffer.
    """
    try:
        finding = await agent.run_once(symbol)
    except Exception as e:
        print(f"Agent {agent.name} failed on {symbol}: {e}")
        return False

    finding = await agent.run_once(symbol)
        if finding:
            row = {
                "agent": agent.name,
                "symbol": symbol,
                "score": float(finding.get("score") or 0.0),
                "label": str(finding.get("label") or agent.name),
                "details": finding.get("details") or {},
            }
            if insert:  # run-now flag or scheduled inserts
                await insert_finding_row(row)
            RECENT_FINDINGS.append({**row, "ts_utc": None})

    return True

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
