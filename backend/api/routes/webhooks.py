import hmac
import hashlib
from fastapi import APIRouter, Request, HTTPException, Header, BackgroundTasks
from backend.core.config import settings
from backend.services.review import process_pr_review
import logging
import json

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(None),
    x_github_event: str = Header(None)
):
    body = await request.body()
    
    if settings.GITHUB_WEBHOOK_SECRET:
        if not x_hub_signature_256:
            raise HTTPException(status_code=403, detail="Missing signature")
        secret = settings.GITHUB_WEBHOOK_SECRET.encode()
        mac = hmac.new(secret, msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + mac.hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=403, detail="Invalid signature")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — accepting all webhook requests")

    payload = json.loads(body)

    if x_github_event == "pull_request":
        action = payload.get("action")
        if action in ["opened", "synchronize"]:
            pr = payload.get("pull_request", {})
            repo = payload.get("repository", {})
            
            pr_number = pr.get("number")
            pr_title = pr.get("title")
            pr_author = pr.get("user", {}).get("login")
            repo_full_name = repo.get("full_name")
            
            background_tasks.add_task(
                process_pr_review,
                repo_full_name,
                pr_number,
                pr_title,
                pr_author
            )

    return {"status": "ok"}
