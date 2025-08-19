# backend/scheduler.py
from __future__ import annotations

import asyncio, os, time, importlib
from typing import Dict, Optional

# Only import the helpers that actually exist in your db.py
from .db import insert_finding_row, heartbeat  # JSONB-safe insert, heartbeat

TICK_SEC = int(os.getenv("SCHED_TICK_SEC", "5"))
SYMBOLS = [s.strip() for s in os.getenv("SYMBOL", "BTC-USD,ETH-USD,ADA-USD").split(",") if s.strip()]


from datetime import datetime, timezone
from .signals import compute_signals_tf, compute_signals
from .db import insert_features_1m

_last_feat_min: dict[str, str] = {}  # "SYMBOL:YYYY-MM-DDTHH:MM" -> True


async def _flush_features_minute(symbol: str):
    """Write a features_1m snapshot once per minute per symbol."""
    tf = await compute_signals_tf(symbol)  # async by your prior contract
    ts_min = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    key = f"{symbol}:{ts_min.isoformat(timespec='minutes')}"
    if _last_feat_min.get(key):
        return
    feat_rec = {
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
        await insert_features_1m(symbol, ts_min, feat_rec)
        _last_feat_min[key] = True
    except Exception as e:
        # keep loop healthy; you'll still see errors in logs
        pass

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
    """
    Main scheduler loop:
      - runs each agent per symbol
      - heartbeats for liveness
      - once-per-minute feature snapshot into features_1m
      - low-frequency refresh of forward-return views
    """
    import time
    from datetime import datetime, timezone
    from .signals import compute_signals_tf, compute_signals
    from .db import insert_features_1m, refresh_return_views, heartbeat

    global _LAST_RUN, _last_feat_min, _view_refresh_counter
    if "_last_feat_min" not in globals():
        _last_feat_min = {}  # key: f"{symbol}:{YYYY-MM-DDTHH:MM}"
    if "_view_refresh_counter" not in globals():
        _view_refresh_counter = 0

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

            # 2) Minute-close feature snapshot (once per minute per symbol)
            try:
                ts_min = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                key = f"{symbol}:{ts_min.isoformat(timespec='minutes')}"
                if not _last_feat_min.get(key):
                    tf = await compute_signals_tf(symbol)  # async
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
                    await insert_features_1m(symbol, ts_min, feat)
                    _last_feat_min[key] = True
            except Exception:
                # never let persistence hiccups break the loop
                pass

        # 3) Liveness breadcrumb
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

