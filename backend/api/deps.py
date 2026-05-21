from fastapi import Depends, Request, HTTPException, status
from sqlalchemy.orm import Session
from backend.core.database import get_db
from backend.core.security import get_current_user_id_from_cookie
from backend.models.models import User

def get_current_user(request: Request, db: Session = Depends(get_db)):
    user_id = get_current_user_id_from_cookie(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user
