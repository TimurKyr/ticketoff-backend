from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TICKET_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    # Separate from jwt_secret: signs ticket QR tokens, which never expire.
    ticket_signing_secret: str
    environment: str = "local"

    @model_validator(mode="after")
    def check_ticket_signing_secret(self) -> Self:
        # Local dev may use placeholders; anywhere else a weak or shared secret would let
        # someone forge tickets.
        if self.environment == "local":
            return self
        if len(self.ticket_signing_secret) < MIN_TICKET_SECRET_LENGTH:
            raise ValueError(
                f"TICKET_SIGNING_SECRET must be at least {MIN_TICKET_SECRET_LENGTH} characters"
            )
        if self.ticket_signing_secret == self.jwt_secret:
            raise ValueError("TICKET_SIGNING_SECRET must differ from JWT_SECRET")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
