import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GEMINI_API_KEY: str = ""
    DATABASE_URL: str = "postgresql://user:password@localhost/reposense"
    ENVIRONMENT: str = "development"
    GEMINI_MODEL: str = "gemini-1.5-flash"
    
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7 # 7 days
    
    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = "http://localhost:8000"
    WEBHOOK_URL: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
