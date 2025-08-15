
# backend/services/slack.py
import json
from typing import Any, Dict, Optional
from ..config import ALERT_WEBHOOK_URL, ALERT_VERBOSE

# Try httpx first; fall back to stdlib so we don't block startup if httpx isn't present.
try:
    import httpx  # type: ignore
except Exception:
    httpx = None  # type: ignore

def should_post(agent_name: str, symbol: str) -> bool:
    # Simple throttle hook; keep verbose gating elsewhere.
    return bool(ALERT_WEBHOOK_URL) and bool(ALERT_VERBOSE)

async def post_webhook(payload: Dict[str, Any]) -> bool:
    """Send a JSON payload to Slack-compatible Incoming Webhook.
    Uses httpx if available; otherwise falls back to urllib (sync) in a thread.
    Always returns True/False, never raises to the scheduler loop.
    """
    url = ALERT_WEBHOOK_URL
    if not url:
        return False

    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}

    if httpx is not None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(url, content=body, headers=headers)
                return 200 <= resp.status_code < 300
        except Exception:
            return False

    # Fallback: stdlib (sync) executed in a thread to avoid blocking
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        def _send_sync():
            import urllib.request
            req = urllib.request.Request(url, data=body.encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                return 200 <= r.status < 300
        return await loop.run_in_executor(None, _send_sync)
    except Exception:
        return False
