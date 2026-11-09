import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.modules.identity.enums import SystemRole, VerificationStatus

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    system_role: SystemRole
    email_verified: bool


class MeOut(UserOut):
    """`/auth/me`: the user plus the roles that come from other tables."""

    is_organizer: bool
    organizer_profile_id: uuid.UUID | None
    staff_event_ids: list[uuid.UUID]


class OrganizerProfileIn(BaseModel):
    display_name: DisplayName


class OrganizerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    verification_status: VerificationStatus


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
