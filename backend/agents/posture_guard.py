import time
from typing import Optional
from ..signals import compute_signals, _dcvd, _mom_bps
from ..bars import build_bars, atr, momentum_bps, rvol_ratio
from ..state import trades, POSTURE_STATE
from ..config import (
    POSTURE_GUARD_INTERVAL,
    POSTURE_ENTRY_CONF,
    PG_CVD_5M_NEG, PG_CVD_2M_NEG,
    PG_RVOL_RATIO, PG_PUSH_RVOL_MIN,
    PG_MOM5_DOWN_BPS, PG_VWAP_MINUTES,
    POSTURE_MAX_AGE_MIN, PG_PERSIST_K, PG_DD_SLOW_BPS
)
from .base import Agent

class PostureGuardAgent(Agent):
    """
    Lightweight exit/guardrail logic to avoid overstaying a long-bias posture.
    Now consults ATR, RVOL, and momentum using bar data.
    """
    def __init__(self, interval_sec: int | None = None):
        super().__init__("posture_guard", interval_sec or POSTURE_GUARD_INTERVAL)
        self._last_score = {}
        self._persist = {}

    async def run_once(self, symbol: str) -> Optional[dict]:
        st = POSTURE_STATE.get(symbol)
        if not st or st.get("state") != "long_bias":
            self._persist.pop(symbol, None)
            return None

        # Bars for structure & volatility
        bars_1m = build_bars(symbol, tf="1m", lookback_min=max(60, POSTURE_MAX_AGE_MIN+10))
        if len(bars_1m) < 30:
            return None

        # Flow & activity
        sig = compute_signals(symbol)
        d2 = sig.get("dcvd_2m") or 0.0
        rvratio = rvol_ratio(bars_1m, win=5, baseline=20)
        mom5_bps = momentum_bps(bars_1m, lookback=5)  # ~5 minutes

        # Structure
        a = atr(bars_1m, period=14)
        aging = (time.time() - (st.get("entered_at") or 0)) / 60.0 > POSTURE_MAX_AGE_MIN
        flow_flip = (d2 <= -abs(PG_CVD_2M_NEG))
        rvol_flip = (rvratio <= PG_RVOL_RATIO * PG_PUSH_RVOL_MIN)  # loss of participation
        structure_break = (mom5_bps <= PG_MOM5_DOWN_BPS)

        # Persist logic to avoid flapping
        if (flow_flip or structure_break) and rvratio > 0.0:
            cnt = self._persist.get(symbol, 0) + 1
            self._persist[symbol] = cnt
        else:
            self._persist.pop(symbol, None)

        score = int(flow_flip) + int(rvol_flip) + int(structure_break) + int(aging)
        prev = self._last_score.get(symbol, 0)
        self._last_score[symbol] = score

        if score >= 2 and prev >= 2:
            POSTURE_STATE.pop(symbol, None)
            return {
                "score": 8.0,
                "label": "posture_exit",
                "details": {
                    "action": "consider-exit",
                    "confidence": 0.8,
                    "metrics": {
                        "dcvd_2m": round(d2,1),
                        "rvratio": round(rvratio,2),
                        "mom5_bps": round(mom5_bps,1),
                        "atr_1m": round(a,5),
                        "aging_min": round((time.time() - (st.get('entered_at') or 0))/60.0,1),
                    },
                },
            }
        return None
