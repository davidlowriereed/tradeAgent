# backend/agents/trend_score.py
import math
from typing import Optional, Dict, Any

from ..signals import compute_signals, compute_signals_tf
from ..config import TS_INTERVAL, TS_WEIGHTS, TS_MTF_WEIGHTS
from .base import Agent
import inspect, sys
sys.stderr.write(f"[trend_score] loaded: {inspect.getsourcefile(sys.modules[__name__])}\n")

def _tanh(x: float, s: float) -> float:
    try:
        return math.tanh(x / (s if s != 0 else 1e-9))
    except Exception:
        return 0.0

def _sigmoid(z: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def _parse_weights(env_value: Optional[str], default: Dict[str, float]) -> Dict[str, float]:
    if not env_value or env_value.strip().lower() == "default":
        return dict(default)
    try:
        import json
        w = json.loads(env_value)
        if isinstance(w, dict):
            out = dict(default); out.update(w); return out
    except Exception:
        pass
    return dict(default)

class TrendScoreAgent(Agent):
    """Trend probability using momentum, price vs VWAP (bps), and RVOL across 1m/5m/15m."""
    name = "trend_score"

    def __init__(self, interval_sec: Optional[int] = None):
        super().__init__(self.name, interval_sec or int(TS_INTERVAL))
        self.w_base = _parse_weights(
            TS_WEIGHTS, {"bias": 0.0, "w_mom": 0.5, "w_vwap": 0.6, "w_rvol": 0.5}
        )
        self.w_mtf = _parse_weights(
            TS_MTF_WEIGHTS, {"w_1m": 0.25, "w_5m": 0.45, "w_15m": 0.30}
        )

    def _hprob(self, mom_bps: float, px_vs_vwap_bps: float, rvol_ratio: float) -> tuple[float, float]:
        w = self.w_base
        z = (
            w["bias"]
            + w["w_mom"]  * _tanh(mom_bps,            10.0)
            + w["w_vwap"] * _tanh(px_vs_vwap_bps,     12.0)
            + w["w_rvol"] * _tanh((rvol_ratio - 1.0),  0.5)
        )
        return _sigmoid(z), z

    async def run_once(self, symbol: str) -> Dict[str, Any]:
        s   = compute_signals(symbol)                  # 1m fast path
        stf = compute_signals_tf(symbol, ["1m","5m","15m"])

        p1,  z1  = self._hprob(float(s.get("mom1_bps", 0.0)),
                               float(s.get("px_vs_vwap_bps", 0.0)),
                               float(s.get("rvol_vs_recent", 1.0)))
        p5,  z5  = self._hprob(float(stf.get("mom_bps_5m", 0.0)),
                               float(stf.get("px_vs_vwap_bps_5m", 0.0)),
                               float(stf.get("rvol_5m", 1.0)))
        p15, z15 = self._hprob(float(stf.get("mom_bps_15m", 0.0)),
                               float(stf.get("px_vs_vwap_bps_15m", 0.0)),
                               float(stf.get("rvol_15m", 1.0)))

        w = self.w_mtf
        p_up = _clamp01(w["w_1m"]*p1 + w["w_5m"]*p5 + w["w_15m"]*p15)

        return {
            "score": round(10.0 * p_up, 2),
            "label": "trend_score",
            "details": {
                "p_up": p_up, "p_1m": p1, "p_5m": p5, "p_15m": p15,
                "z_1m": z1, "z_5m": z5, "z_15m": z15,
                "mom1_bps": float(s.get("mom1_bps", 0.0)),
                "mom5_bps": float(stf.get("mom_bps_5m", 0.0)),
                "mom15_bps": float(stf.get("mom_bps_15m", 0.0)),
                "px_vs_vwap_bps_1m":  float(s.get("px_vs_vwap_bps", 0.0)),
                "px_vs_vwap_bps_5m":  float(stf.get("px_vs_vwap_bps_5m", 0.0)),
                "px_vs_vwap_bps_15m": float(stf.get("px_vs_vwap_bps_15m", 0.0)),
                "rvol_1m":  float(s.get("rvol_vs_recent", 1.0)),
                "rvol_5m":  float(stf.get("rvol_5m", 1.0)),
                "rvol_15m": float(stf.get("rvol_15m", 1.0)),
            },
        }
