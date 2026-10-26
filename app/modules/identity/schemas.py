import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.modules.identity.enums import SystemRole


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    system_role: SystemRole
    email_verified: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
