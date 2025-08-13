# backend/agents/llm_analyst.py  (full replace)

import json, asyncio, time
from typing import Optional

from ..signals import compute_signals, _dcvd, _mom_bps
from ..config import (
    LLM_ENABLE, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL,
    LLM_MIN_INTERVAL, LLM_MAX_INPUT_TOKENS, LLM_ALERT_MIN_CONF,
    SLACK_ANALYSIS_ONLY, ALERT_WEBHOOK_URL, LLM_USE_PROXY, LLM_IGNORE_PROXY,
)
from ..db import latest_trend_snapshot  # keep if you have this
from ..state import trades as STATE_TRADES
from .. import state  # <-- import the module so we can call state.get_position
from .base import Agent


class LLMAnalystAgent(Agent):
    """
    details = {
      action: "observe" | "prepare" | "consider-entry" | "consider-exit",
      confidence: 0..1,
      rationale: str,
      tweaks: [{param, delta, reason}] (optional)
    }
    """

    def __init__(self, interval_sec: int | None = None):
        super().__init__("llm_analyst", interval_sec or LLM_MIN_INTERVAL)
        self.enabled = LLM_ENABLE
        self.model = OPENAI_MODEL
        self.max_tokens = LLM_MAX_INPUT_TOKENS
        self.alert_min_conf = LLM_ALERT_MIN_CONF
        self._client = None
        self.disable_reason = None

        if not OPENAI_API_KEY or not self.enabled:
            if not OPENAI_API_KEY:
                self.disable_reason = "OPENAI_API_KEY missing"
            elif not self.enabled:
                self.disable_reason = "LLM_ENABLE=false"
            return

        try:
            from openai import OpenAI
            kwargs = {"api_key": OPENAI_API_KEY}
            if OPENAI_BASE_URL:
                kwargs["base_url"] = OPENAI_BASE_URL
            if LLM_USE_PROXY and not LLM_IGNORE_PROXY:
                import httpx, os
                proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
                if proxy:
                    kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=30.0)
            self._client = OpenAI(**kwargs)
        except Exception as e:
            self._client = None
            self.disable_reason = f"client_init: {type(e).__name__}"

    async def _gather_context(self, symbol: str) -> dict:
        sig = compute_signals(symbol)
        now = time.time()
        recent = [r for r in (STATE_TRADES.get(symbol) or []) if r[0] >= now - 120]

        ctx = {
            "symbol": symbol,
            "position": state.get_position(symbol),   # <-- persisted 'flat|long|short' etc
            "signals": sig | {
                "dcvd_2m": _dcvd(recent),
                "mom1_bps": _mom_bps(recent),
            },
            "trend_snapshot": latest_trend_snapshot(symbol) or {},
        }
        return ctx

    def _prompt(self, ctx: dict) -> list[dict]:
        sys = (
            "You are the LLM Analyst Agent. Decide one of: "
            "observe | prepare | consider-entry | consider-exit. "
            "Use position.status to keep advice consistent (e.g., if status='long', "
            "prefer 'consider-exit' or 'continue-long' instead of 'consider-entry'). "
            "Favor caution unless dcvd_2m>0, mom1_bps>0, RVOL>1.2 and trend_snapshot.p_up>=0.55. "
            "Output strict JSON {action, confidence, rationale, tweaks} only."
        )
        return [
            {"role": "system", "content": sys},
            {"role": "user",
             "content": "Context:\n" + json.dumps(ctx)[: self.max_tokens * 3] + "\nReturn JSON only."},
        ]

    async def run_once(self, symbol: str) -> Optional[dict]:
        if not self.enabled or self._client is None:
            return {
                "score": 0.0,
                "label": "llm_skipped",
                "details": {"reason": self.disable_reason or ("no_client" if self._client is None else "disabled")},
            }

        ctx = await self._gather_context(symbol)
        msgs = self._prompt(ctx)

        raw = None
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.chat.completions.create(
                    model=self.model,
                    messages=msgs,
                    response_format={"type": "json_object"},
                    temperature=0.2,
                    max_tokens=600,
                ),
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)

            action = (data.get("action") or "observe").strip()
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
            rationale = str(data.get("rationale") or "").strip()
            tweaks = data.get("tweaks") or []

            return {
                "score": round(confidence * 10.0, 2),
                "label": "llm_analysis",
                "details": {
                    "action": action,
                    "confidence": confidence,
                    "rationale": rationale[:600],
                    "tweaks": tweaks[:5],
                },
            }

        except Exception as e:
            det = {"error": f"{type(e).__name__}: {str(e)[:180]}"}
            if raw:
                det["raw"] = raw[:400]
            return {"score": 0.0, "label": "llm_error", "details": det}
