from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.modules.identity import service
from app.modules.identity.dependencies import ACCOUNT_SUSPENDED, CurrentUser, SessionDep
from app.modules.identity.models import User
from app.modules.identity.schemas import (
    MeOut,
    OrganizerProfileIn,
    OrganizerProfileOut,
    OrganizerProfileUpdate,
    TokenOut,
    UserOut,
    UserRegister,
    VerifyEmailIn,
)
from app.modules.identity.security import create_access_token
from app.modules.identity.service import (
    authenticate_user,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])
organizer_profile_router = APIRouter(prefix="/organizer/profile", tags=["organizer-profile"])

FormDep = Annotated[OAuth2PasswordRequestForm, Depends()]


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserRegister, session: SessionDep) -> User:
    return register_user(session, payload.email, payload.password, payload.language)


@router.post("/verify-email", response_model=UserOut, summary="Confirm my email from the link")
def verify_email(payload: VerifyEmailIn, session: SessionDep) -> User:
    """No login needed: the link's token says who it is. Calling it again is fine."""
    return service.verify_email(session, payload.token)


@router.post(
    "/verify-email/resend",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Send me a new verification link",
)
def resend_verification_email(user: CurrentUser, session: SessionDep) -> None:
    service.resend_verification_email(session, user)


@router.post("/login", response_model=TokenOut)
def login(form: FormDep, session: SessionDep) -> TokenOut:
    user = authenticate_user(session, form.username, form.password)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Only after the password matched, so a wrong password never reveals the suspension.
    if user.is_suspended:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ACCOUNT_SUSPENDED)
    return TokenOut(access_token=create_access_token(user.id))


@router.get("/me", response_model=MeOut)
def read_me(user: CurrentUser, session: SessionDep) -> MeOut:
    """Return the logged-in user and their roles. Web and mobile call this to learn who it is."""
    return service.get_me(session, user)


@organizer_profile_router.post(
    "",
    response_model=OrganizerProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Become an organizer",
)
def create_organizer_profile(
    payload: OrganizerProfileIn, user: CurrentUser, session: SessionDep
) -> OrganizerProfileOut:
    return service.create_organizer_profile(session, user, payload)


@organizer_profile_router.get(
    "", response_model=OrganizerProfileOut, summary="My organizer profile"
)
def read_organizer_profile(user: CurrentUser, session: SessionDep) -> OrganizerProfileOut:
    return service.get_organizer_profile(session, user)


@organizer_profile_router.patch(
    "", response_model=OrganizerProfileOut, summary="Edit my organizer profile"
)
def update_organizer_profile(
    payload: OrganizerProfileUpdate, user: CurrentUser, session: SessionDep
) -> OrganizerProfileOut:
    """Partial update of my profile: send only what changes (null clears a contact)."""
    return service.update_organizer_profile(session, user, payload)
