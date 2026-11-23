import re
import uuid
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

from app.modules.identity.enums import (
    Language,
    PayoutAccountStatus,
    SystemRole,
    VerificationStatus,
)

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # Language of the account's emails.
    language: Language = Language.RU


class VerifyEmailIn(BaseModel):
    # The token from the link's ?token= (a JWT; far below this limit).
    token: str = Field(min_length=1, max_length=2048)


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    # The token from the link's ?token= and the new password (same rules as registration).
    token: str = Field(min_length=1, max_length=2048)
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    system_role: SystemRole
    email_verified: bool
    language: Language


class MeOut(UserOut):
    """`/auth/me`: the user plus the roles that come from other tables."""

    is_organizer: bool
    organizer_profile_id: uuid.UUID | None
    staff_event_ids: list[uuid.UUID]


PHONE_DIGITS_MIN, PHONE_DIGITS_MAX = 10, 15
WEBSITE_MAX_LENGTH = 255
_PHONE_INPUT = re.compile(r"^\+?[0-9 ()\-]+$")


LOCAL_PHONE_MESSAGE = "Use the international format, e.g. +7 701 123 45 67"


def normalize_phone(value: str | None) -> str | None:
    """Stored as "+" and 10-15 digits; spaces, dashes and brackets are dropped.

    - "+..." (international): kept, e.g. "+7 (701) 123-45-67" -> "+77011234567";
    - 11 digits starting with 8 (the KZ/RU trunk prefix): "8 701 123 45 67" -> "+77011234567";
    - 10 digits starting with 7 (a KZ number without the country code): "701 123 45 67"
      -> "+77011234567";
    - anything else without "+" is refused: guessing would store a wrong number silently.
    """
    if value is None:
        return None
    raw = value.strip()
    if not _PHONE_INPUT.fullmatch(raw) or "+" in raw[1:]:
        raise ValueError("Phone may contain only digits, spaces, dashes, brackets and a leading +")
    digits = re.sub(r"[^0-9]", "", raw)
    if raw.startswith("+"):
        if not PHONE_DIGITS_MIN <= len(digits) <= PHONE_DIGITS_MAX:
            raise ValueError(f"Phone must have {PHONE_DIGITS_MIN} to {PHONE_DIGITS_MAX} digits")
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"
    if len(digits) == 10 and digits.startswith("7"):
        return f"+7{digits}"
    raise ValueError(LOCAL_PHONE_MESSAGE)


def check_website(value: str | None) -> str | None:
    """An http(s) URL with a host, at most 255 characters; kept as typed (trimmed)."""
    if value is None:
        return None
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        raise ValueError("Website must be an http:// or https:// address")
    if len(url) > WEBSITE_MAX_LENGTH:
        raise ValueError(f"Website must be at most {WEBSITE_MAX_LENGTH} characters")
    return url


def lower_email(value: str | None) -> str | None:
    return value.lower() if value is not None else None


ContactEmail = Annotated[EmailStr, AfterValidator(lower_email)]
ContactPhone = Annotated[str, AfterValidator(normalize_phone)]
Website = Annotated[str, AfterValidator(check_website)]


class OrganizerProfileIn(BaseModel):
    """Becoming an organizer. The contacts are optional and never block publishing."""

    display_name: DisplayName
    contact_email: ContactEmail | None = None
    contact_phone: ContactPhone | None = None
    website: Website | None = None


class OrganizerProfileUpdate(BaseModel):
    """Partial: only the fields sent change. An explicit null clears a contact; the display
    name cannot be cleared."""

    display_name: DisplayName | None = None
    contact_email: ContactEmail | None = None
    contact_phone: ContactPhone | None = None
    website: Website | None = None

    @field_validator("display_name", mode="before")
    @classmethod
    def display_name_not_null(cls, value: object) -> object:
        # Runs only when the field was sent: leaving it out is fine, sending null is not.
        if value is None:
            raise ValueError("may not be null")
        return value


class OrganizerProfileOut(BaseModel):
    id: uuid.UUID
    display_name: str
    contact_email: str | None
    contact_phone: str | None
    website: str | None
    # Read-only. verification_status is the profile's own field; payout_status comes from
    # the organizer's payout account (the best one when there are several), null if none.
    verification_status: VerificationStatus
    payout_status: PayoutAccountStatus | None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
