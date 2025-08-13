# backend/github_webhook.py
import hmac, hashlib, os
from fastapi import APIRouter, Header, HTTPException, Request

router = APIRouter()

SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

def _verify(sig_header: str | None, body: bytes) -> None:
    if not SECRET:
        return  # if you prefer to run without verification in dev
    if not sig_header or not sig_header.startswith("sha256="):
        raise HTTPException(status_code=400, detail="bad signature header")
    sent = sig_header.split("=", 1)[1]
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, sent):
        raise HTTPException(status_code=401, detail="signature mismatch")

@router.post("/github-webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
):
    body = await request.body()
    _verify(x_hub_signature_256, body)
    payload = await request.json()

    # Example: react to push to main
    if x_github_event == "push" and payload.get("ref","").endswith("/main"):
        # TODO: kick your CI task, refresh UI badge, enqueue an “update” job, etc.
        # e.g., write a small file, or put a row into DB that the dashboard reads
        # or call your agents to refresh templates/config based on repo files
        pass

    return {"ok": True}
