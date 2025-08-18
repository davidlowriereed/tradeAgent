import time, asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Tuple, List, Optional

from .config import (
    SYMBOLS, SLACK_ANALYSIS_ONLY, ALERT_WEBHOOK_URL, ALERT_VERBOSE, FEATURE_REVERSAL,
    TS_ENTRY, TS_EXIT, TS_PERSIST, LLM_ANALYST_MIN_SCORE
)
from .signals import compute_signals, compute_signals_tf
from .state import POSTURE_STATE, trades, RECENT_FINDINGS
from .services.slack import should_post, post_webhook
from .db import insert_finding_row, heartbeat, insert_bar_1m, insert_features_1m, refresh_return_views

from .agents.base import Agent
from .agents.rvol_spike import RVOLSpikeAgent
from .agents.cvd_divergence import CvdDivergenceAgent
from .agents.trend_score import TrendScoreAgent
from .agents.llm_analyst import LLMAnalystAgent
from .agents.session_reversal import SessionReversalAgent
from .agents.opening_drive import OpeningDriveReversalAgent


_last_flushed_minute = {}
_view_refresh_counter = 0

async def flush_minute(symbol: str):
    """
    Persist the just-closed 1m bar + feature vector for symbol.
    Safe to call each tick; only writes once per minute boundary.
    """
    # Use /debug/state equivalent from your in-memory bars if available
    from .bars import get_last_1m_bar  # implement this if not present
    bar = await get_last_1m_bar(symbol)  # returns {ts_utc,o,h,l,c,v,vwap,trades}
    if not bar or not bar.get("ts_utc"):
        return
    ts = bar["ts_utc"][:16]  # minute precision
    key = f"{symbol}:{ts}"
    if _last_flushed_minute.get(key):
        return
    # features from compute_signals_tf (already normalized shape)
    tf = await compute_signals_tf(symbol)
    fx = {
        "mom_bps_1m": tf.get("mom_bps_1m"), "mom_bps_5m": tf.get("mom_bps_5m"), "mom_bps_15m": tf.get("mom_bps_15m"),
        "px_vs_vwap_bps_1m": tf.get("px_vs_vwap_bps_1m"), "px_vs_vwap_bps_5m": tf.get("px_vs_vwap_bps_5m"),
        "px_vs_vwap_bps_15m": tf.get("px_vs_vwap_bps_15m"),
        "rvol_1m": tf.get("rvol_1m"), "rvol_5m": tf.get("rvol_5m"), "rvol_15m": tf.get("rvol_15m"),
        "atr_1m": tf.get("atr_1m"), "atr_5m": tf.get("atr_5m"), "atr_15m": tf.get("atr_15m"),
    }
    await insert_bar_1m(symbol, bar["ts_utc"], bar)
    await insert_features_1m(symbol, bar["ts_utc"], fx, schema_version=1)
    _last_flushed_minute[key] = True
AGENTS: List[Agent] = [
    RVOLSpikeAgent(interval_sec=5),
    CvdDivergenceAgent(interval_sec=10),
    TrendScoreAgent(interval_sec=15),
    LLMAnalystAgent(interval_sec=60),
]
if FEATURE_REVERSAL:
    AGENTS += [SessionReversalAgent(), OpeningDriveReversalAgent()]

_last_run_ts: defaultdict = defaultdict(lambda: 0.0)

def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts or ts <= 0:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

def list_agents_last_run() -> Dict[str, Dict[str, Optional[str]]]:
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for agent in AGENTS:
        last = 0.0
        for sym in SYMBOLS:
            last = max(last, _last_run_ts.get((agent.name, sym), 0.0))
        out[agent.name] = {"status": "ok", "last_run": _iso(last)}
    return out

async def agents_loop():
    TICK_SEC = 5
    while True:
        try:
            now = time.time()

            # Maintain posture peaks (unchanged)
            for sym in SYMBOLS:
                ps = POSTURE_STATE.get(sym)
                if ps and ps.get("status") == "long_bias":
                    sig = compute_signals(sym)
                    px = sig.get("best_ask") or sig.get("best_bid")
                    if px is not None and (ps.get("peak_price") is None or px >= ps.get("peak_price")):
                        ps["peak_price"] = px
                        ps["peak_ts"] = now
                        POSTURE_STATE[sym] = ps

            # Run agents on their own intervals
            for agent in AGENTS:
                for sym in SYMBOLS:
                    key = (agent.name, sym)
                    interval = max(1, int(getattr(agent, "interval_sec", 10)))
                    if now - _last_run_ts[key] < interval:
                        continue

                    try:
                        finding = await agent.run_once(sym)
                        _last_run_ts[key] = time.time()

                        await heartbeat(agent.name, "ok")

                        if not finding:
                            continue

                        if agent.name == "llm_analyst" and float(finding.get("score", 0.0)) < float(LLM_ANALYST_MIN_SCORE):
                            continue

                        # ✅ await the async insert; pass a dict body
                        try:
                            await insert_finding_row({
                                "agent": agent.name,
                                "symbol": sym,
                                "score": float(finding.get("score", 0.0)),
                                "label": finding.get("label", agent.name),
                                "details": finding.get("details") or {},
                            })
                        except Exception:
                            # In-mem fallback
                            RECENT_FINDINGS.append({
                                "agent": agent.name,
                                "symbol": sym,
                                "score": float(finding.get("score", 0.0)),
                                "label": finding.get("label", agent.name),
                                "details": finding.get("details") or {},
                            })

                        # Preserve your trend_score posture-state logic
                        if agent.name == "trend_score":
                            p_up = float(finding.get("details", {}).get("p_up", 0.0))
                            ps = POSTURE_STATE.get(sym, {})
                            cnt = int(ps.get("_persist", 0))

                            if p_up >= float(TS_ENTRY):
                                cnt += 1
                                if cnt >= int(TS_PERSIST) and ps.get("status") != "long_bias":
                                    sig = compute_signals(sym)
                                    px = sig.get("best_ask") or sig.get("best_bid")
                                    now2 = time.time()
                                    w5 = [r for r in trades.get(sym, []) if r[0] >= now2 - 300]
                                    base_low = min((r[1] for r in w5), default=px or 0.0) or px
                                    POSTURE_STATE[sym] = {
                                        "status": "long_bias",
                                        "started_at": now2,
                                        "entry_price": px,
                                        "base_low": base_low,
                                        "peak_price": px,
                                        "peak_ts": now2,
                                        "_persist": 0,
                                    }
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps or {"_persist": cnt}
                            elif p_up <= float(TS_EXIT) and ps.get("status") == "long_bias":
                                cnt += 1
                                if cnt >= int(TS_PERSIST):
                                    POSTURE_STATE.pop(sym, None)
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps
                            else:
                                if ps:
                                    ps["_persist"] = max(0, cnt - 1)
                                    POSTURE_STATE[sym] = ps

                    except Exception as e:
                        await heartbeat(agent.name, f"error: {type(e).__name__}")
                        continue
        except Exception:
            pass

        await asyncio.sleep(TICK_SEC)
