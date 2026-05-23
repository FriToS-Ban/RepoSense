from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.core.database import get_db
from backend.models.models import User, Repository
from backend.api.deps import get_current_user
from backend.services.github import list_user_repositories, create_webhook, delete_webhook

router = APIRouter()

class EnableRepoRequest(BaseModel):
    repo_full_name: str
    github_repo_id: str

@router.get("/")
def get_repos(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Fetch from github
    try:
        github_repos = list_user_repositories(current_user.access_token)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    # Get active repos from DB
    db_repos = {r.github_repo_id: r for r in db.query(Repository).filter(Repository.user_id == current_user.id).all()}
    
    # Merge
    result = []
    for r in github_repos:
        is_active = False
        if r["id"] in db_repos:
            is_active = db_repos[r["id"]].is_active
            
        result.append({
            "github_repo_id": r["id"],
            "repo_name": r["name"],
            "repo_full_name": r["full_name"],
            "is_active": is_active
        })
        
    return result

@router.post("/enable")
def enable_repo(req: EnableRepoRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    repo = db.query(Repository).filter(
        Repository.user_id == current_user.id,
        Repository.github_repo_id == req.github_repo_id
    ).first()
    
    if not repo:
        repo = Repository(
            user_id=current_user.id,
            github_repo_id=req.github_repo_id,
            repo_name=req.repo_full_name.split("/")[-1],
            repo_full_name=req.repo_full_name,
            is_active=False
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)
        
    if repo.is_active:
        return {"message": "Already active"}
        
    # Create webhook
    try:
        hook_id = create_webhook(current_user.access_token, repo.repo_full_name)
        repo.webhook_id = hook_id
        repo.is_active = True
        db.commit()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to create webhook: {str(e)}")
        
    return {"message": "Repo enabled successfully"}

@router.post("/disable")
def disable_repo(req: EnableRepoRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    repo = db.query(Repository).filter(
        Repository.user_id == current_user.id,
        Repository.github_repo_id == req.github_repo_id
    ).first()
    
    if not repo or not repo.is_active:
        return {"message": "Already disabled"}
        
    try:
        if repo.webhook_id:
            delete_webhook(current_user.access_token, repo.repo_full_name, repo.webhook_id)
        repo.is_active = False
        repo.webhook_id = None
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete webhook: {str(e)}")
        
    return {"message": "Repo disabled successfully"}
