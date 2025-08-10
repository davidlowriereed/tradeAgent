import asyncio
import csv
import io
import json
import os
import time
import re
from collections import defaultdict, deque
from typing import Dict, Deque, Tuple, Optional
import httpx

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
import websockets

DB_URL = os.getenv("DATABASE_URL", None)
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", os.getenv("SYMBOL", "BTC-USD")).split(",") if s.strip()]
WS_URL = "wss://ws-feed.exchange.coinbase.com"


# ---------- Optional Postgres ----------
pg_conn = None
async def pg_connect():
    global pg_conn
    if DB_URL is None:
        return None
    try:
        import psycopg2
        pg_conn = psycopg2.connect(DB_URL)
        pg_conn.autocommit = True
        with pg_conn.cursor() as cur:
                    
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trades_crypto (
                    id BIGSERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL,
                    price DOUBLE PRECISION,
                    size DOUBLE PRECISION,
                    side TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS findings (
                    id BIGSERIAL PRIMARY KEY,
                    agent TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    score DOUBLE PRECISION NOT NULL,
                    label TEXT NOT NULL,
                    details JSONB DEFAULT '{}'::jsonb
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id BIGSERIAL PRIMARY KEY,
                    agent TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    status TEXT NOT NULL,
                    note TEXT
                );
                """
            )
        return pg_conn
    except Exception:
        pg_conn = None
        return None
        
class Agent:
    name: str
    interval_sec: int

    def __init__(self, name, interval_sec=10):
        self.name = name
        self.interval_sec = interval_sec

    async def run_once(self, symbol) -> Optional[dict]:
        """Return a finding dict or None."""
        raise NotImplementedError

class CVDDivergenceAgent(Agent):
    """
    Trap Spotter v0 — divergence between price and CVD over a short window.
    Emits a higher score when both price move (in bps) and CVD delta (abs) are large.
    Env tweaks:
      AGENT_CVD_WIN_SEC   (default 120)
      AGENT_CVD_MIN       (default 25.0)   # min |CVD delta| over window
      AGENT_CVD_MIN_BPS   (default 10)     # min |price change| in basis points
    """
    def __init__(self, window_sec=120, min_abs_cvd=25.0, min_price_bps=10, interval_sec=10):
        super().__init__("cvd_divergence", interval_sec)
        self.win = int(os.getenv("AGENT_CVD_WIN_SEC", str(window_sec)))
        self.min_abs_cvd = float(os.getenv("AGENT_CVD_MIN", str(min_abs_cvd)))
        self.min_price_bps = float(os.getenv("AGENT_CVD_MIN_BPS", str(min_price_bps)))

    async def run_once(self, symbol):
        now = time.time()
        window = [row for row in trades[symbol] if row[0] >= now - self.win]
        if len(window) < 12:
            return None

        p0 = window[0][1]
        p1 = window[-1][1]
        if p0 <= 0:
            return None

        price_bps = (p1 - p0) / p0 * 1e4  # basis points
        cvd_delta = 0.0
        for _, _, size, side in window:
            if side == "buy":
                cvd_delta += size
            elif side == "sell":
                cvd_delta -= size

        # Divergence: price ↑ & CVD ↓  OR price ↓ & CVD ↑
        diverges = (price_bps > self.min_price_bps and cvd_delta < -self.min_abs_cvd) or \
                   (price_bps < -self.min_price_bps and cvd_delta >  self.min_abs_cvd)

        if not diverges:
            return None

        # Score scales with both magnitudes (cap at 10 for simplicity)
        score = min(10.0,
                    abs(price_bps) / max(1.0, self.min_price_bps) +
                    abs(cvd_delta) / max(1.0, self.min_abs_cvd))

        return {
            "score": score,
            "label": "cvd_divergence",
            "details": {
                "window_sec": self.win,
                "price_bps": price_bps,
                "cvd_delta": cvd_delta,
                "p0": p0, "p1": p1
            }
        }

class MacroWatcher(Agent):
    """
    Macro Watcher v0 — polls a macro/news feed and emits a low-score finding
    so the system can include it as context. Optional; set MACRO_FEED_URL.
      MACRO_FEED_URL       (required to enable)
      MACRO_MIN_INTERVAL   (seconds between polls; default 600)
    """
    def __init__(self, interval_sec=None):
        poll = int(os.getenv("MACRO_MIN_INTERVAL", "600"))
        super().__init__("macro_watcher", interval_sec or poll)
        self.url = os.getenv("MACRO_FEED_URL")
        self._recent = deque(maxlen=200)   # crude de-dupe cache

    async def run_once(self, symbol):
        if not self.url:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(self.url)
            txt = r.text[:200_000]

            # naive RSS/Atom scrape for <title>...</title>
            import re, html
            titles = []
            for m in re.finditer(r"<title>(.*?)</title>", txt, re.I | re.S):
                t = re.sub("<.*?>", "", m.group(1)).strip()
                t = html.unescape(t)
                if t and t.lower() not in ("rss", "feed") and t not in self._recent:
                    self._recent.append(t)
                    titles.append(t)
            if not titles:
                return None

            return {
                "score": 1.0,                 # low weight; contextual
                "label": "macro_update",
                "details": {"items": titles[:5]}
            }
        except Exception:
            return None


class RVOLSpikeAgent(Agent):
    """Manipulation Hunter v0 — flags high relative volume bursts."""
    def __init__(self, min_rvol=2.5, interval_sec=10):
        super().__init__("rvol_spike", interval_sec)
        self.min_rvol = float(os.getenv("AGENT_RVOL_MIN", str(min_rvol)))

    async def run_once(self, symbol):
        # reuse compute_signals() for per-symbol features
        sig = compute_signals(symbol)
        rvol = sig.get("rvol_vs_recent") or 0.0
        if rvol >= self.min_rvol and sig["volume_5m"] > 0:
            score = min(10.0, rvol)  # simple score
            label = "rvol_spike"
            details = {
                "rvol": rvol,
                "volume_5m": sig["volume_5m"],
                "best_bid": sig["best_bid"],
                "best_ask": sig["best_ask"],
            }
            return {"score": score, "label": label, "details": details}
        return None

class CVDDivergenceAgent(Agent):
    """Trap Spotter v0 — price up while CVD down (or reverse) over ~2 minutes."""
    def __init__(self, window_sec=120, min_abs_cvd=25.0, min_price_bps=10, interval_sec=10):
        super().__init__("cvd_divergence", interval_sec)
        self.win = int(os.getenv("AGENT_CVD_WIN_SEC", str(window_sec)))
        self.min_abs_cvd = float(os.getenv("AGENT_CVD_MIN", str(min_abs_cvd)))
        self.min_price_bps = float(os.getenv("AGENT_CVD_MIN_BPS", str(min_price_bps)))

    async def run_once(self, symbol):
        # snapshot window
        now = time.time()
        window = [row for row in trades[symbol] if row[0] >= now - self.win]
        if len(window) < 10:
            return None

        p0 = window[0][1]
        p1 = window[-1][1]
        price_change_bps = (p1 - p0) / p0 * 1e4  # basis points

        # CVD delta over the window
        # (approx: recompute CVD delta from subset instead of whole deque)
        cvd_delta = 0.0
        for _, _, size, side in window:
            if side == "buy":
                cvd_delta += size
            elif side == "sell":
                cvd_delta -= size

        # Divergence: price ↑ & CVD ↓  OR price ↓ & CVD ↑
        diverges = (price_change_bps > self.min_price_bps and cvd_delta < -self.min_abs_cvd) or \
                   (price_change_bps < -self.min_price_bps and cvd_delta > self.min_abs_cvd)

        if diverges:
            score = min(10.0, abs(price_change_bps) / self.min_price_bps + abs(cvd_delta) / self.min_abs_cvd)
            label = "cvd_divergence"
            details = {
                "price_bps": price_change_bps,
                "cvd_delta": cvd_delta,
                "window_sec": self.win
            }
            return {"score": score, "label": label, "details": details}
        return None

class MacroWatcher(Agent):
    def __init__(self, interval_sec=600):
        super().__init__("macro_watcher", interval_sec)
        self.url = os.getenv("MACRO_FEED_URL")

    async def run_once(self, symbol):
        if not self.url: 
            return None
        try:
            import httpx, re, datetime
            r = await httpx.AsyncClient(timeout=10).get(self.url)
            text = r.text[:100000]
            # naive parse for items (works for many RSS feeds)
            items = []
            for m in re.finditer(r"<item>.*?<title>(.*?)</title>.*?</item>", text, re.S):
                title = re.sub("<.*?>","",m.group(1))
                items.append(title.strip())
            if items:
                return {"score": 1.0, "label": "macro_update", "details": {"items": items[:5]}}
        except Exception:
            pass
        return None

# ---- LLM Analyst Agent -------------------------------------------------------
from typing import Any, List
import math

class LLMAnalystAgent(Agent):
    """
    Reads recent signals + findings and produces a structured assessment:
      - action: "observe" | "prepare" | "consider-entry" | "consider-exit"
      - confidence: 0..1
      - rationale: short natural-language explanation
      - tweaks: [{param, delta, reason}]  # suggestions for AOA parameter schema

    ENV:
      LLM_ENABLE=true/false
      OPENAI_API_KEY=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
      OPENAI_MODEL=gpt-4o-mini (default)
      OPENAI_BASE_URL= (optional, OpenAI-compatible)
      LLM_MIN_INTERVAL=180
      LLM_MAX_INPUT_TOKENS=4000
      LLM_ALERT_MIN_CONF=0.65
    """
    def __init__(self, interval_sec=None):
        # respect ENV toggle
        enabled = os.getenv("LLM_ENABLE", "false").lower() == "true"
        poll = int(os.getenv("LLM_MIN_INTERVAL", "180"))
        super().__init__("llm_analyst", interval_sec or poll)
        self.enabled = enabled
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = os.getenv("OPENAI_BASE_URL") or None
        self.max_tokens = int(os.getenv("LLM_MAX_INPUT_TOKENS", "4000"))
        self.alert_min_conf = float(os.getenv("LLM_ALERT_MIN_CONF", "0.65"))

        try:
            from openai import OpenAI
            kwargs = {}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        except Exception:
            self._client = None
            self.enabled = False

        # tiny memory to avoid repeating identical outputs back-to-back
        self._last_hash: Dict[str, str] = {}

    async def _gather_context(self, symbol: str) -> dict:
        # current live signals
        sig = compute_signals(symbol)

        # last 5 mins of mini-stats
        now = time.time()
        window = [r for r in trades[symbol] if r[0] >= now - 300]
        n_tr = len(window)
        avg_sz = (sum(w[2] for w in window) / n_tr) if n_tr else 0.0
        price0 = window[0][1] if n_tr else None
        price1 = window[-1][1] if n_tr else None
        price_bps = ((price1 - price0) / price0 * 1e4) if (price0 and price0 > 0) else 0.0

        # latest recent findings from DB for this symbol
        recent_findings: List[dict] = []
        if pg_conn is None:
            await pg_connect()
        try:
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT ts_utc, agent, score, label, details
                    FROM findings
                    WHERE symbol=%s
                    ORDER BY ts_utc DESC
                    LIMIT 20
                """, (symbol,))
                for ts, a, sc, lb, det in cur.fetchall():
                    recent_findings.append({
                        "ts_utc": str(ts),
                        "agent": a,
                        "score": float(sc),
                        "label": lb,
                        "details": det
                    })
        except Exception:
            pass

        return {
            "symbol": symbol,
            "signals": {
                "cvd": sig.get("cvd"),
                "volume_5m": sig.get("volume_5m"),
                "rvol_vs_recent": sig.get("rvol_vs_recent"),
                "best_bid": sig.get("best_bid"),
                "best_ask": sig.get("best_ask"),
                "trades_cached": sig.get("trades_cached"),
                "price_bps_5m": price_bps,
                "avg_trade_size_5m": avg_sz,
                "trades_5m": n_tr,
            },
            "recent_findings": recent_findings,
        }

    def _prompt(self, ctx: dict) -> List[dict]:
        # system / user messages, JSON-mode friendly
        sys = (
            "You are the LLM Analyst Agent for a real-time trading system. "
            "You DO NOT place trades. You synthesize deterministic agent outputs "
            "into a concise recommendation. Output VALID JSON per the schema."
        )

        schema_hint = {
            "type": "object",
            "properties": {
                "action": {"enum": ["observe","prepare","consider-entry","consider-exit"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "tweaks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "param": {"type": "string"},
                            "delta": {"type": "number"},
                            "reason": {"type": "string"}
                        },
                        "required": ["param","delta","reason"]
                    }
                }
            },
            "required": ["action","confidence","rationale","tweaks"]
        }

        user = {
            "role": "user",
            "content": (
                "Context:\n"
                f"{json.dumps(ctx, ensure_ascii=False)[: self.max_tokens * 3]}\n\n"
                "Task:\n"
                "1) Decide action: observe | prepare | consider-entry | consider-exit\n"
                "2) Provide 1-2 sentence rationale (plain language).\n"
                "3) Suggest 0-3 parameter tweaks for the Alpha-Optimizer (AOA) across agents. "
                "   Use short param names, e.g., 'AGENT_RVOL_MIN', 'AGENT_CVD_MIN_BPS', "
                "   'ALERT_MIN_RVOL'. Use small deltas (e.g., ±0.1, ±2 bps).\n"
                "Return ONLY JSON: {action, confidence, rationale, tweaks}."
            )
        }

        # OpenAI Python SDK uses 'messages=[...]' with dicts like this:
        return [
            {"role": "system", "content": sys},
            user,
        ], schema_hint

    async def run_once(self, symbol) -> Optional[dict]:
        if not self.enabled or self._client is None:
            return None

        ctx = await self._gather_context(symbol)
        msgs, schema = self._prompt(ctx)

        try:
            # Use JSON response format for strict output
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

            # basic validation
            action = data.get("action") or "observe"
            confidence = float(data.get("confidence") or 0.0)
            rationale = str(data.get("rationale") or "").strip()
            tweaks = data.get("tweaks") or []

            finding = {
                "score": max(0.0, min(10.0, confidence * 10.0)),
                "label": "llm_analysis",
                "details": {
                    "action": action,
                    "confidence": confidence,
                    "rationale": rationale[:600],
                    "tweaks": tweaks[:5],
                },
            }

            # optional Slack/Discord ping for higher-confidence calls
            if ALERT_WEBHOOK_URL and confidence >= self.alert_min_conf:
                txt = (
                    f"🧠 LLM Analyst | {symbol} | {action} "
                    f"(conf {confidence:.2f}) — {rationale[:160]}"
                )
                try:
                    await httpx.AsyncClient(timeout=10).post(
                        ALERT_WEBHOOK_URL, json={"text": txt, "content": txt}
                    )
                except Exception:
                    pass

            return finding

        except Exception:
            return None


AGENTS = [
    RVOLSpikeAgent(),
    CVDDivergenceAgent(),
    MacroWatcher(),          # only active if MACRO_FEED_URL is set
    LLMAnalystAgent(),       # only active if LLM_ENABLE=true and key present
]

LLM_ALERT_MIN_CONF = float(os.getenv("LLM_ALERT_MIN_CONF", "0.65"))
ALERT_RATIONALE_CHARS = int(os.getenv("ALERT_RATIONALE_CHARS", "280"))

def _clean_text(s: str) -> str:
    if not s:
        return ""
    # strip HTML and trim
    s = re.sub(r"<[^>]+>", "", s).strip()
    return s

async def _post_webhook(payload: dict):
    if not ALERT_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(ALERT_WEBHOOK_URL, json=payload)
    except Exception:
        pass

def pg_insert_many(rows):
    if pg_conn is None or not rows:
        return
    try:
        from psycopg2.extras import execute_values
        with pg_conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO trades_crypto (symbol, ts_utc, price, size, side) VALUES %s",
                rows,
            )
    except Exception:
        pass
def pg_insert_finding(agent, symbol, score, label, details):
    if pg_conn is None:
        return
    try:
        import json as _json
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO findings (agent, symbol, score, label, details) VALUES (%s,%s,%s,%s,%s)",
                (agent, symbol, score, label, _json.dumps(details or {})),
            )
    except Exception:
        pass

def pg_agent_heartbeat(agent, status="ok", note=None):
    if pg_conn is None:
        return
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_runs (agent, status, note) VALUES (%s,%s,%s)",
                (agent, status, note),
            )
    except Exception:
        pass
        
# ---------- In-memory state (per symbol) ----------
MAX_TICKS = 5000
RV_SECS = 300  # 5 minutes

trades: Dict[str, Deque[Tuple[float,float,float,Optional[str]]]] = defaultdict(lambda: deque(maxlen=MAX_TICKS))
cvd: Dict[str, float] = defaultdict(float)
hist_5m: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=48))
best_bid: Dict[str, Optional[float]] = defaultdict(lambda: None)
best_ask: Dict[str, Optional[float]] = defaultdict(lambda: None)
db_buffer: Dict[str, Deque[Tuple[str,str,float,float,Optional[str]]]] = defaultdict(lambda: deque(maxlen=20000))

# ---------- Alerts (webhook is optional) ----------
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")  # Slack/Discord webhook
ALERT_MIN_RVOL = float(os.getenv("ALERT_MIN_RVOL", "2.0"))      # e.g., 2.0x
ALERT_CVD_DELTA = float(os.getenv("ALERT_CVD_DELTA", "50"))     # abs change in CVD within 5m window
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "180"))# per symbol

last_alert_ts: Dict[str, float] = defaultdict(lambda: 0.0)

app = FastAPI()

# tiny HTML page
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape())

# --- per-agent alert gating ---
AGENT_ALERT_MIN_SCORE_DEFAULT = float(os.getenv("AGENT_ALERT_MIN_SCORE_DEFAULT", "3.0"))
AGENT_ALERT_MIN_SCORE_CVD     = float(os.getenv("AGENT_ALERT_MIN_SCORE_CVD", "8.0"))
AGENT_ALERT_MIN_SCORE_RVOL    = float(os.getenv("AGENT_ALERT_MIN_SCORE_RVOL", "2.5"))
AGENT_ALERT_COOLDOWN_SEC      = int(os.getenv("AGENT_ALERT_COOLDOWN_SEC", "120"))

_last_agent_alert_ts = {}  # key: (agent, sym) -> ts

def _min_score_for(agent_name: str) -> float:
    if agent_name == "cvd_divergence":
        return AGENT_ALERT_MIN_SCORE_CVD
    if agent_name == "rvol_spike":
        return AGENT_ALERT_MIN_SCORE_RVOL
    if agent_name == "llm_analyst":
        # llm_analyst is gated by confidence separately; still keep a floor
        return 2.0
    return AGENT_ALERT_MIN_SCORE_DEFAULT

def _should_post(agent_name: str, sym: str, score: float) -> bool:
    now = time.time()
    key = (agent_name, sym)
    if score < _min_score_for(agent_name):
        return False
    last = _last_agent_alert_ts.get(key, 0)
    if now - last < AGENT_ALERT_COOLDOWN_SEC:
        return False
    _last_agent_alert_ts[key] = now
    return True

def _blocks_for_cvd(sym, finding):
    d = finding.get("details") or {}
    return [
        {"type":"header","text":{"type":"plain_text","text":f"🧊 CVD Divergence • {sym}"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Score*: {finding['score']:.2f}"},
            {"type":"mrkdwn","text":f"*Window*: {d.get('window_sec','—')}s"},
            {"type":"mrkdwn","text":f"*Price Δ (bps)*: {d.get('price_bps','—'):.2f}"},
            {"type":"mrkdwn","text":f"*CVD Δ*: {d.get('cvd_delta','—'):.2f}"},
        ]},
    ]

def _blocks_for_rvol(sym, finding):
    d = finding.get("details") or {}
    return [
        {"type":"header","text":{"type":"plain_text","text":f"🔥 RVOL Spike • {sym}"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Score*: {finding['score']:.2f}"},
            {"type":"mrkdwn","text":f"*RVOL*: {d.get('rvol','—'):.2f}x"},
            {"type":"mrkdwn","text":f"*Vol(5m)*: {d.get('volume_5m','—')}"},
            {"type":"mrkdwn","text":f"*Bid/Ask*: {d.get('best_bid','—')} / {d.get('best_ask','—')}"},
        ]},
    ]

def _blocks_for_llm(sym, finding):
    d = finding.get("details") or {}
    action = (d.get("action") or "observe").upper()
    conf   = float(d.get("confidence") or 0.0)
    rationale = _clean_text(d.get("rationale") or "")[:ALERT_RATIONALE_CHARS]
    tweaks = d.get("tweaks") or []
    tweak_lines = "\n".join([f"• `{t.get('param')}` Δ {t.get('delta')} — {t.get('reason','')}" for t in tweaks[:5]])
    blocks = [
        {"type":"header","text":{"type":"plain_text","text":f"🧠 LLM Analyst → {action} • {sym}"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Score*: {finding['score']:.2f}"},
            {"type":"mrkdwn","text":f"*Confidence*: {conf:.2f}"},
        ]},
        {"type":"section","text":{"type":"mrkdwn","text":f"*Rationale*\n{rationale or '—'}"}},
    ]
    if tweak_lines:
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"*Tweak suggestions*\n{tweak_lines}"}})
    return blocks


async def agents_loop():
    while True:
        try:
            if DB_URL and pg_conn is None:
                await pg_connect()

            for agent in AGENTS:
                for sym in SYMBOLS:
                    try:
                        finding = await agent.run_once(sym)
                        pg_agent_heartbeat(agent.name, "ok")

                        if not finding:
                            continue

                        # persist finding
                        pg_insert_finding(
                            agent.name,
                            sym,
                            float(finding.get("score", 0.0)),
                            finding.get("label", ""),
                            finding.get("details") or {},
                        )

                        # optional webhook alert (thresholded + rate-limited)
                        if ALERT_WEBHOOK_URL and _should_post(agent.name, sym, float(finding.get("score", 0.0))):
                            if agent.name == "cvd_divergence":
                                await _post_webhook({
                                    "text": f"CVD divergence {sym}",
                                    "blocks": _blocks_for_cvd(sym, finding),
                                })

                            elif agent.name == "rvol_spike":
                                await _post_webhook({
                                    "text": f"RVOL spike {sym}",
                                    "blocks": _blocks_for_rvol(sym, finding),
                                })

                            elif agent.name == "llm_analyst":
                                d = finding.get("details") or {}
                                conf = float(d.get("confidence") or 0.0)
                                if conf >= LLM_ALERT_MIN_CONF:
                                    await _post_webhook({
                                        "text": f"LLM analyst {sym}",
                                        "blocks": _blocks_for_llm(sym, finding),
                                    })

                            else:
                                # generic compact message
                                score_s = f"{float(finding.get('score', 0.0)):.2f}"
                                txt = f"🤖 {agent.name} | {sym} | {finding.get('label','')} | score={score_s}"
                                await _post_webhook({"text": txt})

                    except Exception as e:
                        pg_agent_heartbeat(agent.name, "error", note=str(e)[:150])

            await asyncio.sleep(5)

        except Exception:
            # back off a little if the outer loop throws
            await asyncio.sleep(5)



@app.post("/agents/run-now")
async def agents_run_now(symbol: Optional[str] = None):
    ran = []
    syms = [symbol] if symbol else SYMBOLS
    for agent in AGENTS:
        for sym in syms:
            try:
                finding = await agent.run_once(sym)
                pg_agent_heartbeat(agent.name, "ok", note="manual")
                if finding:
                    pg_insert_finding(agent.name, sym, finding["score"], finding["label"], finding["details"])
                    ran.append({"agent": agent.name, "symbol": sym, "score": finding["score"]})
            except Exception as e:
                pg_agent_heartbeat(agent.name, "error", note=f"manual: {e}")
    return {"ran": ran}

@app.get("/test-alert")
async def test_alert():
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return {"ok": False, "reason": "ALERT_WEBHOOK_URL not set"}
    msg = {"text": "✅ Test alert from Opportunity Radar", "content": "✅ Test alert from Opportunity Radar"}
    try:
        await httpx.AsyncClient(timeout=10).post(url, json=msg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok", "symbols": SYMBOLS, "trades_cached": {s: len(trades[s]) for s in SYMBOLS}}

@app.get("/db-health")
async def db_health():
    if DB_URL is None:
        return {"db": "not-configured"}
    ok = False
    try:
        if pg_conn is None:
            await pg_connect()
        if pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
                ok = True
    except Exception:
        ok = False
    return {"db": "ok" if ok else "error"}

def compute_signals(sym: str):
    now = time.time()
    cutoff = now - RV_SECS
    vol_5m = sum(sz for ts, _, sz, _ in trades[sym] if ts >= cutoff)
    baseline = sorted(hist_5m[sym])
    med = baseline[len(baseline)//2] if baseline else 0.0
    rvol = (vol_5m / med) if med > 0 else None
    return {
        "symbol": sym,
        "cvd": cvd[sym],
        "volume_5m": vol_5m,
        "rvol_vs_recent": rvol,
        "best_bid": best_bid[sym],
        "best_ask": best_ask[sym],
        "trades_cached": len(trades[sym]),
        "timestamp": now
    }

@app.get("/signals")
async def signals(symbol: str = Query(None)):
    sym = symbol or SYMBOLS[0]
    return JSONResponse(compute_signals(sym))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = env.get_template("index.html")
    return template.render(symbols=SYMBOLS)

@app.get("/agents")
async def agents_status():
    # latest run per agent
    if pg_conn is None:
        await pg_connect()
    rows = []
    try:
        with pg_conn.cursor() as cur:
            cur.execute("""
                SELECT agent, max(ts_utc) AS last_run
                FROM agent_runs
                GROUP BY agent
                ORDER BY agent
            """)
            rows = cur.fetchall()
    except Exception:
        pass
    return {"agents": [{"agent": a, "last_run": str(t)} for a,t in rows]}

@app.get("/findings")
async def get_findings(symbol: Optional[str]=None, agent: Optional[str]=None, limit: int=50):
    if pg_conn is None:
        await pg_connect()
    result = []
    try:
        q = "SELECT ts_utc, agent, symbol, score, label, details FROM findings"
        cond, vals = [], []
        if symbol:
            cond.append("symbol=%s"); vals.append(symbol)
        if agent:
            cond.append("agent=%s"); vals.append(agent)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY ts_utc DESC LIMIT %s"; vals.append(max(1, min(limit, 200)))
        with pg_conn.cursor() as cur:
            cur.execute(q, vals)
            for ts, a, s, sc, lb, det in cur.fetchall():
                result.append({
                    "ts_utc": str(ts), "agent": a, "symbol": s,
                    "score": float(sc), "label": lb, "details": det
                })
    except Exception:
        pass
    return {"findings": result}

@app.get("/export-csv")
async def export_csv(symbol: str = Query(None), n: int = 2000):
    sym = symbol or SYMBOLS[0]
    n = max(1, min(n, len(trades[sym])))
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["ts_epoch","price","size","side"])
    for row in list(trades[sym])[-n:]:
        w.writerow(row)
    output.seek(0)
    return StreamingResponse(output, media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={sym}-trades.csv"})

async def coinbase_ws(sym: str):
    """One websocket consumer per symbol."""
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"type":"subscribe","product_ids":[sym],"channels":["matches","ticker"]}))
                last_period = int(time.time() // RV_SECS)
                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("type") == "match":
                        ts = time.time()
                        size = float(msg.get("size", 0) or 0.0)
                        side = msg.get("side")
                        price = float(msg.get("price", 0) or 0.0)
                        trades[sym].append((ts, price, size, side))
                        if side == "buy":
                            cvd[sym] += size
                        elif side == "sell":
                            cvd[sym] -= size
                        if DB_URL:
                            db_buffer[sym].append((sym, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts)), price, size, side))

                        # roll 5-min baseline
                        period = int(ts // RV_SECS)
                        if period != last_period:
                            cutoff = ts - RV_SECS
                            vol = sum(s for t,_,s,_ in trades[sym] if t >= cutoff)
                            hist_5m[sym].append(vol)
                            last_period = period

                    elif msg.get("type") == "ticker":
                        try:
                            if msg.get("best_bid") is not None:
                                best_bid[sym] = float(msg["best_bid"])
                            if msg.get("best_ask") is not None:
                                best_ask[sym] = float(msg["best_ask"])
                        except Exception:
                            pass
        except Exception:
            await asyncio.sleep(2)

async def db_flush_loop():
    while True:
        try:
            if DB_URL and pg_conn is None:
                await pg_connect()
            if DB_URL and pg_conn:
                # flush up to 1000 rows per symbol
                for sym in SYMBOLS:
                    batch = []
                    while db_buffer[sym] and len(batch) < 1000:
                        batch.append(db_buffer[sym].popleft())
                    if batch:
                        pg_insert_many(batch)
        except Exception:
            pass
        await asyncio.sleep(3)

async def alert_loop():
    """Simple alert: RVOL spike OR fast CVD change."""
    if not ALERT_WEBHOOK_URL:
        return  # alerts disabled
    import httpx
    last_cvd_sample: Dict[str, Tuple[float,float]] = {}  # sym -> (ts, cvd)

    while True:
        try:
            now = time.time()
            for sym in SYMBOLS:
                sig = compute_signals(sym)
                rv = sig["rvol_vs_recent"] or 0.0

                # CVD delta over ~5m
                prev = last_cvd_sample.get(sym)
                cvd_delta = 0.0
                if prev and now - prev[0] >= RV_SECS:
                    cvd_delta = abs(sig["cvd"] - prev[1])
                    last_cvd_sample[sym] = (now, sig["cvd"])
                elif not prev:
                    last_cvd_sample[sym] = (now, sig["cvd"])

                should_alert = (rv >= ALERT_MIN_RVOL) or (cvd_delta >= ALERT_CVD_DELTA)
                if should_alert and (now - last_alert_ts[sym] >= ALERT_COOLDOWN_SEC):
                    text = f"🔔 {sym} | RVOL: {rv:.2f}x | CVDΔ(5m): {cvd_delta:.2f} | Bid/Ask: {sig['best_bid']} / {sig['best_ask']}"
                    try:
                        # Slack & Discord both accept simple JSON webhooks
                        await httpx.AsyncClient(timeout=10).post(ALERT_WEBHOOK_URL, json={"text": text, "content": text})
                        last_alert_ts[sym] = now
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup_event():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    for sym in SYMBOLS:
        asyncio.create_task(coinbase_ws(sym))
    asyncio.create_task(db_flush_loop())
    asyncio.create_task(alert_loop())
    asyncio.create_task(agents_loop())

    
