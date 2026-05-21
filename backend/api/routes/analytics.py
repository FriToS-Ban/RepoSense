from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta

from backend.core.database import get_db
from backend.models.models import PullRequest, ReviewComment, User, Repository
from backend.api.deps import get_current_user

router = APIRouter()

@router.get("/overview")
def get_analytics_overview(days: int = 30, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    prs = db.query(PullRequest).join(Repository).filter(
        Repository.user_id == current_user.id,
        PullRequest.created_at >= cutoff,
        PullRequest.status == "reviewed"
    ).all()
    
    total_prs = len(prs)
    avg_score = sum(pr.quality_score for pr in prs if pr.quality_score is not None) / total_prs if total_prs > 0 else 100
    
    total_issues = db.query(func.count(ReviewComment.id)).join(PullRequest).join(Repository).filter(
        Repository.user_id == current_user.id,
        ReviewComment.created_at >= cutoff
    ).scalar()
    
    # Trends by date
    trends_dict = {}
    for pr in prs:
        date_str = pr.created_at.strftime("%Y-%m-%d")
        if date_str not in trends_dict:
            trends_dict[date_str] = {"sum": 0, "count": 0}
        if pr.quality_score is not None:
            trends_dict[date_str]["sum"] += pr.quality_score
            trends_dict[date_str]["count"] += 1
            
    trends = [{"date": d, "score": int(trends_dict[d]["sum"] / trends_dict[d]["count"])} for d in sorted(trends_dict.keys())]
    
    return {
        "total_prs": total_prs,
        "average_score": int(avg_score),
        "total_issues": total_issues or 0,
        "trends": trends
    }

@router.get("/categories")
def get_analytics_categories(days: int = 30, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    counts = db.query(ReviewComment.category, func.count(ReviewComment.id)).join(PullRequest).join(Repository).filter(
        Repository.user_id == current_user.id,
        ReviewComment.created_at >= cutoff
    ).group_by(ReviewComment.category).all()
    
    return [{"category": c.value if hasattr(c, 'value') else c, "count": count} for c, count in counts]
