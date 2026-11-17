"""Domain errors map to HTTP codes in one place; CORS lets only the web app's origin in."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.common.errors import (
    BusinessRuleError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.error_handlers import add_error_handlers


@pytest.fixture
def errors_client() -> TestClient:
    app = FastAPI()
    add_error_handlers(app)
    errors = {
        "not-found": NotFoundError("Event not found"),
        "forbidden": PermissionDeniedError("Not yours"),
        "conflict": BusinessRuleError("Sold out"),
        "invalid": InvalidInputError("Unknown venue"),
        "other": DomainError("Something else"),
    }

    @app.get("/{name}")
    def fail(name: str) -> None:
        raise errors[name]

    return TestClient(app)


@pytest.mark.parametrize(
    ("name", "code", "detail"),
    [
        ("not-found", 404, "Event not found"),
        ("forbidden", 403, "Not yours"),
        ("conflict", 409, "Sold out"),
        ("invalid", 422, "Unknown venue"),
        ("other", 400, "Something else"),
    ],
)
def test_domain_errors_map_to_http(
    errors_client: TestClient, name: str, code: int, detail: str
) -> None:
    response = errors_client.get(f"/{name}")

    assert response.status_code == code
    assert response.json() == {"detail": detail}


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/events",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


def test_cors_allows_the_configured_web_origin(client: TestClient) -> None:
    response = _preflight(client, "http://localhost:5173")  # default CORS_ORIGINS

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_ignores_other_origins(client: TestClient) -> None:
    response = _preflight(client, "https://evil.example")

    assert "access-control-allow-origin" not in response.headers
