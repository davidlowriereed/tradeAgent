
import json, asyncio
from typing import Optional, List, Dict, Any
import psycopg2
from .config import DATABASE_URL

pg_conn = None

def connect_sync():
    global pg_conn
    if not DATABASE_URL:
        return
    pg_conn = psycopg2.connect(DATABASE_URL)
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
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
          ts_utc TIMESTAMPTZ DEFAULT NOW(),
          agent TEXT,
          status TEXT
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_findings_symbol_ts ON findings(symbol, ts_utc DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_ts ON agent_runs(agent, ts_utc DESC);")

async def connect_async():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, connect_sync)

def insert_finding(agent: str, symbol: str, score: float, label: str, details: dict):
    if not pg_conn: return
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO findings(agent, symbol, score, label, details) VALUES (%s,%s,%s,%s,%s)",
            (agent, symbol, score, label, json.dumps(details)),
        )

def heartbeat(agent: str, status: str = "ok"):
    if not pg_conn: return
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs(agent, status) VALUES (%s,%s)", (agent, status))

def latest_trend_snapshot(symbol: str) -> dict | None:
    if not pg_conn: return None
    with pg_conn.cursor() as cur:
        cur.execute("""
          SELECT details FROM findings WHERE symbol=%s AND agent='trend_score'
          ORDER BY ts_utc DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        if not row: return None
        return row[0] or {}

def fetch_findings(symbol: str | None, limit: int) -> list[dict]:
    if not pg_conn: return []
    q = "SELECT ts_utc, agent, symbol, score, label, details FROM findings"
    params = []
    if symbol:
        q += " WHERE symbol=%s"
        params.append(symbol)
    q += " ORDER BY ts_utc DESC LIMIT %s"
    params.append(limit)
    out = []
    with pg_conn.cursor() as cur:
        cur.execute(q, params)
        for ts, a, sym, sc, lb, det in cur.fetchall():
            out.append({"ts_utc": str(ts), "agent": a, "symbol": sym, "score": float(sc), "label": lb, "details": det})
    return out

def list_agents_last_run() -> list[dict]:
    if not pg_conn: return []
    out = []
    with pg_conn.cursor() as cur:
        cur.execute("SELECT agent, MAX(ts_utc) FROM agent_runs GROUP BY agent")
        for a, ts in cur.fetchall():
            out.append({"agent": a, "last_run": str(ts)})
    return out
