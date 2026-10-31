from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    # Separate from jwt_secret: signs ticket QR tokens, which never expire.
    ticket_signing_secret: str
    environment: str = "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
