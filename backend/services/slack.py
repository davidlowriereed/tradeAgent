
from __future__ import annotations
import asyncio, json, aiohttp
from ..config import AGENT_ALERT_COOLDOWN_SEC

_last_post = {}

def should_post(agent: str, symbol: str) -> bool:
    import time
    now = time.time()
    key = (agent, symbol)
    last = _last_post.get(key, 0.0)
    if now - last >= AGENT_ALERT_COOLDOWN_SEC:
        _last_post[key] = now
        return True
    return False

async def post_webhook(payload: dict, url: str | None = None) -> None:
    if not url:
        return
    try:
        async with aiohttp.ClientSession() as sess:
            await sess.post(url, data=json.dumps(payload))
    except Exception:
        pass
