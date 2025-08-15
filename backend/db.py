
import asyncio
from typing import Optional, Any, Dict
from .config import DATABASE_URL
from .state import RECENT_FINDINGS

pg_conn: Optional[Any] = None

async def connect_async():
    global pg_conn
    if pg_conn is not None:
        return pg_conn
    try:
        import asyncpg
        if DATABASE_URL:
            pg_conn = await asyncpg.connect(DATABASE_URL)
            return pg_conn
    except Exception:
        pg_conn = None
    return None

def heartbeat(agent: str, status: str) -> None:
    # Optional: write to DB table; no-op if no DB
    pass

def insert_finding(agent: str, symbol: str, score: float, label: str, details: Dict) -> None:
    if pg_conn is None:
        RECENT_FINDINGS.append({
            "agent": agent, "symbol": symbol, "score": score, "label": label, "details": details
        })
        return
    try:
        # This table/SQL is illustrative; adjust to your schema.
        import json, asyncio
        async def _ins():
            await pg_conn.execute(
                "INSERT INTO findings(ts_utc, agent, symbol, score, label, details) VALUES (NOW(), $1,$2,$3,$4,$5)",
                agent, symbol, score, label, json.dumps(details)
            )
        asyncio.get_event_loop().create_task(_ins())
    except Exception:
        RECENT_FINDINGS.append({
            "agent": agent, "symbol": symbol, "score": score, "label": label, "details": details
        })

def pg_fetchone(sql: str, params: tuple) -> Optional[dict]:
    # Stub: return None to indicate no DB row
    return None

def pg_exec(sql: str, params: tuple) -> None:
    # Stub: no-op
    pass
