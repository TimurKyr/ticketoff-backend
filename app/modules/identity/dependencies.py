from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import decode_access_token

SessionDep = Annotated[Session, Depends(get_session)]

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: SessionDep,
) -> User:
    """Turn the Bearer token into the logged-in User, or answer 401."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    user_id = decode_access_token(token)
    if user_id is None:
        raise credentials_error
    user = session.get(User, user_id)
    if user is None:
        raise credentials_error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_platform_admin(user: CurrentUser) -> User:
    if user.system_role != SystemRole.PLATFORM_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Platform admin only")
    return user


def require_organizer(user: CurrentUser, session: SessionDep) -> OrganizerProfile:
    """An organizer is any user that has a row in organizer_profiles."""
    profile = session.scalar(select(OrganizerProfile).where(OrganizerProfile.user_id == user.id))
    if profile is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Organizer account required")
    return profile
