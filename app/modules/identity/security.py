import uuid
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

# argon2 is the current recommended password hash. It is slow on purpose,
# so a stolen database cannot be brute-forced quickly.
password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


# Every JWT we sign says what it is for, and each decoder accepts only its own kind: an email
# verification link must never work as a login, nor a login token as a verification link.
ACCESS_TOKEN_TYPE = "access"
EMAIL_VERIFICATION_TOKEN_TYPE = "email_verification"
EMAIL_VERIFICATION_TTL = timedelta(hours=24)


def _encode(claims: dict, expires_at: datetime) -> str:
    settings = get_settings()
    payload = {**claims, "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _decode(token: str, token_type: str) -> dict | None:
    """The claims of a valid, unexpired token of `token_type`, or None for anything else."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.InvalidTokenError:
        return None
    return claims if claims["typ"] == token_type else None


def create_access_token(user_id: uuid.UUID) -> str:
    minutes = get_settings().access_token_expire_minutes
    return _encode(
        {"sub": str(user_id), "typ": ACCESS_TOKEN_TYPE},
        datetime.now(UTC) + timedelta(minutes=minutes),
    )


def decode_access_token(token: str) -> uuid.UUID | None:
    """Return the user id inside a valid access token, or None if it is bad/expired or
    another kind of token (e.g. an email verification link)."""
    claims = _decode(token, ACCESS_TOKEN_TYPE)
    try:
        return uuid.UUID(claims["sub"]) if claims else None
    except ValueError:
        return None


def create_email_verification_token(
    user_id: uuid.UUID, email: str, now: datetime | None = None
) -> str:
    """A 24 h link token for this user AND this address: once the email changes, it is void."""
    issued = now or datetime.now(UTC)
    return _encode(
        {"sub": str(user_id), "email": email, "typ": EMAIL_VERIFICATION_TOKEN_TYPE},
        issued + EMAIL_VERIFICATION_TTL,
    )


def decode_email_verification_token(token: str) -> tuple[uuid.UUID, str] | None:
    """(user id, email) of a valid verification token, or None for anything else: expired,
    tampered, an access token, a ticket or promo token."""
    claims = _decode(token, EMAIL_VERIFICATION_TOKEN_TYPE)
    if claims is None or not isinstance(claims.get("email"), str):
        return None
    try:
        return uuid.UUID(claims["sub"]), claims["email"]
    except ValueError:
        return None
