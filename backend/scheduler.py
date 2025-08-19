# backend/scheduler.py
from __future__ import annotations
import asyncio, time
from typing import Dict, List, Any
from .config import SYMBOLS
from .db import insert_finding_row
from .state import RECENT_FINDINGS

# Agents
from .agents.rvol_spike import RVolSpikeAgent
from .agents.cvd_divergence import CVDDivergenceAgent
from .agents.trend_score import TrendScoreAgent
from .agents.llm_analyst import LLMAnalystAgent

AGENTS = [
    RVolSpikeAgent(interval_sec=10),
    CVDDivergenceAgent(interval_sec=10),
    TrendScoreAgent(interval_sec=15),
    LLMAnalystAgent(interval_sec=30),
]

_last_run: Dict[str, Dict[str, Any]] = {}

def list_agents_last_run() -> Dict[str, Dict[str, Any]]:
    out = {}
    for a in AGENTS:
        out[a.name] = {"status":"ok", "last_run": _last_run.get(a.name, {}).get("last_run")}
    return out
    
async def agents_loop():
    while True:
        t0 = time.time()
        for sym in SYMBOLS:
            for agent in AGENTS:
                try:
                    finding = await agent.run_once(sym)
                except Exception:
                    finding = None
                _last_run[agent.name] = {"last_run": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}
                if not finding:
                    continue
                payload = {
                    "agent": agent.name,
                    "symbol": sym,
                    "score": float(finding.get("score") or 0.0),
                    "label": str(finding.get("label") or agent.name),
                    "details": finding.get("details") or {},
                }
                # Try DB, fall back to in-memory buffer
                try:
                    await insert_finding_row(payload)
                except Exception:
                    RECENT_FINDINGS.append(payload)
                    if len(RECENT_FINDINGS) > 500:
                        del RECENT_FINDINGS[:len(RECENT_FINDINGS)-500]
        # pacing
        await asyncio.sleep(max(1.0, 5.0 - (time.time()-t0)))
