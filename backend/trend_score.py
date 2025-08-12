
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

    async def run_once(self, symbol: str) -> Optional[dict]:
        now = time.time()
        buf = trades[symbol]
        w1  = [r for r in buf if r[0] >= now - 60]
        w5  = [r for r in buf if r[0] >= now - 300]
        w15 = [r for r in buf if r[0] >= now - 900]

        d1, d5, d15 = _dcvd(w1), _dcvd(w5), _dcvd(w15)
        rvol = _rvol(w5, w15) or 0.0
        up = sum((r[2] or 0.0) for r in w15 if (r[3] or "")=="buy")
        dn = sum((r[2] or 0.0) for r in w15 if (r[3] or "")=="sell")
        rvr = (dn+1e-9)/(up+1e-9)
        mom1, mom5 = _mom_bps(w1), _mom_bps(w5)

        # price vs vwap(20m)
        vwin = [r for r in buf if r[0] >= now - 20*60]
        vwap = None
        if vwin:
            num = sum((r[1] or 0.0)*(r[2] or 0.0) for r in vwin); den = sum((r[2] or 0.0) for r in vwin)
            vwap = (num/den) if den else None
        bid, ask = best_px.get(symbol, (None,None))
        px = ask or bid
        px_vwap_bps = ((px - vwap)/vwap * 1e4) if (vwap and px) else 0.0

        p1  = self._hprob(d1, rvol, rvr, px_vwap_bps, mom1)
        p5  = self._hprob(d5, rvol, rvr, px_vwap_bps, mom5)
        p15 = self._hprob(d15, rvol, rvr, px_vwap_bps, mom5*0.6)
        wm = self.w_mtf
        p_ens = max(0.0, min(1.0, wm["w_1m"]*p1 + wm["w_5m"]*p5 + wm["w_15m"]*p15))

        return {"score": round(p_ens*10.0,2), "label":"trend_score",
                "details":{"p_up": round(p_ens,4),"p_1m": round(p1,4),"p_5m": round(p5,4),"p_15m": round(p15,4)}}
