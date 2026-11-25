from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32
# Placeholders from .env.example that must never reach a real deployment.
_PLACEHOLDER_MARK = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    # Separate from jwt_secret: signs ticket QR tokens, which never expire.
    ticket_signing_secret: str
    environment: str = "local"
    # Comma-separated browser origins allowed to call the API (the web app).
    cors_origins: str = "http://localhost:5173"
    # Public address of the web app. A campaign QR encodes a link on it, so a phone camera
    # opens the event page with the promo code (BE-16).
    frontend_url: str = "https://ticketoff.store"
    # Public address of this API. Links that must reach the API itself, not the web app
    # (the .ics calendar file, BE-31), are built on it.
    public_api_url: str = "https://api.ticketoff.store"
    # Simulated payments (BE-14). The fee must be positive: ck_payments_amount_positive.
    activation_fee_kzt: int = Field(default=5000, ge=1)
    processing_fee_percent: int = Field(default=3, ge=0, le=100)
    # How long a pending paid order holds inventory before it expires.
    checkout_hold_minutes: int = Field(default=15, ge=1)
    # Where support message attachments are stored (BE-33). In production this is the
    # docker volume mounted at /data/attachments; back it up together with the database.
    attachments_dir: str = "data/attachments"
    # Event cover images (R-12). In production a folder inside the attachments volume, so
    # the same backup covers them.
    event_images_dir: str = "data/attachments/events"
    # Email (BE-18). "console" only logs the emails (local, tests); "smtp" sends them.
    email_provider: Literal["console", "smtp"] = "console"
    email_from: str = "ticketoff <no-reply@ticketoff.store>"
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    # Seconds, per SMTP step. A slow or dead mail server fails only that email (logged), never
    # the action; but emails are sent in the request after the commit, so it waits that long.
    smtp_timeout: float = Field(default=10, gt=0)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def check_email(self) -> Self:
        # Fail at startup, not on the first email.
        if self.email_provider == "smtp" and not self.smtp_host:
            raise ValueError("EMAIL_PROVIDER=smtp needs SMTP_HOST")
        return self

    @model_validator(mode="after")
    def check_secrets(self) -> Self:
        # Local dev may use placeholders; anywhere else a weak or shared secret would let
        # someone forge access tokens or tickets.
        if self.environment == "local":
            return self
        for name, value in (
            ("JWT_SECRET", self.jwt_secret),
            ("TICKET_SIGNING_SECRET", self.ticket_signing_secret),
        ):
            if len(value) < MIN_SECRET_LENGTH:
                raise ValueError(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
            if _PLACEHOLDER_MARK in value:
                raise ValueError(f"{name} is still the placeholder from .env.example")
        if self.ticket_signing_secret == self.jwt_secret:
            raise ValueError("TICKET_SIGNING_SECRET must differ from JWT_SECRET")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
