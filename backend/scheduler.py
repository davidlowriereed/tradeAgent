# backend/scheduler.py
import time, asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Tuple, List, Optional

from .config import (
    SYMBOLS, SLACK_ANALYSIS_ONLY, ALERT_WEBHOOK_URL, ALERT_VERBOSE, FEATURE_REVERSAL,
    TS_ENTRY, TS_EXIT, TS_PERSIST, LLM_ANALYST_MIN_SCORE
)
from .signals import compute_signals
from .state import POSTURE_STATE, trades, RECENT_FINDINGS
from .db import connect_async, insert_finding, heartbeat, pg_conn
from .services.slack import should_post, post_webhook

from .agents.base import Agent
from .agents.rvol_spike import RVOLSpikeAgent
from .agents.cvd_divergence import CvdDivergenceAgent  # <- correct class name
from .agents.trend_score import TrendScoreAgent
from .agents.llm_analyst import LLMAnalystAgent
from .agents.session_reversal import SessionReversalAgent
from .agents.opening_drive import OpeningDriveReversalAgent
# Optional macro watcher (wired later if you want it enabled)
# from .agents.macro_watcher import MacroWatcherAgent

# ------------------------------------------------------------------
# Agent registry (single definition)
# ------------------------------------------------------------------
AGENTS: List[Agent] = [
    RVOLSpikeAgent(interval_sec=5),
    CvdDivergenceAgent(interval_sec=10),
    TrendScoreAgent(interval_sec=15),
    LLMAnalystAgent(interval_sec=60),
]
if FEATURE_REVERSAL:
    AGENTS += [SessionReversalAgent(), OpeningDriveReversalAgent()]
# If you want macro watcher running as an agent, add it here:
# AGENTS.append(MacroWatcherAgent(interval_sec=60))

# Last-run timestamps keyed by (agent_name, symbol)
_last_run_ts: defaultdict = defaultdict(lambda: 0.0)

def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts or ts <= 0:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

def list_agents_last_run() -> Dict[str, Dict[str, Optional[str]]]:
    """
    For /health. Summarize agent last_run across symbols (max timestamp).
    """
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for agent in AGENTS:
        last = 0.0
        for sym in SYMBOLS:
            last = max(last, _last_run_ts.get((agent.name, sym), 0.0))
        out[agent.name] = {"status": "ok", "last_run": _iso(last)}
    return out

# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------
async def agents_loop():
    while True:
        try:
            # Ensure DB connection exists (non-fatal if it fails)
            try:
                if pg_conn is None:
                    await connect_async()
            except Exception:
                pass

            now = time.time()

            # Update posture peaks for active "long_bias" status
            for sym in SYMBOLS:
                ps = POSTURE_STATE.get(sym)
                if ps and ps.get("status") == "long_bias":
                    sig = compute_signals(sym)
                    px = sig.get("best_ask") or sig.get("best_bid")
                    if px is not None and (ps.get("peak_price") is None or px >= ps.get("peak_price")):
                        ps["peak_price"] = px
                        ps["peak_ts"] = now
                        POSTURE_STATE[sym] = ps

            # Run agents opportunistically per their interval
            for agent in AGENTS:
                for sym in SYMBOLS:
                    key = (agent.name, sym)
                    interval = max(1, int(getattr(agent, "interval_sec", 10)))
                    if now - _last_run_ts[key] < interval:
                        continue

                    try:
                        finding = await agent.run_once(sym)
                        _last_run_ts[key] = time.time()
                        heartbeat(agent.name, "ok")

                        if not finding:
                            continue

                        # Gate LLM analyst by min score
                        if agent.name == "llm_analyst" and float(finding.get("score", 0.0)) < float(LLM_ANALYST_MIN_SCORE):
                            continue

                        # Insert to DB; if it fails, fall back to in-memory buffer
                        try:
                            insert_finding(
                                agent.name,
                                sym,
                                float(finding["score"]),
                                finding["label"],
                                finding.get("details") or {},
                            )
                        except Exception:
                            RECENT_FINDINGS.append({
                                "ts_utc": finding.get("ts_utc"),
                                "agent": agent.name,
                                "symbol": sym,
                                "score": float(finding.get("score", 0.0)),
                                "label": finding.get("label", agent.name),
                                "details": finding.get("details") or {},
                            })

                        # TrendScore drives posture transitions (use 'status' consistently)
                        if agent.name == "trend_score":
                            p_up = float(finding.get("details", {}).get("p_up", 0.0))
                            ps = POSTURE_STATE.get(sym, {})
                            cnt = int(ps.get("_persist", 0))

                            if p_up >= float(TS_ENTRY):
                                if ps.get("status") == "long_bias":
                                    cnt = max(1, cnt + 1)  # keep it at least 1 while in-trend
                                else:
                                    cnt = cnt + 1
                                if cnt >= int(TS_PERSIST) and ps.get("status") != "long_bias":
                                    sig = compute_signals(sym)
                                    px = sig.get("best_ask") or sig.get("best_bid")
                                    now2 = time.time()
                                    # recent 5m window
                                    w5 = [r for r in trades.get(sym, []) if r[0] >= now2 - 300]
                                    default_low = px if px is not None else 0.0
                                    base_low = min((r[1] for r in w5), default=default_low)
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
                                    if "status" not in ps:
                                        ps["status"] = "flat"
                                    POSTURE_STATE[sym] = ps

                            elif p_up <= float(TS_EXIT) and ps.get("status") == "long_bias":
                                cnt = cnt + 1
                                if cnt >= int(TS_PERSIST):
                                    POSTURE_STATE.pop(sym, None)
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps
                            else:
                                if ps:
                                    ps["_persist"] = max(0, cnt - 1)
                                    POSTURE_STATE[sym] = ps

                        # Optional Slack alerting (verbose mode + not analysis-only)
                        if ALERT_WEBHOOK_URL and not SLACK_ANALYSIS_ONLY and ALERT_VERBOSE:
                            if should_post(agent.name, sym):
                                try:
                                    await post_webhook({
                                        "text": f"{agent.name} {sym} → {finding['label']} {float(finding['score']):.1f}"
                                    })
                                except Exception:
                                    pass

                    except Exception:
                        heartbeat(agent.name, "error")
                        # keep loop resilient to individual agent errors
                        continue

        except Exception:
            # Never kill the loop due to unexpected exceptions
            pass

        await asyncio.sleep(5)
