from datetime import datetime
from backend.core.database import SessionLocal
from backend.models.models import PullRequest, Repository, User, ReviewComment, PRStatus
from backend.services.github import get_pr_diff, post_review_comments
from backend.services.llm import get_review_from_llm
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def calculate_quality_score(comments):
    score = 100
    for c in comments:
        severity = c.get("severity", "").lower()
        if severity == "critical":
            score -= 15
        elif severity == "warning":
            score -= 5
        elif severity == "suggestion":
            score -= 1
    return max(0, score)

def process_pr_review(repo_full_name: str, pr_number: int, pr_title: str, pr_author: str):
    db = SessionLocal()
    try:
        # Find repo in db
        repo = db.query(Repository).filter(Repository.repo_full_name == repo_full_name).first()
        if not repo:
            logger.error(f"Repository {repo_full_name} not found in DB")
            return
            
        user = db.query(User).filter(User.id == repo.user_id).first()
        if not user:
            logger.error("User not found")
            return

        # Find or create PR
        pr = db.query(PullRequest).filter(
            PullRequest.repo_id == repo.id,
            PullRequest.github_pr_number == pr_number
        ).first()
        
        if not pr:
            pr = PullRequest(
                repo_id=repo.id,
                github_pr_number=pr_number,
                pr_title=pr_title,
                pr_author=pr_author,
                status=PRStatus.pending
            )
            db.add(pr)
            db.commit()
            db.refresh(pr)
        else:
            pr.status = PRStatus.pending
            # Clear old comments if re-reviewing
            db.query(ReviewComment).filter(ReviewComment.pr_id == pr.id).delete()
            db.commit()

        # Fetch PR Diff
        try:
            diff = get_pr_diff(user.access_token, repo_full_name, pr_number)

            # Get review from LLM
            comments = get_review_from_llm(pr_title, pr_author, repo_full_name, diff)

            # Save comments to DB first
            for c in comments:
                db_comment = ReviewComment(
                    pr_id=pr.id,
                    file_path=c.get("file_path"),
                    line_number=c.get("line_number"),
                    severity=c.get("severity", "suggestion"),
                    category=c.get("category", "logic"),
                    comment_body=c.get("comment"),
                )
                db.add(db_comment)

            # Update PR status
            pr.quality_score = calculate_quality_score(comments)
            pr.status = PRStatus.reviewed
            pr.reviewed_at = datetime.now(timezone.utc)
            db.commit()

            # Post to GitHub after DB is safe
            if comments:
                try:
                    post_review_comments(user.access_token, repo_full_name, pr_number, comments)
                except Exception as e:
                    logger.error(f"Failed to post comments to GitHub: {str(e)}")

        except Exception as e:
            logger.error(f"Error processing PR {pr_number}: {str(e)}")
            pr.status = PRStatus.failed
            db.commit()
            
    finally:
        db.close()
