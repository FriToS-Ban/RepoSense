from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.models.models import PullRequest, ReviewComment, User, Repository
from backend.api.deps import get_current_user
from backend.services.review import process_pr_review

router = APIRouter()

@router.get("/")
def get_prs(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    prs = db.query(PullRequest).join(Repository).filter(Repository.user_id == current_user.id).order_by(PullRequest.created_at.desc()).all()
    return [{
        "id": pr.id,
        "repo_full_name": pr.repository.repo_full_name,
        "pr_number": pr.github_pr_number,
        "title": pr.pr_title,
        "author": pr.pr_author,
        "status": pr.status,
        "quality_score": pr.quality_score,
        "created_at": pr.created_at
    } for pr in prs]

@router.get("/{pr_id}")
def get_pr_details(pr_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pr = db.query(PullRequest).join(Repository).filter(
        PullRequest.id == pr_id,
        Repository.user_id == current_user.id
    ).first()
    if not pr:
        return None
        
    return {
        "id": pr.id,
        "repo_full_name": pr.repository.repo_full_name,
        "pr_number": pr.github_pr_number,
        "title": pr.pr_title,
        "author": pr.pr_author,
        "status": pr.status,
        "quality_score": pr.quality_score,
        "created_at": pr.created_at
    }

@router.get("/{pr_id}/comments")
def get_pr_comments(pr_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Simple check for ownership
    pr = db.query(PullRequest).join(Repository).filter(PullRequest.id == pr_id, Repository.user_id == current_user.id).first()
    if not pr:
        return []
    
    comments = db.query(ReviewComment).filter(ReviewComment.pr_id == pr_id).all()
    return [{
        "id": c.id,
        "file_path": c.file_path,
        "line_number": c.line_number,
        "severity": c.severity.value if c.severity else "suggestion",
        "category": c.category.value if c.category else "logic",
        "comment_body": c.comment_body
    } for c in comments]

@router.post("/{pr_id}/retry")
def retry_pr_review(pr_id: str, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pr = db.query(PullRequest).join(Repository).filter(PullRequest.id == pr_id, Repository.user_id == current_user.id).first()
    if not pr:
        return {"message": "Not found"}
        
    background_tasks.add_task(
        process_pr_review,
        pr.repository.repo_full_name,
        pr.github_pr_number,
        pr.pr_title,
        pr.pr_author
    )
    return {"message": "Retry started"}
