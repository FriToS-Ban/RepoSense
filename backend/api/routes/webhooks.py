import hmac
import hashlib
from fastapi import APIRouter, Request, HTTPException, Header, BackgroundTasks
from backend.core.config import settings
from backend.services.review import process_pr_review
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

async def verify_signature(request: Request, signature: str):
    body = await request.body()
    secret = settings.GITHUB_WEBHOOK_SECRET.encode()
    mac = hmac.new(secret, msg=body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + mac.hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(None),
    x_github_event: str = Header(None)
):
    if not settings.GITHUB_WEBHOOK_SECRET:
        # For local dev without secret, just accept if secret not configured
        pass
    else:
        if not x_hub_signature_256:
            raise HTTPException(status_code=403, detail="Missing signature")
        await verify_signature(request, x_hub_signature_256)

    payload = await request.json()

    if x_github_event == "pull_request":
        action = payload.get("action")
        if action in ["opened", "synchronize"]:
            pr = payload.get("pull_request", {})
            repo = payload.get("repository", {})
            
            pr_number = pr.get("number")
            pr_title = pr.get("title")
            pr_author = pr.get("user", {}).get("login")
            repo_full_name = repo.get("full_name")
            
            # Fire background task
            background_tasks.add_task(
                process_pr_review,
                repo_full_name,
                pr_number,
                pr_title,
                pr_author
            )
            
    return {"status": "ok"}
