from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.identity import service
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.schemas import (
    MeOut,
    OrganizerProfileIn,
    OrganizerProfileOut,
    TokenOut,
    UserOut,
    UserRegister,
)
from app.modules.identity.security import create_access_token
from app.modules.identity.service import (
    EmailAlreadyRegisteredError,
    authenticate_user,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])
organizer_profile_router = APIRouter(prefix="/organizer/profile", tags=["organizer-profile"])

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


@router.get("/me", response_model=MeOut)
def read_me(user: CurrentUser, session: SessionDep) -> MeOut:
    """Return the logged-in user and their roles. Web and mobile call this to learn who it is."""
    return service.get_me(session, user)


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@organizer_profile_router.post(
    "",
    response_model=OrganizerProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Become an organizer",
)
def create_organizer_profile(
    payload: OrganizerProfileIn, user: CurrentUser, session: SessionDep
) -> OrganizerProfile:
    with _http_errors():
        return service.create_organizer_profile(session, user, payload.display_name)


@organizer_profile_router.get(
    "", response_model=OrganizerProfileOut, summary="My organizer profile"
)
def read_organizer_profile(user: CurrentUser, session: SessionDep) -> OrganizerProfile:
    with _http_errors():
        return service.get_organizer_profile(session, user)


@organizer_profile_router.patch(
    "", response_model=OrganizerProfileOut, summary="Edit my organizer profile"
)
def update_organizer_profile(
    payload: OrganizerProfileIn, user: CurrentUser, session: SessionDep
) -> OrganizerProfile:
    with _http_errors():
        return service.update_organizer_profile(session, user, payload.display_name)
