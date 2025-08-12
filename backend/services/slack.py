
import time, httpx
from collections import defaultdict
from ..config import ALERT_WEBHOOK_URL, AGENT_ALERT_COOLDOWN_SEC

_last_post: dict[tuple, float] = defaultdict(float)

def should_post(agent: str, symbol: str) -> bool:
    now = time.time()
    key = (agent, symbol)
    if now - _last_post[key] < AGENT_ALERT_COOLDOWN_SEC:
        return False
    _last_post[key] = now
    return True

async def post_webhook(payload: dict | str):
    if not ALERT_WEBHOOK_URL:
        return
    p = {"text": payload} if isinstance(payload, str) else payload
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(ALERT_WEBHOOK_URL, json=p)
    except Exception:
        pass
