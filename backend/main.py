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
from openai import OpenAI
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
import websockets
import hashlib, time
from collections import defaultdict


DB_URL = os.getenv("DATABASE_URL", None)
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", os.getenv("SYMBOL", "BTC-USD")).split(",") if s.strip()]
WS_URL = "wss://ws-feed.exchange.coinbase.com"
_last_run_ts: Dict[Tuple[str, str], float] = defaultdict(float)
_last_run_ts = defaultdict(float)  # key: (agent.name, symbol) -> last ts
AGENTS = [
    RVOLSpikeAgent(),
    CVDDivergenceAgent(),
    MacroWatcher(),          # only active if MACRO_FEED_URL is set
    LLMAnalystAgent(),       # only active if LLM_ENABLE=true and key present
]

LLM_ALERT_MIN_CONF = float(os.getenv("LLM_ALERT_MIN_CONF", "0.65"))
ALERT_RATIONALE_CHARS = int(os.getenv("ALERT_RATIONALE_CHARS", "280"))
LLM_ANALYST_MIN_SCORE = float(os.getenv("LLM_ANALYST_MIN_SCORE", "2.0"))

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

ALERT_VERBOSE       = os.getenv("ALERT_VERBOSE", "true").lower() == "true"
AGENT_ALERT_COOLDOWN_SEC = int(os.getenv("AGENT_ALERT_COOLDOWN_SEC", "120"))
_last_analysis_post: Dict[Tuple[str,str], float] = defaultdict(float)  # (agent,symbol)->ts
SLACK_ANALYSIS_ONLY = os.getenv("SLACK_ANALYSIS_ONLY","false").lower() == "true"

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
        self.post_from_agent = os.getenv("LLM_POST_FROM_AGENT", "false").lower() == "true"

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

# ---- LLM Analyst Agent -------------------------------------------------------
from typing import Any, List
import math

def _agent_map():
    # Build a fresh name->agent map (handles reloading)
    return {a.name: a for a in AGENTS}

class LLMAnalystAgent(Agent):
    """
    Reads recent signals + findings and produces a structured assessment:
      - action: "observe" | "prepare" | "consider-entry" | "consider-exit"
      - confidence: 0..1
      - rationale: short natural-language explanation
      - tweaks: [{param, delta, reason}]

    ENV:
      LLM_ENABLE=true/false
      OPENAI_API_KEY=...
      OPENAI_MODEL=gpt-4o-mini (default; falls back to LLM_MODEL)
      LLM_MODEL= (optional fallback)
      OPENAI_BASE_URL= (optional, OpenAI-compatible)
      LLM_MIN_INTERVAL=180
      LLM_MAX_INPUT_TOKENS=4000
      LLM_ALERT_MIN_CONF=0.65

      # Proxy controls:
      # By default, the OpenAI httpx client ignores env proxies (trust_env=False)
      # Set LLM_USE_PROXY=true to force using HTTP(S)_PROXY explicitly.
      HTTP_PROXY / HTTPS_PROXY (used only if LLM_USE_PROXY=true)
      LLM_USE_PROXY=true|false
    """

    def __init__(self, interval_sec=None):
        enabled = os.getenv("LLM_ENABLE", "false").lower() == "true"
        poll = int(os.getenv("LLM_MIN_INTERVAL", "180"))
        super().__init__("llm_analyst", interval_sec or poll)
        self.post_from_agent = os.getenv("LLM_POST_FROM_AGENT", "false").lower() == "true"
        self.enabled = enabled
        # Model selection with fallback; prevents invalid values like "gpt-5" from breaking silently
        self.model = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        self.base_url = os.getenv("OPENAI_BASE_URL") or None
        self.max_tokens = int(os.getenv("LLM_MAX_INPUT_TOKENS", "4000"))
        self.alert_min_conf = float(os.getenv("LLM_ALERT_MIN_CONF", "0.65"))
        self._last_seen: Dict[str, Tuple[str, float]] = {}  # symbol -> (digest, ts)
        self._last_seen = {}  # symbol -> (digest, ts)

        # Track why we might be disabled (shown in /health)
        self.disable_reason = None
        self._client = None

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            self.enabled = False
            self.disable_reason = "OPENAI_API_KEY env var is missing"
        elif not self.enabled:
            # LLM explicitly disabled by env
            self.disable_reason = "LLM_ENABLE=false"
        else:
            # Try to construct the OpenAI client (no invalid 'proxies=' kwarg)
            try:
                import httpx
                from openai import OpenAI

                client_kwargs = {"api_key": api_key}
                if self.base_url:
                    client_kwargs["base_url"] = self.base_url

                # IMPORTANT:
                # Never allow ambient env proxies to affect the OpenAI client unless explicitly requested.
                # This avoids APIConnectionError where GET /models works but POST /chat/completions fails.
                use_proxy = os.getenv("LLM_USE_PROXY", "false").lower() == "true"
                proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")                

                if use_proxy and proxy_url:
                    http_client = httpx.Client(proxy=proxy_url, timeout=30.0, http2=False, trust_env=False)
                else:
                    http_client = httpx.Client(timeout=30.0, trust_env=False)

                client_kwargs["http_client"] = http_client
                self._client = OpenAI(**client_kwargs)

            except ImportError:
                self.enabled = False
                self.disable_reason = "OpenAI SDK not installed (add `openai` to requirements.txt)"
            except Exception as e:
                self.enabled = False
                self.disable_reason = f"Init error: {e}"

        # tiny memory to reduce duplicate outputs
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
                        AND label <> 'llm_error'
                        AND ts_utc > NOW() - INTERVAL '30 minutes'
                    ORDER BY ts_utc DESC
                    LIMIT 50
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
        sys = (
            "You are the LLM Analyst Agent for a real-time trading system.\n"
            "- You DO NOT place trades.\n"
            "- Base your decision on the current `signals` and any recent NON-error findings only.\n"
            "- Ignore connectivity/tooling errors (e.g., items labeled `llm_error`) and do NOT mention them.\n"
            "- If live data is sparse (e.g., volume_5m == 0 and trades_5m == 0), choose 'observe' with low confidence.\n"
            "Output VALID JSON only."
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
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "param": {"type": "string"},
                                    "delta": {"type": "number"},
                                    "reason": {"type": "string"}
                                },
                                "required": ["param","delta","reason"]
                            },
                            {"type": "string"}  # also accept compact "PARAM:+0.1" forms
                        ]
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
                "1) Decide action: observe | prepare | consider-entry | consider-exit.\n"
                "2) Provide a 1-2 sentence rationale in plain language. Do NOT mention tooling or connectivity.\n"
                "3) Suggest 0-3 parameter tweaks for the Alpha-Optimizer (AOA). Either give\n"
                "   objects {param, delta, reason} or compact strings like 'ALERT_MIN_RVOL:+0.1'.\n"
                "Return ONLY JSON: {action, confidence, rationale, tweaks}."
            )
        }
        return [
            {"role": "system", "content": sys},
            user,
        ], schema_hint

    async def run_once(self, symbol) -> Optional[dict]:
        if not self.enabled or self._client is None:
            return None
    
        import re, httpx
    
        # 1) Build context and defensively drop error findings from it
        ctx = await self._gather_context(symbol)
        if isinstance(ctx.get("recent_findings"), list):
            ctx["recent_findings"] = [
                f for f in ctx["recent_findings"] if str(f.get("label")) != "llm_error"
            ]
    
        msgs, _schema = self._prompt(ctx)
    
        # Helper: accept either dict tweaks or "PARAM:+0.1" strings and normalize
        def _normalize_tweaks(t):
            out = []
            if not isinstance(t, list):
                return out
            for item in t:
                if isinstance(item, dict):
                    param = str(item.get("param") or "").strip()
                    reason = str(item.get("reason") or "").strip() or "model-suggested"
                    try:
                        delta = float(item.get("delta"))
                    except Exception:
                        continue
                    if param:
                        out.append({"param": param, "delta": delta, "reason": reason})
                elif isinstance(item, str):
                    m = re.match(r"\s*([A-Za-z0-9_.:-]+)\s*:\s*([+\-]?\d+(?:\.\d+)?)", item)
                    if m:
                        out.append({
                            "param": m.group(1),
                            "delta": float(m.group(2)),
                            "reason": "compact format"
                        })
            return out[:5]
    
        raw = None
        try:
            # 2) Call OpenAI in a thread to avoid blocking the loop
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
    
            # 3) Parse + sanitize
            data = json.loads(raw)
            action = (data.get("action") or "observe").strip()
            try:
                confidence = float(data.get("confidence") or 0.0)
            except Exception:
                confidence = 0.0
            rationale = str(data.get("rationale") or "").strip()
            tweaks = _normalize_tweaks(data.get("tweaks") or [])
    
            # Clamp confidence + compute score
            confidence = max(0.0, min(1.0, confidence))
            score = max(0.0, min(10.0, confidence * 10.0))
    
            finding = {
                "score": score,
                "label": "llm_analysis",
                "details": {
                    "action": action,
                    "confidence": confidence,
                    "rationale": rationale[:600],
                    "tweaks": tweaks,
                },
            }
            
            import hashlib, time
            
            sig_src = json.dumps({
                "a": action,
                "c": round(confidence, 2),
                "r": rationale[:200],
                "t": [(t.get("param"), float(t.get("delta", 0))) for t in tweaks[:5]],
            }, sort_keys=True).encode("utf-8")
            digest = hashlib.sha1(sig_src).hexdigest()
            last = self._last_seen.get(symbol)
            now = time.time()
            if last and last[0] == digest and (now - last[1]) < 90:
                return None  # suppress duplicate insert/post within 90s
            self._last_seen[symbol] = (digest, now)
    
            # 4) Optional Slack ping from the agent itself (usually keep off;
            #     let the global poster handle cooldown/dedupe). Requires:
            #     self.post_from_agent = env LLM_POST_FROM_AGENT=="true"
            if getattr(self, "post_from_agent", False) and ALERT_WEBHOOK_URL and confidence >= self.alert_min_conf:
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
    
            self._last_hash[symbol] = raw[:800]

            return finding
    
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:180]}"
            det = {"error": err}
            if raw:
                det["raw"] = raw[:400]
            return {
                "score": 0.0,
                "label": "llm_error",
                "details": det,
            }





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

def _fmt_price(x):
    try: return f"{float(x):,.2f}"
    except: return str(x)

def _analysis_blocks(sym: str, finding: dict) -> dict:
    d = finding.get("details") or {}
    action     = (d.get("action") or "observe").upper()
    conf       = float(d.get("confidence") or 0.0)
    rationale  = _clean_text(d.get("rationale") or "")
    tweaks     = d.get("tweaks") or []

    tweak_lines = "\n".join(
        [f"• `{t.get('param')}` Δ {t.get('delta')} — {t.get('reason','')}" for t in tweaks[:4]]
    ) or "—"

    # include a quick snapshot
    sig = compute_signals(sym)
    snap = f"RVOL { (sig['rvol_vs_recent'] or 0):.2f}x · CVD {sig['cvd']:.0f} · Bid/Ask {_fmt_price(sig['best_bid'])} / {_fmt_price(sig['best_ask'])}"

    blocks = [
        {"type":"header","text":{"type":"plain_text","text":f"🧠 LLM Analyst → {action} · {sym}"}},
        {"type":"context","elements":[{"type":"mrkdwn","text":snap}]},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Score*\n{finding['score']:.2f}"},
            {"type":"mrkdwn","text":f"*Confidence*\n{conf:.2f}"},
        ]},
        {"type":"section","text":{"type":"mrkdwn","text":f"*Rationale*\n{rationale[:400]}"}},
        {"type":"section","text":{"type":"mrkdwn","text":f"*Tweaks*\n{tweak_lines}"}},
        {"type":"context","elements":[{"type":"mrkdwn","text":"<https://tradeagent-…ondigitalocean.app|Open dashboard> · auto"}]},
    ]
    return {"text": f"LLM: {action} {sym} (conf {conf:.2f})", "blocks": blocks}


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

            now = time.time()
            for agent in AGENTS:
                for sym in SYMBOLS:
                    key = (agent.name, sym)
                    # honor each agent’s interval_sec (default from its ctor/env)
                    if now - _last_run_ts[key] < max(1, int(getattr(agent, "interval_sec", 10))):
                        continue

                    try:
                        finding = await agent.run_once(sym)
                        _last_run_ts[key] = time.time()
                        pg_agent_heartbeat(agent.name, "ok")
                        if not finding:
                            continue

                        # optional LLM floor (see #3)
                        if agent.name == "llm_analyst" and finding["score"] < LLM_ANALYST_MIN_SCORE:
                            continue

                        pg_insert_finding(agent.name, sym, finding["score"], finding["label"], finding["details"])

                        # your existing Slack policy block here…
                    except Exception:
                        pg_agent_heartbeat(agent.name, "error")
                        # (optional) log/print the exception
        except Exception:
            # (optional) log/print the exception
            pass

        await asyncio.sleep(5)  # keep your base tick

async def digest_loop():
    interval = int(os.getenv("ALERT_SUMMARY_INTERVAL","0") or "0")
    if interval <= 0 or not ALERT_WEBHOOK_URL:
        return
    while True:
        try:
            rows = []
            if pg_conn is None:
                await pg_connect()
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT ts_utc, agent, symbol, score, label
                    FROM findings
                    WHERE ts_utc > NOW() - INTERVAL '10 minutes'
                    ORDER BY score DESC
                    LIMIT 6
                """)
                rows = cur.fetchall()

            if rows:
                lines = [f"• *{s}* — {a} `{lb}` (score {float(sc):.2f})" for _,a,s,sc,lb in rows]
                await _post_webhook({"text":"Summary",
                    "blocks":[
                        {"type":"header","text":{"type":"plain_text","text":"🧾 10-minute Summary"}},
                        {"type":"section","text":{"type":"mrkdwn","text":"\n".join(lines)}}
                    ]})
        except Exception:
            pass
        await asyncio.sleep(interval)



@app.post("/agents/run-now")
async def run_now(
    symbol: str = Query("BTC-USD"),
    names: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    insert: bool = Query(True),
    post_slack: bool = Query(False),
):
    # accept either ?names=a,b or ?agent=a
    chosen = []
    if names:
        chosen = [n.strip() for n in names.split(",") if n.strip()]
    elif agent:
        chosen = [agent.strip()]

    m = {a.name: a for a in AGENTS}
    if not chosen:
        chosen = list(m.keys())

    wanted = [n for n in chosen if n in m]
    if not wanted:
        return {"ok": False, "error": "no agents matched", "available": list(m.keys())}

    results = []
    for nm in wanted:
        ag = m[nm]
        try:
            # CALL THE RIGHT METHOD HERE:
            finding = await ag.run_once(symbol)

            # heartbeat + optional insert
            pg_agent_heartbeat(ag.name, "manual")
            if finding and insert:
                pg_insert_finding(ag.name, symbol, finding["score"], finding["label"], finding.get("details") or {})

            # optional Slack when manually testing
            if finding and post_slack and ALERT_WEBHOOK_URL:
                if ag.name == "llm_analyst":
                    conf = float((finding.get("details") or {}).get("confidence") or 0)
                    if conf >= LLM_ALERT_MIN_CONF:
                        await _post_webhook(_analysis_blocks(symbol, finding))
                elif not SLACK_ANALYSIS_ONLY and ALERT_VERBOSE:
                    await _post_webhook({"text": f"🤖 {ag.name} | {symbol} | {finding['label']} | score={finding['score']:.2f}"})

            results.append({"agent": ag.name, "finding": finding})
        except Exception as e:
            results.append({"agent": ag.name, "error": f"{type(e).__name__}: {e}"})

    return {"ok": True, "ran": wanted, "results": results}



@app.post("/agents/test-llm")
async def test_llm(
    symbol: str = Query("BTC-USD"),
    insert: bool = Query(False, description="insert the generated analysis into DB"),
    post_slack: bool = Query(False, description="post Slack if conf >= threshold"),
):
    llm = _agent_map().get("llm_analyst")
    if not llm or not getattr(llm, "enabled", False) or llm._client is None:
        return {"ok": False, "reason": "llm_analyst not enabled/ready"}

    # Gather live context but call OpenAI directly (no internal Slack, no duplicates)
    ctx = await llm._gather_context(symbol)
    msgs, _schema = llm._prompt(ctx)

    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: llm._client.chat.completions.create(
                model=llm.model,
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
        rationale = str(data.get("rationale") or "").strip()
        tweaks = data.get("tweaks") or []

        finding = {
            "agent": "llm_analyst",
            "symbol": symbol,
            "score": max(0.0, min(10.0, confidence * 10.0)),
            "label": "llm_analysis",
            "details": {
                "action": action,
                "confidence": confidence,
                "rationale": rationale[:600],
                "tweaks": tweaks[:5],
            },
        }

        if insert:
            pg_insert_finding("llm_analyst", symbol, finding["score"], finding["label"], finding["details"])

        if post_slack and ALERT_WEBHOOK_URL and confidence >= LLM_ALERT_MIN_CONF:
            await _post_webhook(_analysis_blocks(symbol, finding))

        return {"ok": True, "finding": finding}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.get("/llm/netcheck")
async def llm_netcheck():
    import httpx, os
    base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(base + "/models",
                            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"})
        return {"ok": r.status_code < 500, "status": r.status_code, "base": base, "snippet": (r.text or "")[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "base": base}

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
    agent_status = {}

    # Try to pull last heartbeat times (optional; ignore if DB missing)
    last_runs = {}
    try:
        if DB_URL and pg_conn is None:
            await pg_connect()
        if pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT agent, max(ts_utc) AS last_run
                    FROM agent_runs
                    GROUP BY agent
                """)
                for a, t in cur.fetchall():
                    last_runs[a] = str(t)
    except Exception:
        pass

    for agent in AGENTS:
        # Default structure
        info = {"status": "ok"}
        # If agent has an "enabled" flag, reflect it
        if hasattr(agent, "enabled") and not getattr(agent, "enabled"):
            info["status"] = "disabled"
            info["reason"] = getattr(agent, "disable_reason", "unknown")
        # Add last run if we have it
        if agent.name in last_runs:
            info["last_run"] = last_runs[agent.name]
        agent_status[agent.name] = info

    return {
        "status": "ok",
        "symbols": SYMBOLS,
        "trades_cached": {s: len(trades[s]) for s in SYMBOLS},
        "agents": agent_status
    }


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

# simple mirror so we have both paths available
@app.post("/llm/selftest")
@app.post("/agents/llm-selftest")
async def llm_selftest(symbol: str = Query("BTC-USD")):
    # find the LLM agent
    llm = next((a for a in AGENTS if getattr(a, "name", "") == "llm_analyst"), None)
    if not llm or not llm.enabled or llm._client is None:
        return {"ok": False, "reason": "llm_analyst not enabled/ready"}

    # minimal context (keeps it fast & deterministic)
    ctx = {
        "symbol": symbol,
        "signals": {"cvd": 1.23, "volume_5m": 2.34, "rvol_vs_recent": 1.1,
                    "best_bid": 100.0, "best_ask": 100.1, "trades_cached": 42,
                    "price_bps_5m": 5.0, "avg_trade_size_5m": 0.01, "trades_5m": 20},
        "recent_findings": []
    }
    msgs, _ = llm._prompt(ctx)

    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: llm._client.chat.completions.create(
                model=llm.model,
                messages=msgs,
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=200,
            )
        )
        raw = resp.choices[0].message.content
        return {"ok": True, "raw": raw}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.post("/llm/httpx-probe")
async def llm_httpx_probe(model: str = Query("gpt-4o-mini")):
    import os, httpx, json
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"ok": False, "error": "no OPENAI_API_KEY"}
    # Force no env proxies, no HTTP/2 weirdness
    async with httpx.AsyncClient(timeout=20, http2=False, trust_env=False) as c:
        try:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say 'pong' as JSON"}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": 20,
                },
            )
            return {"ok": r.status_code < 400, "status": r.status_code, "body": (r.text or "")[:400]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

@app.get("/llm/config")
async def llm_config():
    # Peek at what the running process actually sees
    return {
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL"),
        "LLM_MODEL": os.getenv("LLM_MODEL"),
        "OPENAI_BASE_URL": os.getenv("OPENAI_BASE_URL"),
        "HTTPS_PROXY": os.getenv("HTTPS_PROXY"),
        "HTTP_PROXY": os.getenv("HTTP_PROXY"),
        "LLM_USE_PROXY": os.getenv("LLM_USE_PROXY"),
        "LLM_ENABLE": os.getenv("LLM_ENABLE"),
    }


@app.on_event("startup")
async def startup_event():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    for sym in SYMBOLS:
        asyncio.create_task(coinbase_ws(sym))
    asyncio.create_task(db_flush_loop())
     # asyncio.create_task(alert_loop())  # <- comment out OR gate it:
    if not SLACK_ANALYSIS_ONLY:
        asyncio.create_task(alert_loop())
    asyncio.create_task(agents_loop())


    
