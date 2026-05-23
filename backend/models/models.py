import enum
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Boolean, ForeignKey, DateTime, Enum as SAEnum, Text
from sqlalchemy.orm import relationship
import uuid

from backend.core.database import Base

def generate_uuid():
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    github_id = Column(String, unique=True, index=True)
    github_username = Column(String)
    access_token = Column(String)  # Note: should be encrypted in a real prod app
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    repositories = relationship("Repository", back_populates="owner")

class Repository(Base):
    __tablename__ = "repositories"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id"))
    github_repo_id = Column(String)
    repo_name = Column(String)
    repo_full_name = Column(String)
    webhook_id = Column(String, nullable=True)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    owner = relationship("User", back_populates="repositories")
    pull_requests = relationship("PullRequest", back_populates="repository")

class PRStatus(str, enum.Enum):
    pending = "pending"
    reviewed = "reviewed"
    failed = "failed"

class PullRequest(Base):
    __tablename__ = "pull_requests"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    repo_id = Column(String, ForeignKey("repositories.id"))
    github_pr_number = Column(Integer)
    pr_title = Column(String)
    pr_author = Column(String)
    status = Column(SAEnum(PRStatus), default=PRStatus.pending)
    quality_score = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    repository = relationship("Repository", back_populates="pull_requests")
    comments = relationship("ReviewComment", back_populates="pull_request")

class SeverityLevel(str, enum.Enum):
    critical = "critical"
    warning = "warning"
    suggestion = "suggestion"

class IssueCategory(str, enum.Enum):
    security = "security"
    performance = "performance"
    logic = "logic"
    style = "style"
    documentation = "documentation"

class ReviewComment(Base):
    __tablename__ = "review_comments"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    pr_id = Column(String, ForeignKey("pull_requests.id"))
    file_path = Column(String)
    line_number = Column(Integer)
    severity = Column(SAEnum(SeverityLevel))
    category = Column(SAEnum(IssueCategory))
    comment_body = Column(Text)
    github_comment_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    pull_request = relationship("PullRequest", back_populates="comments")
