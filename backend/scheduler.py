
import time, asyncio
from collections import defaultdict
from typing import List, Tuple

from .config import (SYMBOLS, SLACK_ANALYSIS_ONLY, ALERT_WEBHOOK_URL, ALERT_VERBOSE,
                     TS_ENTRY, TS_EXIT, TS_PERSIST, LLM_ANALYST_MIN_SCORE)
from .signals import compute_signals
from .state import POSTURE_STATE, trades
from .db import connect_async, insert_finding, heartbeat, pg_conn
from .services.slack import should_post, post_webhook
from .agents.base import Agent
from .agents.rvol_spike import RVOLSpikeAgent
from .agents.cvd_divergence import CvdDivergenceAgent
from .agents.trend_score import TrendScoreAgent
from .agents.llm_analyst import LLMAnalystAgent
from .agents.posture_guard import PostureGuardAgent
from backend.agents.macro_watcher import MacroWatcherAgent

AGENTS = [
    RVOLSpikeAgent(),
    CvdDivergenceAgent(),
    TrendScoreAgent(),
    LLMAnalystAgent(),
    PostureGuardAgent(),
    MacroWatcherAgent(),
]

_last_run_ts: dict[tuple, float] = defaultdict(lambda: 0.0)

async def agents_loop():
    while True:
        try:
            if pg_conn is None:
                await connect_async()

            now = time.time()
            # update posture peaks
            for sym in SYMBOLS:
                st = POSTURE_STATE.get(sym)
                if st and st.get("state") == "long_bias":
                    sig = compute_signals(sym)
                    px = sig.get("best_ask") or sig.get("best_bid")
                    if px and px >= st.get("peak_price", px):
                        st["peak_price"] = px
                        st["peak_ts"] = now

            for agent in AGENTS:
                for sym in SYMBOLS:
                    key = (agent.name, sym)
                    if now - _last_run_ts[key] < max(1, int(getattr(agent, "interval_sec", 10))):
                        continue

                    try:
                        finding = await agent.run_once(sym)
                        _last_run_ts[key] = time.time()
                        heartbeat(agent.name, "ok")
                        if not finding:
                            continue

                        if agent.name == "llm_analyst" and float(finding.get("score",0.0)) < LLM_ANALYST_MIN_SCORE:
                            continue
                        insert_finding(agent.name, sym, float(finding["score"]), finding["label"], finding.get("details") or {})

                        # TrendScore drives posture
                        if agent.name == "trend_score":
                            p_up = float(finding["details"].get("p_up", 0.0))
                            ps = POSTURE_STATE.get(sym, {})
                            cnt = ps.get("_persist", 0)
                            if p_up >= TS_ENTRY:
                                cnt = max(1, cnt+1) if ps.get("state")=="long_bias" else (cnt+1)
                                if cnt >= TS_PERSIST and ps.get("state") != "long_bias":
                                    sig = compute_signals(sym)
                                    px = sig.get("best_ask") or sig.get("best_bid")
                                    now2 = time.time()
                                    w5 = [r for r in trades[sym] if r[0] >= now2 - 300]
                                    base_low = min((r[1] for r in w5), default=px or 0.0) or px
                                    POSTURE_STATE[sym] = {
                                        "state":"long_bias",
                                        "started_at": now2,
                                        "entry_price": px,
                                        "base_low": base_low,
                                        "peak_price": px,
                                        "peak_ts": now2,
                                        "_persist": 0,
                                    }
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps or {"_persist":cnt}
                            elif p_up <= TS_EXIT and ps.get("state") == "long_bias":
                                cnt = ps.get("_persist",0) + 1
                                if cnt >= TS_PERSIST:
                                    POSTURE_STATE.pop(sym, None)
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps
                            else:
                                if ps:
                                    ps["_persist"] = max(0, ps.get("_persist",0) - 1)
                                    POSTURE_STATE[sym] = ps

                        if ALERT_WEBHOOK_URL and not SLACK_ANALYSIS_ONLY and ALERT_VERBOSE:
                            if should_post(agent.name, sym):
                                await post_webhook({"text": f"{agent.name} {sym} → {finding['label']} {finding['score']:.1f}"})
                    except Exception:
                        heartbeat(agent.name, "error")
                        continue
        except Exception:
            pass

        await asyncio.sleep(5)
