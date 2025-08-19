# backend/scheduler.py
from __future__ import annotations

import asyncio, os, time, importlib
from typing import Dict, Optional

# Only import the helpers that actually exist in your db.py
from .db import insert_finding_row, heartbeat  # JSONB-safe insert, heartbeat

TICK_SEC = int(os.getenv("SCHED_TICK_SEC", "5"))
SYMBOLS = [s.strip() for s in os.getenv("SYMBOL", "BTC-USD,ETH-USD,ADA-USD").split(",") if s.strip()]

# -----------------------------------------------------------------------------
# Resilient agent loader — tolerates class name changes
# -----------------------------------------------------------------------------
def _load_agent(module: str, *class_names: str):
    """
    Try importing an agent class from backend.agents.<module>.
    Accept multiple possible class names (handles repo drift).
    Returns an instance or None.
    """
    try:
        mod = importlib.import_module(f"{__package__}.agents.{module}")
    except Exception:
        return None
    for cls in class_names:
        impl = getattr(mod, cls, None)
        if impl is not None:
            try:
                return impl()  # instantiate
            except Exception:
                return None
    return None

def build_agents() -> Dict[str, object]:
    # Try common variants per agent
    candidates = [
        ("rvol_spike",    ("RVolSpikeAgent", "RVolSpike")),
        ("cvd_divergence",("CVDDivergenceAgent","CvdDivergenceAgent","CvdDivergence")),
        ("trend_score",   ("TrendScoreAgent","TrendScore")),
        ("llm_analyst",   ("LLMAnalystAgent",)),
    ]
    out: Dict[str, object] = {}
    for modname, classes in candidates:
        inst = _load_agent(modname, *classes)
        if inst is not None:
            out[getattr(inst, "name", modname)] = inst
    return out

AGENTS: Dict[str, object] = build_agents()
_LAST_RUN: Dict[str, float] = {}  # agent_name -> epoch seconds of last success

def list_agents_last_run() -> Dict[str, Optional[str]]:
    """Return a simple map for /health to render last-run stamps."""
    res = {}
    for name, ts in _LAST_RUN.items():
        try:
            # return ISO8601-ish seconds since process start to mark “ok”
            res[name] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z"
        except Exception:
            res[name] = None
    return res

# -----------------------------------------------------------------------------
# Uniform agent execution
# -----------------------------------------------------------------------------
async def run_agent_once(agent, symbol: str, insert: bool = True) -> Optional[dict]:
    """
    Agent contract: async run_once(symbol) -> Optional[dict] with keys:
      score: float, label: str, details: dict
    If a finding is returned and insert=True, persist via insert_finding_row (JSONB-safe).
    """
    finding = await agent.run_once(symbol)
    if finding and insert:
        rec = {
            "agent": getattr(agent, "name", "unknown"),
            "symbol": symbol,
            "score": float(finding.get("score", 0.0)),
            "label": str(finding.get("label", "")),
            "details": finding.get("details", {}),  # dict → JSONB via db helper
        }
        await insert_finding_row(rec)
    return finding

# -----------------------------------------------------------------------------
# Background loop
# -----------------------------------------------------------------------------
async def agents_loop():
    global _LAST_RUN
    while True:
        loop_start = time.time()
        for symbol in SYMBOLS:
            for name, agent in AGENTS.items():
                try:
                    await run_agent_once(agent, symbol, insert=True)
                    _LAST_RUN[name] = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # keep the loop healthy even if one agent fails
                    pass
        # liveness breadcrumb
        try:
            await heartbeat("scheduler")
        except Exception:
            pass

        # pace the loop
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0.0, TICK_SEC - elapsed))
