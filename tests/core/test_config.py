import pytest
from pydantic import ValidationError

from app.core.config import Settings

STRONG = "t" * 32


def make(environment: str, ticket_signing_secret: str, jwt_secret: str = "j" * 32) -> Settings:
    # Explicit values win over env vars; _env_file=None ignores the developer's .env.
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db",
        jwt_secret=jwt_secret,
        ticket_signing_secret=ticket_signing_secret,
        environment=environment,
    )


def test_non_local_accepts_strong_distinct_secret() -> None:
    assert make("production", STRONG).ticket_signing_secret == STRONG


@pytest.mark.parametrize("environment", ["production", "staging", "test"])
def test_non_local_rejects_short_secret(environment: str) -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make(environment, "t" * 31)


def test_non_local_rejects_secret_equal_to_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="must differ from JWT_SECRET"):
        make("production", STRONG, jwt_secret=STRONG)


@pytest.mark.parametrize(("ticket_secret", "jwt_secret"), [("short", "j" * 32), ("same", "same")])
def test_local_allows_weak_secrets(ticket_secret: str, jwt_secret: str) -> None:
    assert make("local", ticket_secret, jwt_secret=jwt_secret).environment == "local"
