# backend/scheduler.py
from __future__ import annotations

import asyncio, os, time, importlib
from typing import Dict, Optional
from datetime import datetime, timezone

# config
TICK_SEC = int(os.getenv("SCHED_TICK_SEC", "5"))
SYMBOLS = [s.strip() for s in os.getenv("SYMBOL", "BTC-USD,ETH-USD,ADA-USD").split(",") if s.strip()]

# app imports
from .db import insert_finding_row, heartbeat, insert_bar_1m, insert_features_1m, refresh_return_views
from .signals import compute_signals_tf
from .state import trades  # expected shape: Mapping[symbol] -> List[[ts_epoch, price, size], ...]

# -----------------------------------------------------------------------------
# Minute flush for bars + features (single source of truth)
# -----------------------------------------------------------------------------
_last_flushed_min: dict[str, bool] = {}  # key: f"{symbol}:{epoch_minute_start}"

def _minute_open(ts_epoch: float) -> int:
    return int(ts_epoch // 60) * 60

async def flush_minute(symbol: str):
    """
    At the close of each minute, compute OHLCV from in-memory trades and persist:
      - bars_1m (o,h,l,c,v,vwap,trades)
      - features_1m (selected metrics from compute_signals_tf)
    """
    now = time.time()
    m_open = _minute_open(now - 1)  # just-closed minute
    key = f"{symbol}:{m_open}"
    if _last_flushed_min.get(key):
        return

    # Gather trades for that minute
    window = [t for t in trades.get(symbol, []) if _minute_open(t[0]) == m_open]
    if not window:
        _last_flushed_min[key] = True  # avoid re-checking endlessly
        return

    px = [float(t[1]) for t in window]
    sz = [abs(float(t[2])) if len(t) > 2 and t[2] is not None else 0.0 for t in window]
    o, h, l, c = px[0], max(px), min(px), px[-1]
    v = sum(sz)
    vwap = (sum(p*s for p, s in zip(px, sz)) / v) if v else None
    ntr = len(window)

    ts_utc = datetime.fromtimestamp(m_open, tz=timezone.utc).isoformat()

    # 1) Write bar
    await insert_bar_1m(symbol, ts_utc, {"o": o, "h": h, "l": l, "c": c, "v": v, "vwap": vwap, "trades": ntr})

    # 2) Compute feature snapshot and write (minimal schema)
    tf = await compute_signals_tf(symbol)
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
    await insert_features_1m(symbol, ts_utc, feat)

    _last_flushed_min[key] = True

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
      - minute-close bar/feature flush
      - runs each agent per symbol
      - heartbeats for liveness
      - low-frequency refresh of forward-return views
    """
    global _LAST_RUN
    _view_refresh_counter = 0

    while True:
        loop_start = time.time()

        for symbol in SYMBOLS:
            # 1) Per-minute persistence from live trades
            try:
                await flush_minute(symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                # keep the loop healthy
                pass

            # 2) Run agents
            for name, agent in AGENTS.items():
                try:
                    await run_agent_once(agent, symbol, insert=True)
                    _LAST_RUN[name] = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # keep the loop healthy even if one agent fails
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
