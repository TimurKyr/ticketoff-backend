from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.identity.models import User
from app.modules.identity.schemas import TokenOut, UserOut, UserRegister
from app.modules.identity.security import create_access_token
from app.modules.identity.service import (
    EmailAlreadyRegisteredError,
    authenticate_user,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])

FormDep = Annotated[OAuth2PasswordRequestForm, Depends()]


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserRegister, session: SessionDep) -> User:
    try:
        return register_user(session, payload.email, payload.password)
    except EmailAlreadyRegisteredError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered") from None


@router.post("/login", response_model=TokenOut)
def login(form: FormDep, session: SessionDep) -> TokenOut:
    user = authenticate_user(session, form.username, form.password)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def read_me(user: CurrentUser) -> User:
    """Return the logged-in user. The frontend calls this to learn who it is."""
    return user
