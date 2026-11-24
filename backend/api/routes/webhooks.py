import hmac
import hashlib
from fastapi import APIRouter, Request, HTTPException, Header, BackgroundTasks
from backend.core.config import settings
from backend.services.review import process_pr_review
import logging
import json

router = APIRouter()
logger = logging.getLogger(__name__)


def kg_push_target(payload: dict):
    """(repo_full_name, before, after) if a push should update the v2 knowledge graph, else None.

    The graph represents the default branch: pushes to other branches, branch deletions
    (after = 0000…) and pushes without a 'before' are ignored."""
    repo = payload.get("repository", {}) or {}
    before, after = payload.get("before") or "", payload.get("after") or ""
    branch = repo.get("default_branch")
    if not repo.get("full_name") or not branch or payload.get("ref") != f"refs/heads/{branch}":
        return None
    if not after or set(after) == {"0"} or not before:
        return None
    return repo["full_name"], before, after


def kg_update_from_push(payload: dict) -> bool:
    target = kg_push_target(payload)
    if target is None:
        return False
    repo_full_name, before, after = target
    from backend.core.database import SessionLocal
    from backend.models.models import Repository, User

    db = SessionLocal()
    try:
        db_repo = db.query(Repository).filter(
            Repository.repo_full_name == repo_full_name,
            Repository.is_active == True,  # noqa: E712
            Repository.crawl_permission == True,  # noqa: E712
        ).first()
        user = db.query(User).filter(User.id == db_repo.user_id).first() if db_repo else None
        if not user:
            return False
        from backend.services.kg_indexer import update_on_push_background

        update_on_push_background(db_repo.id, user.access_token, repo_full_name, before, after)
        logger.info(f"Triggered knowledge-graph update {before[:10]}..{after[:10]} for {repo_full_name}")
        return True
    finally:
        db.close()

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

    # ─── PR review ────────────────────────────────────────────────────────────
    if x_github_event == "pull_request":
        action = payload.get("action")
        if action in ["opened", "synchronize"]:
            pr = payload.get("pull_request", {})
            repo = payload.get("repository", {})
            background_tasks.add_task(
                process_pr_review,
                repo.get("full_name"),
                pr.get("number"),
                pr.get("title"),
                pr.get("user", {}).get("login")
            )

    # ─── Improvement 4: Re-index on push ──────────────────────────────────────
    elif x_github_event == "push":
        repo = payload.get("repository", {})
        repo_full_name = repo.get("full_name")
        commits = payload.get("commits", [])

        if getattr(settings, "RETRIEVAL_ENGINE", "v1") == "v2":
            kg_update_from_push(payload)
            return {"status": "ok"}

        # Collect all unique changed/added files across commits
        changed_paths = set()
        for commit in commits:
            changed_paths.update(commit.get("added", []))
            changed_paths.update(commit.get("modified", []))

        # Filter to only code files
        code_extensions = (".py", ".js", ".ts", ".jsx", ".tsx")
        changed_paths = [p for p in changed_paths if p.endswith(code_extensions)]

        if changed_paths and repo_full_name:
            from backend.core.database import SessionLocal
            from backend.models.models import Repository, User
            db = SessionLocal()
            try:
                db_repo = db.query(Repository).filter(
                    Repository.repo_full_name == repo_full_name,
                    Repository.is_active == True,
                    Repository.crawl_permission == True,
                    Repository.is_indexed == True
                ).first()
                if db_repo:
                    user = db.query(User).filter(User.id == db_repo.user_id).first()
                    if user:
                        from backend.services.indexer import reindex_changed_files_background
                        reindex_changed_files_background(db_repo.id, user.access_token, changed_paths)
                        logger.info(f"Triggered re-index for {len(changed_paths)} files in {repo_full_name}")
            finally:
                db.close()

    return {"status": "ok"}