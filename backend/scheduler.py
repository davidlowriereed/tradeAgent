# backend/scheduler.py

from __future__ import annotations
from .agents import REGISTRY
import asyncio, os, time, importlib
from .agents import REGISTRY
from typing import Dict, Optional
from datetime import datetime, timezone

TICK_SEC = int(os.getenv("SCHED_TICK_SEC", "5"))
SYMBOLS = [s.strip() for s in os.getenv("SYMBOL", "BTC-USD,ETH-USD,ADA-USD").split(",") if s.strip()]

AGENT_NAMES = os.getenv("AGENTS", "momentum,rvol").split(",")
AGENTS = [REGISTRY[name.strip()] for name in AGENT_NAMES if name.strip() in REGISTRY]

# Import only functions that are guaranteed to exist
from .db import insert_finding_row, heartbeat, refresh_return_views, insert_features_1m
from .signals import compute_signals_tf

# -----------------------------------------------------------------------------
# Agent discovery — tolerate class name drift across modules
# -----------------------------------------------------------------------------
def resolve_agents(spec):
    """
    spec can be:
      - list of short-name strings (e.g. ["trend_score", "opening_drive"])
      - list of classes
      - list of already-instantiated agents
    """
    out = []
    for item in spec:
        if isinstance(item, str):
            out.append(REGISTRY[item]())
        elif isinstance(item, type):
            out.append(item())              # it’s a class
        else:
            out.append(item)                # assume instance
    return out

def _load_agent(module: str, *class_names: str):
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
_LAST_RUN: Dict[str, float] = {}   # agent_name -> epoch seconds of last success
_last_feat_min: dict[str, bool] = {}  # f"{symbol}:{YYYY-MM-DDTHH:MM}" -> True
_view_refresh_counter: int = 0

def list_agents_last_run() -> Dict[str, Optional[str]]:
    res = {}
    for name, ts in _LAST_RUN.items():
        try:
            res[name] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z"
        except Exception:
            res[name] = None
    return res

# -----------------------------------------------------------------------------
# Uniform agent execution
# -----------------------------------------------------------------------------
async def run_agent_once(agent, symbol: str, insert: bool = True) -> Optional[dict]:
    finding = await agent.run_once(symbol)
    if finding and insert:
        rec = {
            "agent": getattr(agent, "name", "unknown"),
            "symbol": symbol,
            "score": float(finding.get("score", 0.0)),
            "label": str(finding.get("label", "")),
            "details": finding.get("details", {}),
        }
        await insert_finding_row(rec)
    return finding

# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------
async def _maybe_write_features(symbol: str):
    """Write a features_1m snapshot once per minute per symbol (conflict-safe upsert in db.py)."""
    ts_min = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    key = f"{symbol}:{ts_min.isoformat(timespec='minutes')}"
    if _last_feat_min.get(key):
        return
    tf = await compute_signals_tf(symbol)  # should be async and never raise on empty
    feat = {
        "mom_bps_1m":         tf.get("mom_bps_1m"),
        "mom_bps_5m":         tf.get("mom_bps_5m"),
        "mom_bps_15m":        tf.get("mom_bps_15m"),
        "px_vs_vwap_bps_1m":  tf.get("px_vs_vwap_bps_1m"),
        "px_vs_vwap_bps_5m":  tf.get("px_vs_vwap_bps_5m"),
        "px_vs_vwap_bps_15m": tf.get("px_vs_vwap_bps_15m"),
        "rvol_1m":            tf.get("rvol_1m"),
        "atr_1m":             tf.get("atr_1m"),
        "schema_version":     1,
    }
    try:
        await insert_features_1m(symbol, ts_min, feat)
        _last_feat_min[key] = True
    except Exception:
        # keep loop resilient
        pass

# -----------------------------------------------------------------------------
# Main background loop (moved into a private runner)
# -----------------------------------------------------------------------------
async def _agents_forever():
    global _view_refresh_counter
    while True:
        loop_start = time.time()

        for symbol in SYMBOLS:
            # 1) Run agents
            for name, agent in AGENTS.items():
                try:
                    await run_agent_once(agent, symbol, insert=True)
                    _LAST_RUN[name] = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # keep the loop healthy even if one agent fails
                    pass

            # 2) Once-per-minute feature snapshot
            try:
                await _maybe_write_features(symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        # 3) Heartbeat
        try:
            await heartbeat("scheduler")
        except Exception:
            pass

        # 4) Low-frequency refresh of return views (~once/min if TICK_SEC≈5)
        try:
            _view_refresh_counter = (_view_refresh_counter + 1) % 12
            if _view_refresh_counter == 0:
                await refresh_return_views()
        except Exception:
            pass

        # 5) Pace the loop
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0.0, TICK_SEC - elapsed))

# -----------------------------------------------------------------------------
# Supervising wrapper — restarts background loop with backoff
# -----------------------------------------------------------------------------
async def agents_loop():
    backoff = 1.0
    while True:
        try:
            await _agents_forever()
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        else:
            backoff = 1.0
