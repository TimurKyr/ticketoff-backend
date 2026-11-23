import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import (
    BusinessRuleError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
)
from app.modules.identity import repository
from app.modules.identity.enums import Language
from app.modules.identity.models import OrganizerProfile, StaffAssignment, User
from app.modules.identity.schemas import (
    MeOut,
    OrganizerProfileIn,
    OrganizerProfileOut,
    OrganizerProfileUpdate,
    UserOut,
)
from app.modules.identity.security import (
    EMAIL_VERIFICATION_TTL,
    PASSWORD_RESET_TTL,
    create_email_verification_token,
    create_password_reset_token,
    decode_email_verification_token,
    decode_password_reset_token,
    hash_password,
    password_matches_reset_token,
    verify_password,
)
from app.modules.notifications import links
from app.modules.notifications import service as notifications
from app.modules.ops.enums import NotificationType

# One message for every bad link, so a caller learns nothing about why it failed.
INVALID_VERIFICATION_LINK = "Invalid or expired verification link"
# A user may ask for a new verification email at most this often (registration counts).
VERIFICATION_RESEND_COOLDOWN = timedelta(seconds=60)

# Same idea for password reset: one message for every bad link.
INVALID_RESET_LINK = "Invalid or expired password reset link"
# A password reset email goes to one address at most this often. Anyone can ask for it, so the
# limit is what stops the endpoint from being used to flood somebody's inbox.
PASSWORD_RESET_COOLDOWN = timedelta(seconds=60)

# Hash of a throwaway password. Checked when the email is unknown so that
# "unknown email" and "wrong password" take the same time (no user enumeration).
_DUMMY_HASH = hash_password("not-a-real-password")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def register_user(
    session: Session, email: str, password: str, language: Language = Language.RU
) -> User:
    user = User(
        email=normalize_email(email), password_hash=hash_password(password), language=language
    )
    session.add(user)
    try:
        session.flush()  # the id goes into the verification link
    except IntegrityError as exc:  # unique index on users.email
        session.rollback()
        raise BusinessRuleError("Email already registered") from exc
    # A brand-new row: nobody else can hold it yet, so no lock is needed for the clock.
    send_verification_email(session, user, datetime.now(UTC))
    session.commit()  # the user; the verification email goes out after this
    return user


def send_verification_email(session: Session, user: User, now: datetime) -> None:
    """Queue an account_verification email (email only, no in-app row) for after the
    commit, and record when (the resend cooldown counts from it). The token is only in the
    email body, which is never logged."""
    user.email_verification_sent_at = now
    token = create_email_verification_token(user.id, user.email)
    context = {
        "verify_url": links.verify_email_url(token),
        "expires_hours": int(EMAIL_VERIFICATION_TTL.total_seconds() // 3600),
    }
    notifications.notify(
        session, user, NotificationType.ACCOUNT_VERIFICATION, context, in_app=False
    )


def verify_email(session: Session, token: str) -> User:
    """Mark the token's user verified. Idempotent: an already verified user gets the same
    answer. Expired, tampered, wrong-kind tokens, an unknown user, or a user whose email
    is no longer the one in the token all get the same InvalidInputError."""
    decoded = decode_email_verification_token(token)
    user = session.get(User, decoded[0]) if decoded else None
    if user is None or user.email != decoded[1]:
        raise InvalidInputError(INVALID_VERIFICATION_LINK)
    if not user.email_verified:
        user.email_verified = True
        session.commit()
    return user


def resend_verification_email(session: Session, user: User) -> None:
    """A fresh link for the logged-in user, at most once per VERIFICATION_RESEND_COOLDOWN.

    The user row is locked (FOR UPDATE) before the clock is read, so two resends at the same
    moment run one after the other and the second one is refused (RateLimitedError -> 429
    with Retry-After).
    """
    user = repository.lock_user(session, user.id)
    if user.email_verified:
        raise BusinessRuleError("Email is already verified")
    now = datetime.now(UTC)
    if user.email_verification_sent_at is not None:
        wait = user.email_verification_sent_at + VERIFICATION_RESEND_COOLDOWN - now
        if wait > timedelta(0):
            raise RateLimitedError(
                "A verification email was sent recently; try again later",
                retry_after=math.ceil(wait.total_seconds()),
            )
    send_verification_email(session, user, now)
    session.commit()  # the timestamp; the queued email goes out after this


def request_password_reset(session: Session, email: str) -> None:
    """Email a reset link to the account with this address, if there is one.

    Always returns normally, so the answer never tells a stranger whether an address has an
    account: unknown email, suspended account and "asked too recently" all do nothing. The row
    is locked (FOR UPDATE) before the clock is read, like the verification resend.
    """
    found = session.scalar(select(User.id).where(User.email == normalize_email(email)))
    user = repository.lock_user(session, found) if found else None
    if user is None or user.is_suspended:
        session.rollback()
        return
    now = datetime.now(UTC)
    last = user.password_reset_sent_at
    if last is not None and last + PASSWORD_RESET_COOLDOWN > now:
        session.rollback()
        return
    user.password_reset_sent_at = now
    token = create_password_reset_token(user.id, user.password_hash)
    context = {
        "reset_url": links.password_reset_url(token),
        "expires_hours": int(PASSWORD_RESET_TTL.total_seconds() // 3600),
    }
    notifications.notify(session, user, NotificationType.PASSWORD_RESET, context, in_app=False)
    session.commit()  # the timestamp; the queued email goes out after this


def reset_password(session: Session, token: str, new_password: str) -> None:
    """Set a new password from a reset link.

    The token carries a fingerprint of the password it was issued for, so it works once: after
    the change the fingerprint no longer matches. Expired, tampered, wrong-kind, used, unknown
    user and suspended user all get the same InvalidInputError.
    """
    decoded = decode_password_reset_token(token)
    user = repository.lock_user(session, decoded[0]) if decoded else None
    if (
        user is None
        or user.is_suspended
        or not password_matches_reset_token(user.password_hash, decoded[1])
    ):
        session.rollback()
        raise InvalidInputError(INVALID_RESET_LINK)
    user.password_hash = hash_password(new_password)
    session.commit()


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


def _profile_out(session: Session, profile: OrganizerProfile) -> OrganizerProfileOut:
    return OrganizerProfileOut(
        id=profile.id,
        display_name=profile.display_name,
        contact_email=profile.contact_email,
        contact_phone=profile.contact_phone,
        website=profile.website,
        verification_status=profile.verification_status,
        payout_status=repository.payout_status(session, profile.id),
    )


def _own_profile(session: Session, user: User) -> OrganizerProfile:
    profile = find_organizer_profile(session, user)
    if profile is None:
        raise NotFoundError("Organizer profile not found")
    return profile


def get_organizer_profile(session: Session, user: User) -> OrganizerProfileOut:
    return _profile_out(session, _own_profile(session, user))


def create_organizer_profile(
    session: Session, user: User, data: OrganizerProfileIn
) -> OrganizerProfileOut:
    if find_organizer_profile(session, user) is not None:
        raise BusinessRuleError("Organizer profile already exists")
    profile = OrganizerProfile(user_id=user.id, **data.model_dump())
    session.add(profile)
    try:
        session.commit()
    except IntegrityError as exc:  # unique user_id: two requests at the same time
        session.rollback()
        raise BusinessRuleError("Organizer profile already exists") from exc
    return _profile_out(session, profile)


def update_organizer_profile(
    session: Session, user: User, data: OrganizerProfileUpdate
) -> OrganizerProfileOut:
    """Only the fields that were sent change; an explicit null clears a contact. Always the
    caller's own profile: there is no profile id in the path, so nobody edits another's."""
    profile = _own_profile(session, user)
    for name, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, name, value)
    session.commit()
    return _profile_out(session, profile)


def get_me(session: Session, user: User) -> MeOut:
    profile = find_organizer_profile(session, user)
    staff_event_ids = session.scalars(
        select(StaffAssignment.event_id)
        .where(StaffAssignment.user_id == user.id)
        .distinct()
        .order_by(StaffAssignment.event_id)
    ).all()
    return MeOut(
        **UserOut.model_validate(user).model_dump(),
        is_organizer=profile is not None,
        organizer_profile_id=profile.id if profile else None,
        staff_event_ids=list(staff_event_ids),
    )
