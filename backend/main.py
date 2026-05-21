from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.core.config import settings
from backend.core.database import Base, engine
import backend.models.models  # to ensure models are imported before create_all

from backend.api.routes import auth, repos, webhooks, prs, analytics

# Create tables
Base.metadata.create_all(bind=engine)

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

@app.get("/")
def read_root():
    return {"message": "Welcome to RepoSense API"}
