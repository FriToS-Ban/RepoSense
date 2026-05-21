import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GEMINI_API_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./reposense.db"
    
    SECRET_KEY: str = "supersecretkey" # Replace in prod
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    
    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = "http://localhost:8000"

    class Config:
        env_file = ".env"

settings = Settings()
