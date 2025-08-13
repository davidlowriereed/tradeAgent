
import json, asyncio, time
from typing import Optional, List
from ..signals import compute_signals, _dcvd, _mom_bps
from ..config import (LLM_ENABLE, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL,
                      LLM_MIN_INTERVAL, LLM_MAX_INPUT_TOKENS, LLM_ALERT_MIN_CONF,
                      SLACK_ANALYSIS_ONLY, ALERT_WEBHOOK_URL, LLM_USE_PROXY, LLM_IGNORE_PROXY)
from ..db import pg_conn, latest_trend_snapshot
from ..services.slack import post_webhook
from .base import Agent

class LLMAnalystAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("llm_analyst", interval_sec or LLM_MIN_INTERVAL)
        self.enabled = LLM_ENABLE
        self.model = OPENAI_MODEL
        self.max_tokens = LLM_MAX_INPUT_TOKENS
        self.alert_min_conf = LLM_ALERT_MIN_CONF
        self._client = None

        if not OPENAI_API_KEY or not self.enabled:
            return
        try:
            from openai import OpenAI
            kwargs = {"api_key": OPENAI_API_KEY}
            if OPENAI_BASE_URL:
                kwargs["base_url"] = OPENAI_BASE_URL
            if LLM_USE_PROXY and not LLM_IGNORE_PROXY:
                import httpx as _httpx, os
                proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
                if proxy:
                    kwargs["http_client"] = _httpx.Client(proxy=proxy, timeout=30.0)
            self._client = OpenAI(**kwargs)
        except Exception:
            self._client = None

    async def _gather(self, symbol: str) -> dict:
        sig = compute_signals(symbol)
        now = time.time()
        w2 = [r for r in __import__('backend.state', fromlist=['trades']).state.trades[symbol] if r[0] >= now - 120]  # quick access
        ctx = {
            "symbol": symbol,
            "signals": sig | {"dcvd_2m": _dcvd(w2), "mom1_bps": _mom_bps(w2)},
            "trend_snapshot": latest_trend_snapshot(symbol) or {},
        }
        return ctx

    def _prompt(self, ctx: dict) -> list[dict]:
        sys = (
            "You are the LLM Analyst Agent. Decide one of: observe | prepare | consider-entry | consider-exit. "
            "Favor caution unless dcvd_2m>0, mom1_bps>0, RVOL>1.2 and trend_snapshot.p_up>=0.55 with 5m/15m supportive. "
            "Output strict JSON {action, confidence, rationale, tweaks}."
        )
        return [{"role":"system","content":sys},
                {"role":"user","content":"Context:\n" + json.dumps(ctx)[: self.max_tokens*3] + "\nReturn JSON only."}]

async def run_once(self, symbol) -> Optional[dict]:
    # PRE-FLIGHT: never be silent
    if not self.enabled:
        return {
            "score": 0.0,
            "label": "llm_skipped",
            "details": {"reason": self.disable_reason or "disabled"}
        }
    if self._client is None:
        return {
            "score": 0.0,
            "label": "llm_skipped",
            "details": {"reason": "no_client"}
        }

    ctx = await self._gather_context(symbol)
    # (tip) filter noisy error findings from ctx if you want
    msgs, _schema = self._prompt(ctx)

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
            )
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)

        action = (data.get("action") or "observe").strip()
        confidence = float(data.get("confidence") or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(data.get("rationale") or "").strip()
        tweaks = data.get("tweaks") or []

        finding = {
            "score": round(confidence * 10.0, 2),
            "label": "llm_analysis",
            "details": {
                "action": action,
                "confidence": confidence,
                "rationale": rationale[:600],
                "tweaks": tweaks[:5],
            },
        }
        return finding

    except Exception as e:
        det = {"error": f"{type(e).__name__}: {str(e)[:180]}"}
        if raw:
            det["raw"] = raw[:400]
        return {"score": 0.0, "label": "llm_error", "details": det}
