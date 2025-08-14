
import math, time
from typing import Optional
from ..signals import _dcvd, _rvol, _mom_bps, compute_signals
from ..state import trades, best_px
from ..config import TS_INTERVAL, TS_WEIGHTS, TS_MTF_WEIGHTS
from .base import Agent

def _tanh(x, s): 
    try: return math.tanh(x/(s if s!=0 else 1e-9))
    except Exception: return 0.0

def _sigmoid(z):
    try: return 1.0/(1.0+math.exp(-z))
    except OverflowError: return 0.0 if z<0 else 1.0

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

class TrendScoreAgent(Agent):
    
    name = "trend_score"
    
    def __init__(self, interval_sec: int | None = None):
        super().__init__("trend_score", interval_sec or TS_INTERVAL)
        self.w_base = self._init_base()
        self.w_mtf = self._init_mtf()

    def _init_base(self):
        if TS_WEIGHTS and TS_WEIGHTS.strip().lower() != "default":
            import json
            try: return json.loads(TS_WEIGHTS)
            except Exception: pass
        return {"bias":0.0,"w_dcvd":0.8,"w_rvol":0.5,"w_rvratio":0.7,"w_vwap":0.6,"w_mom":0.5}

    def _init_mtf(self):
        if TS_MTF_WEIGHTS and TS_MTF_WEIGHTS.strip().lower() != "default":
            import json
            try: return json.loads(TS_MTF_WEIGHTS)
            except Exception: pass
        return {"w_1m":0.25,"w_5m":0.45,"w_15m":0.30}

    def _hprob(self, dcvd, rvol, rvratio, px_vs_vwap_bps, mom_bps):
        w = self.w_base
        z = w["bias"] + w["w_dcvd"]*_tanh(dcvd,1500) + w["w_rvol"]*_tanh((rvol-1.0),0.5) - w["w_rvratio"]*_tanh((rvratio-1.0),0.3) + w["w_vwap"]*_tanh(px_vs_vwap_bps,12.0) + w["w_mom"]*_tanh(mom_bps,10.0)
        return _sigmoid(z)

    async def run_once(self, symbol: str):
        s = compute_signals(symbol)   # <-- call it; returns dict

        # Very lightweight probabilities from simple features
        p1 = _clamp01(0.5 + (s["mom1_bps"] / 800.0))                 # ±400 bps ⇒ ~±0.5
        p5 = _clamp01(0.5 + 0.12 * (1 if s["dcvd_2m"] > 0 else -1) + 0.08*((s["rvol_vs_recent"]-1.0)))
        p15 = _clamp01(0.5*0.2 + 0.8*p5)

        p_up = _clamp01((p1 + p5 + p15) / 3.0)
        details = {"p_up": p_up, "p_1m": p1, "p_5m": p5, "p_15m": p15}
        return {"score": round(10*p_up, 2), "label": "trend_score", "details": details}
