import os, time, json, asyncio, math, hashlib, csv, io
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse, HTMLResponse

# -------------------------------
# Globals / In-memory state
# -------------------------------
SYMBOLS = [s.strip() for s in os.getenv("SYMBOL", "BTC-USD,ETH-USD,ADA-USD").split(",") if s.strip()]
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL") or ""
SLACK_ANALYSIS_ONLY = os.getenv("SLACK_ANALYSIS_ONLY", "true").lower() == "true"
ALERT_VERBOSE = os.getenv("ALERT_VERBOSE", "false").lower() == "true"

LLM_ANALYST_MIN_SCORE = float(os.getenv("LLM_ANALYST_MIN_SCORE", "3.0"))
LLM_ALERT_MIN_CONF = float(os.getenv("LLM_ALERT_MIN_CONF", "0.60"))
AGENT_ALERT_COOLDOWN_SEC = int(os.getenv("AGENT_ALERT_COOLDOWN_SEC", "120"))

# TrendScore thresholds / config
TS_INTERVAL = int(os.getenv("TS_INTERVAL", "30"))
TS_ENTRY = float(os.getenv("TS_ENTRY", "0.65"))
TS_EXIT  = float(os.getenv("TS_EXIT",  "0.35"))
TS_PERSIST = int(os.getenv("TS_PERSIST", "2"))
TS_WEIGHTS = os.getenv("TS_WEIGHTS", "default")
TS_MTF_WEIGHTS = os.getenv("TS_MTF_WEIGHTS", "default")  # optional per-horizon weights

# Posture guard cadence
POSTURE_GUARD_INTERVAL = int(os.getenv("POSTURE_GUARD_INTERVAL", "60"))

# Trades buffer (ts, price, size, side) side in {"buy","sell"}
trades: Dict[str, deque] = {sym: deque(maxlen=50_000) for sym in SYMBOLS}

# A minimal price cache (best bid/ask)
best_px: Dict[str, Tuple[float, float]] = {sym: (None, None) for sym in SYMBOLS}

# DB wiring (simple psycopg2 use)
pg_conn = None
def pg_connect_sync():
    global pg_conn
    try:
        import psycopg2
        DB_URL = os.getenv("DATABASE_URL")
        if not DB_URL:
            return
        pg_conn = psycopg2.connect(DB_URL)
        pg_conn.autocommit = True
        with pg_conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS findings (
              ts_utc TIMESTAMPTZ DEFAULT NOW(),
              agent TEXT,
              symbol TEXT,
              score DOUBLE PRECISION,
              label TEXT,
              details JSONB
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
              ts_utc TIMESTAMPTZ DEFAULT NOW(),
              agent TEXT,
              status TEXT
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_findings_symbol_ts ON findings(symbol, ts_utc DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_ts ON agent_runs(agent, ts_utc DESC);")
    except Exception:
        pg_conn = None

async def pg_connect():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, pg_connect_sync)

def pg_insert_finding(agent: str, symbol: str, score: float, label: str, details: dict):
    try:
        if pg_conn is None:
            return
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO findings(agent, symbol, score, label, details) VALUES (%s,%s,%s,%s,%s)",
                (agent, symbol, score, label, json.dumps(details)),
            )
    except Exception:
        pass

def pg_agent_heartbeat(agent: str, status: str = "ok"):
    try:
        if pg_conn is None:
            return
        with pg_conn.cursor() as cur:
            cur.execute("INSERT INTO agent_runs(agent, status) VALUES (%s,%s)", (agent, status))
    except Exception:
        pass

# -------------------------------
# Utility: compute_signals + multi-timeframe helpers
# -------------------------------
def _mom_bps(win: list) -> float:
    if not win: return 0.0
    p0 = win[0][1]; p1 = win[-1][1]
    if not p0: return 0.0
    return (p1 - p0) / p0 * 1e4

def _dcvd(win: list) -> float:
    total = 0.0
    for _, _, sz, sd in win:
        if sd == "buy": total += (sz or 0.0)
        elif sd == "sell": total -= (sz or 0.0)
    return total

def _rvol(w5: list, w15: list) -> float | None:
    vol5 = sum((r[2] or 0.0) for r in w5)
    baseline = (sum((r[2] or 0.0) for r in w15)/3.0) if w15 else 0.0
    return (vol5 / baseline) if baseline > 0 else None

def compute_signals(symbol: str) -> dict:
    """
    Derive quick signals from the trades buffer.
    """
    now = time.time()
    buf = trades[symbol]
    w1  = [r for r in buf if r[0] >= now - 60]
    w5  = [r for r in buf if r[0] >= now - 300]
    w15 = [r for r in buf if r[0] >= now - 900]

    bid, ask = best_px.get(symbol, (None, None))
    rvol_vs_recent = _rvol(w5, w15)
    mom1_bps = _mom_bps(w1)
    mom5_bps = _mom_bps(w5)

    return {
        "cvd": _dcvd(w5),
        "volume_5m": sum((r[2] or 0.0) for r in w5),
        "rvol_vs_recent": rvol_vs_recent,
        "best_bid": bid,
        "best_ask": ask,
        "trades_cached": len(buf),
        "mom1_bps": mom1_bps,
        "mom5_bps": mom5_bps,
    }

# -------------------------------
# Minimal Slack helpers
# -------------------------------
_last_post_digest: Dict[Tuple[str,str], float] = defaultdict(float)

async def _post_webhook(blocks_or_text: dict | str):
    if not ALERT_WEBHOOK_URL:
        return
    payload = {"text": blocks_or_text} if isinstance(blocks_or_text, str) else blocks_or_text
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(ALERT_WEBHOOK_URL, json=payload)
    except Exception:
        pass

def _should_post(agent: str, symbol: str, score: float) -> bool:
    now = time.time()
    key = (agent, symbol)
    if now - _last_post_digest[key] < AGENT_ALERT_COOLDOWN_SEC:
        return False
    _last_post_digest[key] = now
    return True

# -------------------------------
# Agent base
# -------------------------------
class Agent:
    def __init__(self, name: str, interval_sec: int = 60):
        self.name = name
        self.interval_sec = interval_sec

    async def run_once(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError

# -------------------------------
# RVOL spike agent
# -------------------------------
class RVOLSpikeAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("rvol_spike", interval_sec or 30)
        self.min_rvol = float(os.getenv("ALERT_MIN_RVOL", "5.0"))

    async def run_once(self, symbol: str) -> Optional[dict]:
        sig = compute_signals(symbol)
        rvol = sig.get("rvol_vs_recent") or 0.0
        if rvol >= self.min_rvol:
            return {
                "score": min(10.0, rvol),
                "label": "rvol_spike",
                "details": {
                    "rvol": rvol,
                    "volume_5m": sig.get("volume_5m"),
                    "best_bid": sig.get("best_bid"),
                    "best_ask": sig.get("best_ask"),
                },
            }
        return None

# -------------------------------
# CVD divergence agent
# -------------------------------
class CvdDivergenceAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("cvd_divergence", interval_sec or 30)
        self.min_delta = float(os.getenv("ALERT_CVD_DELTA", "75"))

    async def run_once(self, symbol: str) -> Optional[dict]:
        now = time.time()
        w5 = [r for r in trades[symbol] if r[0] >= now - 300]
        d5 = _dcvd(w5)
        sc = abs(d5) / max(1.0, self.min_delta)
        if sc >= 1.0:
            return {"score": min(10.0, 10.0*sc), "label": "cvd_divergence", "details": {"delta_5m": d5}}
        return None

# -------------------------------
# TrendScoreAgent (multi-timeframe ensemble)
# -------------------------------
def _tanh(x, s):
    try:
        return math.tanh(x / (s if s != 0 else 1e-9))
    except Exception:
        return 0.0

def _sigmoid(z):
    try:
        return 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0

class TrendScoreAgent(Agent):
    """
    Produces an ensemble probability p_up in [0,1] for next ~10–30m.
    Also exposes per-horizon scores p_1m / p_5m / p_15m.
    """
    def __init__(self, interval_sec: int | None = None):
        super().__init__("trend_score", interval_sec or TS_INTERVAL)
        self.w_base  = self._init_base_weights()
        self.w_mtf   = self._init_mtf_weights()

    def _init_base_weights(self):
        # defaults if TS_WEIGHTS not provided
        if TS_WEIGHTS and TS_WEIGHTS.strip().lower() != "default":
            try: return json.loads(TS_WEIGHTS)
            except Exception: pass
        return {
            "bias": 0.0,
            "w_dcvd": 0.8,
            "w_rvol": 0.5,
            "w_rvratio": 0.7,   # negative component (applied with minus sign)
            "w_vwap": 0.6,
            "w_mom": 0.5,
        }

    def _init_mtf_weights(self):
        if TS_MTF_WEIGHTS and TS_MTF_WEIGHTS.strip().lower() != "default":
            try: return json.loads(TS_MTF_WEIGHTS)
            except Exception: pass
        # default ensemble weights for horizons: 1m, 5m, 15m
        return {"w_1m": 0.25, "w_5m": 0.45, "w_15m": 0.30}

    def _horizon_prob(self, dcvd: float, rvol: float, rvratio: float, px_vs_vwap_bps: float, mom_bps: float) -> float:
        w = self.w_base
        z = (
            w["bias"]
            + w["w_dcvd"]  * _tanh(dcvd, 1500.0)
            + w["w_rvol"]  * _tanh((rvol - 1.0), 0.5)
            - w["w_rvratio"] * _tanh((rvratio - 1.0), 0.3)
            + w["w_vwap"]  * _tanh(px_vs_vwap_bps, 12.0)
            + w["w_mom"]   * _tanh(mom_bps, 10.0)
        )
        return _sigmoid(z)

    async def run_once(self, symbol: str) -> Optional[dict]:
        now = time.time()
        buf = trades[symbol]

        w1  = [r for r in buf if r[0] >= now - 60]
        w5  = [r for r in buf if r[0] >= now - 300]
        w15 = [r for r in buf if r[0] >= now - 900]

        # flows
        d1, d5, d15 = _dcvd(w1), _dcvd(w5), _dcvd(w15)

        # RVOL(5m) + buy/sell ratio(15m)
        rvol = _rvol(w5, w15) or 0.0
        up = sum((r[2] or 0.0) for r in w15 if (r[3] or "") == "buy")
        dn = sum((r[2] or 0.0) for r in w15 if (r[3] or "") == "sell")
        rvratio = (dn + 1e-9) / (up + 1e-9)

        # momentum
        mom1, mom5 = _mom_bps(w1), _mom_bps(w5)

        # price vs vwap(20m)
        vwin = [r for r in buf if r[0] >= now - 20*60]
        vwap = None
        if vwin:
            num = sum((r[1] or 0.0)*(r[2] or 0.0) for r in vwin)
            den = sum((r[2] or 0.0) for r in vwin)
            vwap = (num/den) if den else None

        bid, ask = best_px.get(symbol, (None, None))
        px = ask or bid
        px_vwap_bps = 0.0
        if vwap and px:
            px_vwap_bps = (px - vwap) / vwap * 1e4

        # per-horizon map: (dcvd, mom_bps) scaling
        p1  = self._horizon_prob(d1, rvol, rvratio, px_vwap_bps, mom1)
        p5  = self._horizon_prob(d5, rvol, rvratio, px_vwap_bps, mom5)
        # 15m momentum: reuse 5m momentum as slow proxy
        p15 = self._horizon_prob(d15, rvol, rvratio, px_vwap_bps, mom5*0.6)

        wm = self.w_mtf
        p_ens = max(0.0, min(1.0, wm["w_1m"]*p1 + wm["w_5m"]*p5 + wm["w_15m"]*p15))

        return {
            "score": round(p_ens * 10.0, 2),
            "label": "trend_score",
            "details": {
                "p_up": round(p_ens, 4),
                "p_1m": round(p1, 4), "p_5m": round(p5, 4), "p_15m": round(p15, 4),
                "features": {
                    "dcvd_1m": d1, "dcvd_5m": d5, "dcvd_15m": d15,
                    "rvol": rvol, "rvratio": rvratio, "px_vs_vwap_bps": px_vwap_bps,
                    "mom1_bps": mom1, "mom5_bps": mom5
                }
            }
        }

# -------------------------------
# LLM Analyst Agent (gated by micro + ensemble trend)
# -------------------------------
class LLMAnalystAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        poll = int(os.getenv("LLM_MIN_INTERVAL", "180"))
        super().__init__("llm_analyst", interval_sec or poll)
        self.enabled = os.getenv("LLM_ENABLE", "true").lower() == "true"
        self.model = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        self.max_tokens = int(os.getenv("LLM_MAX_INPUT_TOKENS", "4000"))
        self.disable_reason = None
        self._client = None

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key or not self.enabled:
            self.disable_reason = "no_api_key_or_disabled"
        else:
            try:
                from openai import OpenAI
                kwargs = {"api_key": api_key}
                base_url = os.getenv("OPENAI_BASE_URL", "").strip()
                if base_url:
                    kwargs["base_url"] = base_url
                use_proxy = os.getenv("LLM_USE_PROXY", "false").lower() == "true"
                ignore_proxy = os.getenv("LLM_IGNORE_PROXY", "true").lower() == "true"
                if use_proxy and not ignore_proxy:
                    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
                    if proxy_url:
                        import httpx as _httpx
                        kwargs["http_client"] = _httpx.Client(proxy=proxy_url, timeout=30.0)
                self._client = OpenAI(**kwargs)
            except Exception as e:
                self.disable_reason = f"init_error: {e}"

        self.alert_min_conf = float(os.getenv("LLM_ALERT_MIN_CONF", "0.60"))
        self._last_hash: Dict[str, str] = {}

    async def _gather_context(self, symbol: str) -> dict:
        sig = compute_signals(symbol)
        now = time.time()
        w2 = [r for r in trades[symbol] if r[0] >= now - 120]
        w5 = [r for r in trades[symbol] if r[0] >= now - 300]
        n_tr = len(w5)
        avg_sz = (sum(w[2] for w in w5) / n_tr) if n_tr else 0.0
        price0 = w5[0][1] if n_tr else None
        price1 = w5[-1][1] if n_tr else None
        price_bps = ((price1 - price0) / price0 * 1e4) if (price0 and price0 > 0) else 0.0

        # last 10 findings for symbol
        recent_findings: List[dict] = []
        if pg_conn:
            try:
                with pg_conn.cursor() as cur:
                    cur.execute("""
                        SELECT ts_utc, agent, score, label, details
                        FROM findings WHERE symbol=%s
                        ORDER BY ts_utc DESC LIMIT 20
                    """, (symbol,))
                    for ts, a, sc, lb, det in cur.fetchall():
                        recent_findings.append({"ts_utc": str(ts), "agent": a, "score": float(sc), "label": lb, "details": det})
            except Exception:
                pass

        d2 = _dcvd(w2)
        mom1_bps = _mom_bps(w2)

        # also include most recent trend ensemble if present
        p_up = None; p5=None; p15=None
        if pg_conn:
            try:
                with pg_conn.cursor() as cur:
                    cur.execute("""
                      SELECT details FROM findings
                      WHERE symbol=%s AND agent='trend_score'
                      ORDER BY ts_utc DESC LIMIT 1
                    """, (symbol,))
                    row = cur.fetchone()
                    if row and row[0]:
                        d = row[0]
                        p_up  = float(d.get("p_up")) if d.get("p_up") is not None else None
                        p5    = float(d.get("p_5m")) if d.get("p_5m") is not None else None
                        p15   = float(d.get("p_15m")) if d.get("p_15m") is not None else None
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
                "mom1_bps": mom1_bps,
                "mom5_bps": sig.get("mom5_bps"),
                "dcvd_2m": d2,
            },
            "recent_findings": recent_findings,
            "trend_snapshot": {"p_up": p_up, "p_5m": p5, "p_15m": p15},
        }

    def _prompt(self, ctx: dict) -> List[dict]:
        sys = (
            "You are the LLM Analyst Agent for a real-time trading system. "
            "Decide one of: observe | prepare | consider-entry | consider-exit. "
            "Prefer caution unless flow (dcvd_2m>0), momentum (mom1_bps>0), RVOL>1.2 and trend_snapshot.p_up>=0.55 "
            "with 5m/15m supportive. Output strict JSON with {action, confidence, rationale, tweaks}."
        )
        user = {"role": "user", "content": "Context:\n" + json.dumps(ctx, ensure_ascii=False)[: self.max_tokens * 3] + "\nReturn ONLY JSON."}
        return [{"role": "system", "content": sys}, user]

    async def run_once(self, symbol: str) -> Optional[dict]:
        if not self._client:
            return None
        ctx = await self._gather_context(symbol)
        msgs = self._prompt(ctx)

        raw = None
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._client.chat.completions.create(
                    model=self.model,
                    messages=msgs,
                    response_format={"type": "json_object"},
                    temperature=0.2, max_tokens=600,
                )
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)

            action = (data.get("action") or "observe").strip()
            try: confidence = float(data.get("confidence") or 0.0)
            except: confidence = 0.0
            rationale = str(data.get("rationale") or "").strip()
            tweaks = data.get("tweaks") or []

            # hard clamps using micro + ensemble trend
            d2 = ctx["signals"]["dcvd_2m"]
            mom1 = ctx["signals"]["mom1_bps"]
            rvol = (ctx["signals"]["rvol_vs_recent"] or 0.0) or 0.0
            trend = ctx["trend_snapshot"] or {}
            p_up  = trend.get("p_up"); p5 = trend.get("p_5m"); p15 = trend.get("p_15m")
            entry_min_rvol = float(os.getenv("LLM_ENTRY_MIN_RVOL", "1.20"))

            if action == "consider-entry":
                if (d2 < 0) or (mom1 <= -8) or (rvol < entry_min_rvol) or (p_up is not None and p_up < 0.55):
                    action = "observe"; confidence = min(confidence, 0.40)
                if (p5 is not None and p15 is not None) and (p5 < 0.55 and p15 < 0.55):
                    action = "observe"; confidence = min(confidence, 0.40)

            if action == "consider-exit":
                if (d2 > 0 and mom1 > 0 and rvol >= 1.0) and (p_up is not None and p_up > 0.60):
                    action = "observe"; confidence = min(confidence, 0.40)

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

            if ALERT_WEBHOOK_URL and not SLACK_ANALYSIS_ONLY and confidence >= self.alert_min_conf:
                if _should_post(self.name, symbol, finding["score"]):
                    await _post_webhook({"text": f"🧠 {symbol} {action} ({confidence:.2f}) — {rationale[:160]}"})

            return finding
        except Exception as e:
            det = {"error": f"{type(e).__name__}: {str(e)[:180]}"}
            if raw: det["raw"] = raw[:300]
            return {"score": 0.0, "label": "llm_error", "details": det}

# -------------------------------
# Posture Guard Agent (persistence + slow drawdown)
# -------------------------------
POSTURE_STATE: Dict[str, dict] = {}

class PostureGuardAgent(Agent):
    def __init__(self, interval_sec: int | None = None):
        super().__init__("posture_guard", interval_sec or POSTURE_GUARD_INTERVAL)
        self._last_score: dict[str, int] = {}
        self._persist: dict[str, dict] = {}

    async def run_once(self, symbol: str) -> Optional[dict]:
        st = POSTURE_STATE.get(symbol)
        if not st or st.get("state") != "long_bias":
            self._persist.pop(symbol, None)
            return None

        now = time.time()
        buf = trades[symbol]
        w2  = [r for r in buf if r[0] >= now - 120]
        w5  = [r for r in buf if r[0] >= now - 300]
        w15 = [r for r in buf if r[0] >= now - 900]

        d2, d5 = _dcvd(w2), _dcvd(w5)
        s15 = _dcvd(w15) / max(1, len(w15))

        up = sum((w[2] or 0.0) for w in w15 if (w[3] or "") == "buy")
        dn = sum((w[2] or 0.0) for w in w15 if (w[3] or "") == "sell")
        rvratio = (dn + 1e-9) / (up + 1e-9)
        push_rvol = up / max(1e-9, up + dn)

        mom5_bps = _mom_bps(w5)

        sig = compute_signals(symbol)
        px = sig.get("best_bid") or sig.get("best_ask")
        base_low = st.get("base_low", px)

        vwin_min = int(os.getenv("PG_VWAP_MINUTES", "20"))
        vwin = [r for r in buf if r[0] >= now - 60*vwin_min]
        vwap = None
        if vwin:
            num = sum((r[1] or 0.0)*(r[2] or 0.0) for r in vwin)
            den = sum((r[2] or 0.0) for r in vwin)
            vwap = (num/den) if den else None

        CVD5    = float(os.getenv("PG_CVD_5M_NEG", "1500"))
        CVD2    = float(os.getenv("PG_CVD_2M_NEG", "800"))
        RVR     = float(os.getenv("PG_RVOL_RATIO", "1.2"))
        PUSHMIN = float(os.getenv("PG_PUSH_RVOL_MIN", "0.7"))
        MOM5    = float(os.getenv("PG_MOM5_DOWN_BPS", "-8"))
        MAX_AGE = 60 * float(os.getenv("POSTURE_MAX_AGE_MIN", "30"))
        PERSIST_K = int(os.getenv("PG_PERSIST_K", "5"))
        DD_SLOW   = float(os.getenv("PG_DD_SLOW_BPS", "-60"))

        if px and px >= st.get("peak_price", px):
            st["peak_price"], st["peak_ts"] = px, now

        flow_flip = (d5 <= -CVD5) or (d2 <= -CVD2) or (s15 <= 0)
        rvol_flip = (rvratio >= RVR) or (push_rvol < PUSHMIN)
        structure_break = ((px is not None and base_low is not None and px < base_low) or
                           (vwap is not None and px is not None and px < vwap) or
                           (mom5_bps <= MOM5))
        time_in_state   = now - st.get("started_at", now)
        time_since_peak = now - st.get("peak_ts", st.get("started_at", now))
        aging = (time_in_state > MAX_AGE and time_since_peak > 300)

        pc = self._persist.setdefault(symbol, {"flow":0, "rvol":0, "structure":0})
        pc["flow"] = pc["flow"] + 1 if flow_flip else 0
        pc["rvol"] = pc["rvol"] + 1 if rvol_flip else 0
        pc["structure"] = pc["structure"] + 1 if structure_break else 0

        score = int(flow_flip) + int(rvol_flip) + int(structure_break) + int(aging)
        prev = self._last_score.get(symbol, 0)
        self._last_score[symbol] = score
        if score >= 2 and prev >= 2:
            POSTURE_STATE.pop(symbol, None)
            return {"score": 8.0, "label": "posture_exit",
                    "details": {"action":"consider-exit","confidence":0.8,
                                "reasons":["2-of-4 cues twice"],
                                "metrics":{"d2":d2,"d5":d5,"rvratio":rvratio,"mom5_bps":mom5_bps,"vwap":vwap,"px":px,"base_low":base_low}}}

        if pc["structure"] >= PERSIST_K or pc["flow"] >= PERSIST_K:
            POSTURE_STATE.pop(symbol, None)
            return {"score": 7.5, "label": "posture_exit",
                    "details": {"action":"consider-exit","confidence":0.75,
                                "reasons":[f"Persistent {'structure' if pc['structure']>=PERSIST_K else 'flow'} weakness"],
                                "metrics":{"mom5_bps":mom5_bps,"vwap":vwap,"px":px}}}

        dd_from_peak_bps = 0.0
        if px and st.get("peak_price"):
            dd_from_peak_bps = (px - st["peak_price"]) / st["peak_price"] * 1e4
        if dd_from_peak_bps <= DD_SLOW and ((vwap and px and px < vwap) or mom5_bps <= MOM5):
            POSTURE_STATE.pop(symbol, None)
            return {"score": 7.5, "label": "posture_exit",
                    "details": {"action":"consider-exit","confidence":0.75,
                                "reasons":[f"Slow drawdown {dd_from_peak_bps:.0f}bps with weak structure"],
                                "metrics":{"dd_from_peak_bps":dd_from_peak_bps,"vwap":vwap,"px":px,"mom5_bps":mom5_bps}}}
        return None

# -------------------------------
# Agents list & scheduler
# -------------------------------
AGENTS: List[Agent] = [
    RVOLSpikeAgent(),
    CvdDivergenceAgent(),
    TrendScoreAgent(),
    LLMAnalystAgent(),
    PostureGuardAgent(),
]

_last_run_ts: Dict[Tuple[str,str], float] = defaultdict(lambda: 0.0)

async def market_loop():
    """Stream Coinbase ticker -> fill `trades` and `best_px` with robust reconnect."""
    import json, websockets

    def _f(x):
        try: return float(x)
        except: return None

    while True:
        try:
            uri = "wss://ws-feed.exchange.coinbase.com"
            sub = {"type": "subscribe", "channels": [{"name": "ticker", "product_ids": SYMBOLS}]}
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(sub))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "ticker":
                        continue
                    sym = msg.get("product_id")
                    if sym not in trades:
                        continue

                    ts   = time.time()
                    px   = _f(msg.get("price")) or _f(msg.get("best_bid")) or _f(msg.get("best_ask"))
                    size = _f(msg.get("last_size")) or 0.0
                    side = msg.get("side")

                    if px:
                        trades[sym].append((ts, px, size, side))

                    bb, ba = _f(msg.get("best_bid")), _f(msg.get("best_ask"))
                    if bb or ba:
                        old_bb, old_ba = best_px.get(sym, (None, None))
                        best_px[sym] = (bb or old_bb, ba or old_ba)
        except Exception:
            await asyncio.sleep(2.0)

async def agents_loop():
    while True:
        try:
            if os.getenv("DATABASE_URL") and pg_conn is None:
                await pg_connect()

            now = time.time()

            # posture peak tracker
            for sym in SYMBOLS:
                st = POSTURE_STATE.get(sym)
                if st and st.get("state") == "long_bias":
                    sig = compute_signals(sym)
                    px = sig.get("best_ask") or sig.get("best_bid")
                    if px and px >= st.get("peak_price", px):
                        st["peak_price"] = px
                        st["peak_ts"] = now

            for agent in AGENTS:
                for sym in SYMBOLS:
                    key = (agent.name, sym)
                    if now - _last_run_ts[key] < max(1, int(getattr(agent, "interval_sec", 10))):
                        continue

                    try:
                        finding = await agent.run_once(sym)
                        _last_run_ts[key] = time.time()
                        pg_agent_heartbeat(agent.name, "ok")
                        if not finding:
                            continue

                        # Persist finding (with LLM floor)
                        if agent.name == "llm_analyst" and float(finding.get("score", 0.0)) < LLM_ANALYST_MIN_SCORE:
                            continue
                        pg_insert_finding(agent.name, sym, float(finding["score"]), finding["label"], finding.get("details") or {})

                        # --- TrendScore drives posture open/close ---
                        if agent.name == "trend_score":
                            p_up = float(finding["details"].get("p_up", 0.0))
                            ps = POSTURE_STATE.get(sym, {})
                            cnt = ps.get("_persist", 0)
                            if p_up >= TS_ENTRY:
                                cnt = max(1, cnt + 1) if ps.get("state") == "long_bias" else (cnt + 1)
                                if cnt >= TS_PERSIST and ps.get("state") != "long_bias":
                                    sig = compute_signals(sym)
                                    px = sig.get("best_ask") or sig.get("best_bid")
                                    now2 = time.time()
                                    w5 = [r for r in trades[sym] if r[0] >= now2 - 300]
                                    base_low = min((r[1] for r in w5), default=px or 0.0) or px
                                    POSTURE_STATE[sym] = {
                                        "state": "long_bias",
                                        "started_at": now2,
                                        "entry_price": px,
                                        "base_low": base_low,
                                        "peak_price": px,
                                        "peak_ts": now2,
                                        "_persist": 0,
                                    }
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps or {"_persist": cnt}
                            elif p_up <= TS_EXIT and ps.get("state") == "long_bias":
                                cnt = ps.get("_persist", 0) + 1
                                if cnt >= TS_PERSIST:
                                    POSTURE_STATE.pop(sym, None)
                                else:
                                    ps["_persist"] = cnt
                                    POSTURE_STATE[sym] = ps
                            else:
                                # decay persistence
                                if ps:
                                    ps["_persist"] = max(0, ps.get("_persist", 0) - 1)
                                    POSTURE_STATE[sym] = ps

                        # Optional Slack for raw agents if not analysis-only
                        if ALERT_WEBHOOK_URL and not SLACK_ANALYSIS_ONLY and ALERT_VERBOSE:
                            if _should_post(agent.name, sym, float(finding["score"])):
                                await _post_webhook({"text": f"{agent.name} {sym} → {finding['label']} {finding['score']:.1f}"})
                    except Exception:
                        pg_agent_heartbeat(agent.name, "error")
                        continue
        except Exception:
            pass

        await asyncio.sleep(5)

# -------------------------------
# FastAPI app & endpoints
# -------------------------------
app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the dashboard HTML from backend/templates/index.html"""
    here = os.path.dirname(__file__)
    index_path = os.path.join(here, "templates", "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception as e:
        return HTMLResponse(
            "<h3>Opportunity Radar (Alpha)</h3>"
            "<p>See <a href='/signals'>/signals</a>, "
            "<a href='/findings'>/findings</a>, "
            "<a href='/health'>/health</a></p>"
            f"<pre style='color:#b00'>index error: {e}</pre>"
        )

# --- startup tasks -----------------------------------------------------------
_feed_task = None
_agents_task = None

@app.on_event("startup")
async def _startup():
    global _feed_task, _agents_task
    if _feed_task is None or _feed_task.done():
        _feed_task = asyncio.create_task(market_loop(), name="market_loop")
    if _agents_task is None or _agents_task.done():
        _agents_task = asyncio.create_task(agents_loop(), name="agents_loop")

@app.post("/feed/restart")
async def feed_restart():
    global _feed_task
    try:
        if _feed_task and not _feed_task.done():
            _feed_task.cancel()
        _feed_task = asyncio.create_task(market_loop(), name="market_loop")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/signals")
async def signals(symbol: str = Query(default=SYMBOLS[0])):
    sig = compute_signals(symbol)
    sig["symbol"] = symbol
    return sig

@app.get("/findings")
async def findings(symbol: Optional[str] = None, limit: int = 50):
    if pg_conn is None:
        return {"findings": []}
    q = """
      SELECT ts_utc, agent, symbol, score, label, details
      FROM findings
    """
    params = []
    if symbol:
        q += " WHERE symbol=%s"
        params.append(symbol)
    q += " ORDER BY ts_utc DESC LIMIT %s"
    params.append(limit)
    out = []
    try:
        with pg_conn.cursor() as cur:
            cur.execute(q, params)
            for ts, a, sym, sc, lb, det in cur.fetchall():
                out.append({"ts_utc": str(ts), "agent": a, "symbol": sym, "score": float(sc), "label": lb, "details": det})
    except Exception:
        pass
    return {"findings": out}

@app.get("/agents")
async def agents():
    if pg_conn is None:
        return {"agents": []}
    out = []
    try:
        with pg_conn.cursor() as cur:
            cur.execute("""SELECT agent, MAX(ts_utc) FROM agent_runs GROUP BY agent""")
            for a, ts in cur.fetchall():
                out.append({"agent": a, "last_run": str(ts)})
    except Exception:
        pass
    return {"agents": out}

@app.post("/agents/run-now")
async def run_now(names: Optional[str] = None, agent: Optional[str] = None, symbol: str = Query(default=SYMBOLS[0]), insert: bool = True):
    lookup = {a.name: a for a in AGENTS}
    chosen = []
    if names:
        for n in [x.strip() for x in names.split(",")]:
            if n in lookup: chosen.append(lookup[n])
    elif agent and agent in lookup:
        chosen.append(lookup[agent])
    else:
        chosen = AGENTS

    results = []
    for a in chosen:
        try:
            f = await a.run_once(symbol)
            if f and insert:
                pg_insert_finding(a.name, symbol, float(f["score"]), f["label"], f.get("details") or {})
            results.append({"agent": a.name, "finding": f})
        except NotImplementedError as e:
            results.append({"agent": a.name, "error": f"NotImplementedError: {e}"})
        except Exception as e:
            results.append({"agent": a.name, "error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "ran": [a.name for a in chosen], "results": results}

@app.get("/health")
async def health():
    agents = {}
    if pg_conn:
        try:
            with pg_conn.cursor() as cur:
                cur.execute("""SELECT agent, MAX(ts_utc) FROM agent_runs GROUP BY agent""")
                for a, ts in cur.fetchall():
                    agents[a] = {"status": "ok", "last_run": str(ts)}
        except Exception:
            pass
    return {
        "status": "ok",
        "symbols": SYMBOLS,
        "trades_cached": {s: len(trades[s]) for s in SYMBOLS},
        "agents": agents
    }

@app.get("/db-health")
async def db_health():
    try:
        if pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT NOW()")
                cur.fetchone()
            return {"ok": True}
        return {"ok": False, "error": "no_connection"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/export-csv")
async def export_csv(symbol: str = Query(default=SYMBOLS[0]), limit: int = 500):
    if pg_conn is None:
        return PlainTextResponse("no db", status_code=503)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["ts_utc","agent","symbol","score","label","details"])
    try:
        with pg_conn.cursor() as cur:
            cur.execute("""
              SELECT ts_utc, agent, symbol, score, label, details
              FROM findings WHERE symbol=%s ORDER BY ts_utc DESC LIMIT %s
            """, (symbol, limit))
            for row in cur.fetchall():
                writer.writerow([row[0], row[1], row[2], row[3], row[4], json.dumps(row[5])])
    except Exception:
        pass
    out.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="findings_{symbol}.csv"'}
    return StreamingResponse(iter([out.read()]), media_type="text/csv", headers=headers)

@app.get("/test-alert")
async def test_alert():
    if not ALERT_WEBHOOK_URL:
        return {"ok": False, "error": "no webhook"}
    await _post_webhook({"text": "✅ Opportunity Radar webhook test"})
    return {"ok": True}

# --- LLM netcheck endpoints ---
@app.get("/llm/netcheck")
async def llm_netcheck():
    base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(base + "/models", headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY','')}" })
            snip = r.text[:300]
            return {"ok": r.status_code < 500, "status": r.status_code, "base": base, "snippet": snip}
    except Exception as e:
        return {"ok": False, "error": str(e), "base": base}

@app.post("/agents/test-llm")
async def test_llm(symbol: str = Query(default=SYMBOLS[0])):
    agent = next((a for a in AGENTS if a.name == "llm_analyst"), None)
    if not agent:
        return {"ok": False, "error": "llm agent missing"}
    f = await agent.run_once(symbol)
    return {"ok": True if f else False, "raw": json.dumps(f.get("details") if f else {})}
