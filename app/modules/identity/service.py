from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.identity.models import User
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
