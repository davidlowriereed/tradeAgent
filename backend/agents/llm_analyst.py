from __future__ import annotations
from typing import Optional
from .base import Agent
import os, json, re
from ..db import heartbeat

LLM_ENABLE = os.getenv("LLM_ENABLE", "false").lower() in ("1","true","yes")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini-2024-07-18")
MIN_CONF = float(os.getenv("LLM_ALERT_MIN_CONF", "0.6"))
MIN_SCORE = float(os.getenv("LLM_ANALYST_MIN_SCORE", "0.0"))
RATIONALE_CHARS = int(os.getenv("ALERT_RATIONALE_CHARS", "400"))

class LLMAnalystAgent(Agent):
    name = "llm_analyst"
    def __init__(self, interval_sec: int = 60):
        super().__init__(self.name, interval_sec)

    async def run_once(self, symbol: str) -> Optional[dict]:
        if not LLM_ENABLE:
            return None

        from ..signals import compute_signals_tf, compute_signals
        tf = await compute_signals_tf(symbol)
        s  = await compute_signals(symbol)

        score = (
            abs(tf.get("mom_bps_1m", 0))
            + abs(tf.get("px_vs_vwap_bps_1m", 0)) / 5
            + max(0.0, (tf.get("rvol_1m", 1.0) - 1.0)) * 50
        ) / 10.0
        score = float(max(0.0, min(score, 10.0)))

        posture, conf, rationale = "no_position", 0.5, "Neutral"

         try:
            from openai import OpenAI
            client = OpenAI()

            prompt = (
                f"Given features for {symbol}:\n"
                f"mom_1m={tf.get('mom_bps_1m',0)} bps, "
                f"mom_5m={tf.get('mom_bps_5m',0)} bps, "
                f"mom_15m={tf.get('mom_bps_15m',0)} bps,\n"
                f"vwap_1m={tf.get('px_vs_vwap_bps_1m',0)} bps, "
                f"rvol_1m={tf.get('rvol_1m',1.0)},\n"
                f"trend_p_up={s.get('trend_p_up', 0.5)}.\n"
                "Recommend one of: LONG, SHORT, or FLAT. "
                "Return JSON {\"posture\":\"...\",\"confidence\":0-1,\"rationale\":\"...\"}.\n"
            )

            msg = [
                {"role": "system", "content": "You are a disciplined crypto trade analyst. Be concise."},
                {"role": "user", "content": prompt},
            ]

            rsp = client.chat.completions.create(
                model=MODEL, messages=msg, temperature=0.2, max_tokens=180
            )
            txt = rsp.choices[0].message.content or "{}"
            m = re.search(r"\{.*\}", txt, re.S)
            obj = json.loads(m.group(0) if m else "{}")

            posture_map = {"LONG": "long", "SHORT": "short", "FLAT": "no_position"}
            posture = posture_map.get(str(obj.get("posture", "")).upper(), "no_position")
            conf = float(obj.get("confidence", 0.5) or 0.5)
            rationale = (obj.get("rationale", "") or "N/A")[:RATIONALE_CHARS]

            # success: note a healthy heartbeat
            await heartbeat(self.name, "ok")
        except Exception as e:
            # failure: record the error so /health and the dashboard can show it
            await heartbeat(self.name, f"error: {type(e).__name__}")
            return None

        return {
            "score": round(max(score, conf * 10), 2),
            "label": "llm_signal",
            "details": {
                "posture": posture, "confidence": conf,
                "trend_p_up": s.get("trend_p_up", 0.5),
                "mom_1m": tf.get("mom_bps_1m", 0),
                "rvol_1m": tf.get("rvol_1m", 1.0),
                "rationale": rationale,
            },
        }
