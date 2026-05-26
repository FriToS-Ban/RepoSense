from fastapi import APIRouter, Depends, Request, Response, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import httpx
from urllib.parse import urlencode

from backend.core.config import settings
from backend.core.database import get_db
from backend.core.security import create_access_token
from backend.models.models import User
from backend.api.deps import get_current_user

router = APIRouter()

@router.get("/github")
def login_github():
    if not settings.GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub Client ID not configured")
    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": f"{settings.BACKEND_URL}/api/auth/callback",
        "scope": "repo pull_requests write:discussion",
    }
    url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
    return RedirectResponse(url)

@router.get("/callback")
async def auth_callback(code: str, response: Response, db: Session = Depends(get_db)):
    # Exchange code for token
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{settings.BACKEND_URL}/api/auth/callback",
            },
        )
        token_data = token_res.json()
        
        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data["error_description"])
            
        access_token = token_data["access_token"]
        
        # Get user info
        user_res = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        user_data = user_res.json()
        github_id = str(user_data["id"])
        github_username = user_data["login"]

    # Save to db
    user = db.query(User).filter(User.github_id == github_id).first()
    if not user:
        user = User(github_id=github_id, github_username=github_username, access_token=access_token)
        db.add(user)
    else:
        user.github_username = github_username
        user.access_token = access_token
    
    db.commit()
    db.refresh(user)

    # Create JWT
    jwt_token = create_access_token(data={"sub": user.id})

    # Redirect to frontend with cookie
    redirect = RedirectResponse(url=f"{settings.FRONTEND_URL}/dashboard")
    redirect.set_cookie(
        key="access_token",
        value=jwt_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="none",
        secure=True
    )
    return redirect

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "github_id": current_user.github_id,
        "github_username": current_user.github_username,
    }

@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key="access_token")
    return {"message": "Logged out successfully"}
