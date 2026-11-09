from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.modules.identity.models import OrganizerProfile, StaffAssignment, User
from app.modules.identity.schemas import MeOut, UserOut
from app.modules.identity.security import hash_password, verify_password

# Hash of a throwaway password. Checked when the email is unknown so that
# "unknown email" and "wrong password" take the same time (no user enumeration).
_DUMMY_HASH = hash_password("not-a-real-password")


class EmailAlreadyRegisteredError(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


def register_user(session: Session, email: str, password: str) -> User:
    user = User(email=normalize_email(email), password_hash=hash_password(password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:  # unique index on users.email
        session.rollback()
        raise EmailAlreadyRegisteredError from exc
    return user


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def find_organizer_profile(session: Session, user: User) -> OrganizerProfile | None:
    return session.scalar(select(OrganizerProfile).where(OrganizerProfile.user_id == user.id))


def get_organizer_profile(session: Session, user: User) -> OrganizerProfile:
    profile = find_organizer_profile(session, user)
    if profile is None:
        raise NotFoundError("Organizer profile not found")
    return profile


def create_organizer_profile(session: Session, user: User, display_name: str) -> OrganizerProfile:
    if find_organizer_profile(session, user) is not None:
        raise BusinessRuleError("Organizer profile already exists")
    profile = OrganizerProfile(user_id=user.id, display_name=display_name)
    session.add(profile)
    try:
        session.commit()
    except IntegrityError as exc:  # unique user_id: two requests at the same time
        session.rollback()
        raise BusinessRuleError("Organizer profile already exists") from exc
    return profile


def update_organizer_profile(session: Session, user: User, display_name: str) -> OrganizerProfile:
    profile = get_organizer_profile(session, user)
    profile.display_name = display_name
    session.commit()
    return profile


def get_me(session: Session, user: User) -> MeOut:
    profile = find_organizer_profile(session, user)
    staff_event_ids = session.scalars(
        select(StaffAssignment.event_id).where(StaffAssignment.user_id == user.id)
    ).all()
    return MeOut(
        **UserOut.model_validate(user).model_dump(),
        is_organizer=profile is not None,
        organizer_profile_id=profile.id if profile else None,
        staff_event_ids=list(staff_event_ids),
    )
