from __future__ import annotations
from typing import Optional
from .base import Agent
import os, math, time

LLM_ENABLE = os.getenv("LLM_ENABLE", "false").lower() in ("1","true","yes")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini-2024-07-18")
MIN_CONF = float(os.getenv("LLM_ALERT_MIN_CONF", "0.6"))
MIN_SCORE = float(os.getenv("LLM_ANALYST_MIN_SCORE", "0.0"))

class LLMAnalystAgent(Agent):
    name = "llm_analyst"
    def __init__(self, interval_sec: int = 60):
        super().__init__(self.name, interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        if not LLM_ENABLE:
            return None
        # Pull your latest computed features
        from ..signals import compute_signals_tf, compute_signals
        tf = await compute_signals_tf(symbol)
        s  = await compute_signals(symbol)

        # Simple rubric → score + posture
        score = (abs(tf.get("mom_bps_1m", 0)) + abs(tf.get("px_vs_vwap_bps_1m", 0)) / 5 + 50*(tf.get("rvol_1m",1)-1))
        score = float(min(max(score/10.0, 0.0), 10.0))

        posture = "no_position"
        rationale = "Neutral"
        conf = 0.5

        try:
            from openai import OpenAI
            client = OpenAI()
            msg = [
              {"role": "system", "content": "You are a disciplined crypto trade analyst."},
              {"role": "user", "content":
               f\"\"\"Given features for {symbol}:
               mom_1m={tf.get('mom_bps_1m',0)} bps, mom_5m={tf.get('mom_bps_5m',0)} bps,
               vwap_1m={tf.get('px_vs_vwap_bps_1m',0)} bps, rvol_1m={tf.get('rvol_1m',1)},
               trend_p_up={s.get('trend_p_up', 0.5)}.
               Recommend one of: LONG, SHORT, or FLAT. Return JSON {{"posture": "...", "confidence": 0-1, "rationale": "..."}}.
               \"\"\"}
            ]
            rsp = client.chat.completions.create(model=MODEL, messages=msg, temperature=0.2, max_tokens=180)
            txt = rsp.choices[0].message.content or ""
            import json, re
            m = re.search(r\"\\{.*\\}\", txt, re.S)
            if m:
                obj = json.loads(m.group(0))
                posture_map = {"LONG":"long", "SHORT":"short", "FLAT":"no_position"}
                posture = posture_map.get(str(obj.get("posture","")).upper(), "no_position")
                conf = float(obj.get("confidence",0.5) or 0.5)
                rationale = str(obj.get("rationale","")).strip()[:int(os.getenv("ALERT_RATIONALE_CHARS","400"))]
        except Exception:
            pass

        if conf < MIN_CONF and score < MIN_SCORE:
            return None

        return {
          "score": round(max(score, conf*10), 2),
          "label": "llm_signal",
          "details": {
            "posture": posture, "confidence": conf,
            "trend_p_up": s.get("trend_p_up", 0.5),
            "mom_1m": tf.get("mom_bps_1m", 0), "rvol_1m": tf.get("rvol_1m",1),
            "rationale": rationale
          }
        }
