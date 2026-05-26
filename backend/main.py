from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.core.config import settings
from backend.core.database import Base, engine
import backend.models.models  # to ensure models are imported before create_all

from backend.api.routes import auth, repos, webhooks, prs, analytics

app = FastAPI(title="RepoSense API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(repos.router, prefix="/api/repos", tags=["Repos"])
app.include_router(prs.router, prefix="/api/prs", tags=["PRs"])
app.include_router(webhooks.router, prefix="/api/webhook", tags=["Webhooks"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"])

@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text('ALTER TABLE repositories ADD COLUMN IF NOT EXISTS crawl_permission BOOLEAN DEFAULT FALSE'))
        conn.execute(text('ALTER TABLE repositories ADD COLUMN IF NOT EXISTS is_indexed BOOLEAN DEFAULT FALSE'))
        conn.execute(text('''CREATE TABLE IF NOT EXISTS code_nodes (
            id VARCHAR PRIMARY KEY,
            repo_id VARCHAR REFERENCES repositories(id),
            node_type VARCHAR,
            name VARCHAR,
            file_path VARCHAR,
            content TEXT,
            pinecone_id VARCHAR,
            created_at TIMESTAMP
        )'''))
        conn.execute(text('''CREATE TABLE IF NOT EXISTS code_edges (
            id VARCHAR PRIMARY KEY,
            source_id VARCHAR REFERENCES code_nodes(id),
            target_id VARCHAR REFERENCES code_nodes(id),
            edge_type VARCHAR
        )'''))
        conn.commit()

@app.get("/")
def read_root():
    return {"message": "Welcome to RepoSense API"}
