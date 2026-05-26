import enum
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Boolean, ForeignKey, DateTime, Enum as SAEnum, Text, Float
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
    access_token = Column(String)
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
    crawl_permission = Column(Boolean, default=False)  # NEW
    is_indexed = Column(Boolean, default=False)         # NEW
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    owner = relationship("User", back_populates="repositories")
    pull_requests = relationship("PullRequest", back_populates="repository")
    code_nodes = relationship("CodeNode", back_populates="repository")  # NEW

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

# NEW — Knowledge Graph Tables
class NodeType(str, enum.Enum):
    file = "file"
    function = "function"
    class_ = "class"

class CodeNode(Base):
    __tablename__ = "code_nodes"
    id = Column(String, primary_key=True, default=generate_uuid)
    repo_id = Column(String, ForeignKey("repositories.id"))
    node_type = Column(SAEnum(NodeType))
    name = Column(String)
    file_path = Column(String)
    content = Column(Text)
    pinecone_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    repository = relationship("Repository", back_populates="code_nodes")
    outgoing_edges = relationship("CodeEdge", foreign_keys="CodeEdge.source_id", back_populates="source")
    incoming_edges = relationship("CodeEdge", foreign_keys="CodeEdge.target_id", back_populates="target")

class EdgeType(str, enum.Enum):
    imports = "imports"
    calls = "calls"
    inherits = "inherits"
    defines = "defines"

class CodeEdge(Base):
    __tablename__ = "code_edges"
    id = Column(String, primary_key=True, default=generate_uuid)
    source_id = Column(String, ForeignKey("code_nodes.id"))
    target_id = Column(String, ForeignKey("code_nodes.id"))
    edge_type = Column(SAEnum(EdgeType))
    source = relationship("CodeNode", foreign_keys=[source_id], back_populates="outgoing_edges")
    target = relationship("CodeNode", foreign_keys=[target_id], back_populates="incoming_edges")