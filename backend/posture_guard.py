
import time
from typing import Optional
from ..signals import compute_signals, _dcvd, _mom_bps
from ..state import trades, POSTURE_STATE
from ..config import POSTURE_GUARD_INTERVAL
from .base import Agent

class PostureGuardAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("posture_guard", interval_sec or POSTURE_GUARD_INTERVAL)
        self._last_score = {}
        self._persist = {}

    async def run_once(self, symbol: str) -> Optional[dict]:
        st = POSTURE_STATE.get(symbol)
        if not st or st.get("state") != "long_bias":
            self._persist.pop(symbol, None)
            return None

        now = time.time()
        buf = trades[symbol]
        w2  = [r for r in buf if r[0] >= now - 120]
        w5  = [r for r in buf if r[0] >= now - 300]
        w15 = [r for r in buf if r[0] >= now - 900]

        d2, d5 = _dcvd(w2), _dcvd(w5)
        up = sum((w[2] or 0.0) for w in w15 if (w[3] or "")=="buy")
        dn = sum((w[2] or 0.0) for w in w15 if (w[3] or "")=="sell")
        rvratio = (dn+1e-9)/(up+1e-9)
        mom5_bps = _mom_bps(w5)

        sig = compute_signals(symbol)
        px = sig.get("best_bid") or sig.get("best_ask")
        base_low = st.get("base_low", px)

        # thresholds (can be env-driven if needed)
        CVD5, CVD2 = 1500, 800
        RVR, PUSHMIN = 1.2, 0.7
        MOM5 = -8
        MAX_AGE = 60*30  # 30 min

        if px and px >= st.get("peak_price", px):
            st["peak_price"], st["peak_ts"] = px, now

        flow_flip = (d5 <= -CVD5) or (d2 <= -CVD2)
        rvol_flip = (rvratio >= RVR)
        structure_break = ((px is not None and base_low is not None and px < base_low) or (mom5_bps <= MOM5))
        time_in_state   = now - st.get("started_at", now)
        time_since_peak = now - st.get("peak_ts", st.get("started_at", now))
        aging = (time_in_state > MAX_AGE and time_since_peak > 300)

        pc = self._persist.setdefault(symbol, {"flow":0,"rvol":0,"structure":0})
        pc["flow"] = pc["flow"] + 1 if flow_flip else 0
        pc["rvol"] = pc["rvol"] + 1 if rvol_flip else 0
        pc["structure"] = pc["structure"] + 1 if structure_break else 0

        score = int(flow_flip) + int(rvol_flip) + int(structure_break) + int(aging)
        prev = self._last_score.get(symbol, 0)
        self._last_score[symbol] = score
        if score >= 2 and prev >= 2:
            POSTURE_STATE.pop(symbol, None)
            return {"score": 8.0, "label":"posture_exit",
                    "details":{"action":"consider-exit","confidence":0.8,
                              "metrics":{"d2":d2,"d5":d5,"rvratio":rvratio,"mom5_bps":mom5_bps}}}
        if pc["structure"] >= 5 or pc["flow"] >= 5:
            POSTURE_STATE.pop(symbol, None)
            return {"score": 7.5, "label":"posture_exit",
                    "details":{"action":"consider-exit","confidence":0.75}}
        return None
