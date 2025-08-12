
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

    async def run_once(self, symbol: str) -> Optional[dict]:
        if not self._client:
            return None
        ctx = await self._gather(symbol)
        msgs = self._prompt(ctx)

        raw = None
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._client.chat.completions.create(
                    model=self.model, messages=msgs, response_format={"type":"json_object"},
                    temperature=0.2, max_tokens=600
                )
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            action = (data.get("action") or "observe").strip()
            confidence = float(data.get("confidence") or 0.0)
            rationale = str(data.get("rationale") or "").strip()
            tweaks = data.get("tweaks") or []

            # Clamp with trend_snapshot
            snap = ctx.get("trend_snapshot") or {}
            p_up = snap.get("p_up"); p5=snap.get("p_5m"); p15=snap.get("p_15m")
            entry_min_rvol = float(__import__('os').getenv("LLM_ENTRY_MIN_RVOL","1.20"))
            rvol = (ctx["signals"].get("rvol_vs_recent") or 0.0) or 0.0
            if action == "consider-entry":
                if (ctx["signals"].get("dcvd_2m",0) < 0) or (ctx["signals"].get("mom1_bps",0) <= -8) or (rvol < float(entry_min_rvol)) or (p_up is not None and p_up < 0.55):
                    action, confidence = "observe", min(confidence, 0.40)
                if (p5 is not None and p15 is not None) and (p5 < 0.55 and p15 < 0.55):
                    action, confidence = "observe", min(confidence, 0.40)
            if action == "consider-exit":
                if (ctx["signals"].get("dcvd_2m",0) > 0 and ctx["signals"].get("mom1_bps",0) > 0 and rvol >= 1.0) and (p_up is not None and p_up > 0.60):
                    action, confidence = "observe", min(confidence, 0.40)

            finding = {"score": max(0.0, min(10.0, confidence*10.0)),
                       "label":"llm_analysis",
                       "details":{"action":action,"confidence":confidence,"rationale":rationale[:600],"tweaks":tweaks[:5]}}

            if ALERT_WEBHOOK_URL and not SLACK_ANALYSIS_ONLY and confidence >= self.alert_min_conf:
                await post_webhook({"text": f"🧠 {symbol} {action} ({confidence:.2f}) — {rationale[:160]}"})
            return finding
        except Exception as e:
            det = {"error": f"{type(e).__name__}: {str(e)[:180]}"}
            if raw: det["raw"] = raw[:300]
            return {"score": 0.0, "label":"llm_error", "details": det}
