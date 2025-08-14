
import math, time
from typing import Optional
from ..signals import _dcvd, _rvol, _mom_bps
from ..state import trades, best_px
from ..config import TS_INTERVAL, TS_WEIGHTS, TS_MTF_WEIGHTS
from .base import Agent

def _tanh(x, s): 
    try: return math.tanh(x/(s if s!=0 else 1e-9))
    except Exception: return 0.0

def _sigmoid(z):
    try: return 1.0/(1.0+math.exp(-z))
    except OverflowError: return 0.0 if z<0 else 1.0

class TrendScoreAgent(Agent):
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

def run_once(self, symbol: str) -> Optional[dict]:
    # --- bar-based path ---
    if FEATURE_BARS:
        try:
            tf = compute_signals_tf(symbol, ["1m","5m","15m"]) or {}
            mom1 = float(tf.get("mom_bps_1m", 0.0) or 0.0)
            mom5 = float(tf.get("mom_bps_5m", 0.0) or 0.0)
            mom15 = float(tf.get("mom_bps_15m", 0.0) or 0.0)
            pxv = float(tf.get("px_vs_vwap_bps_1m", 0.0) or 0.0)
            rvol = float(tf.get("rvol_5m", 0.0) or 0.0)

            p1  = self._hprob(mom1, rvol, rvol, pxv, mom1)
            p5  = self._hprob(mom5, rvol, rvol, pxv, mom5)
            p15 = self._hprob(mom15, rvol, rvol, pxv, mom5 * 0.6)

            wm = self.w_mtf
            p_up = max(0.0, min(1.0, wm["w_1m"]*p1 + wm["w_5m"]*p5 + wm["w_15m"]*p15))
            return {"score": round(p_up*10,2), "label":"trend_score",
                    "details":{"p_up":round(p_up,4),"p_1m":round(p1,4),"p_5m":round(p5,4),"p_15m":round(p15,4)}}
        except Exception:
            pass  # fall back to legacy path

    # --- legacy path (unchanged, but cast floats) ---
    sig = compute_signals(symbol) or {}
    mom1 = float(sig.get("mom1_bps", 0.0) or 0.0)
    mom5 = float(sig.get("mom5_bps", 0.0) or 0.0)
    pxv  = float(sig.get("px_vs_vwap_bps", 0.0) or 0.0)
    rvol = float(sig.get("rvol_vs_recent", 0.0) or 0.0)

    p1  = self._hprob(mom1, rvol, rvol, pxv, mom1)
    p5  = self._hprob(mom5, rvol, rvol, pxv, mom5)
    p15 = self._hprob(mom5*0.6, rvol, rvol, pxv, mom1*0.4)

    wm = self.w_mtf
    p_up = max(0.0, min(1.0, wm["w_1m"]*p1 + wm["w_5m"]*p5 + wm["w_15m"]*p15))
    return {"score": round(p_up*10,2), "label":"trend_score",
            "details":{"p_up":round(p_up,4),"p_1m":round(p1,4),"p_5m":round(p5,4),"p_15m":round(p15,4)}}
