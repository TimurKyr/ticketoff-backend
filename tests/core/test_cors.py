"""CORS for the web app: it reads Date (checkout timer on server time) and Retry-After (the
resend cooldown after a 429) cross-origin, so both must be exposed."""

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.identity.security import create_access_token
from tests.factories import create_user
from tests.markers import postgres_only


def exposed(response) -> list[str]:
    return [h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")]


def test_cors_exposes_date_and_retry_after(client: TestClient) -> None:
    origin = get_settings().cors_origin_list[0]

    response = client.get("/health", headers={"Origin": origin})

    assert response.headers["access-control-allow-origin"] == origin
    assert {"date", "retry-after"} <= set(exposed(response))


@postgres_only
def test_a_cross_origin_429_from_resend_carries_and_exposes_retry_after(
    client: TestClient, db_session: Session
) -> None:
    app.dependency_overrides[get_session] = lambda: db_session
    user = create_user(db_session)
    headers = {
        "Origin": get_settings().cors_origin_list[0],
        "Authorization": f"Bearer {create_access_token(user.id)}",
    }

    first = client.post("/auth/verify-email/resend", headers=headers)
    second = client.post("/auth/verify-email/resend", headers=headers)

    assert first.status_code == 204
    assert second.status_code == 429
    assert 1 <= int(second.headers["retry-after"]) <= 60
    assert second.headers["access-control-allow-origin"] == headers["Origin"]
    assert "retry-after" in exposed(second)


def test_an_unknown_origin_gets_no_cors_headers(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in response.headers


@pytest.fixture
def live_server() -> Iterator[str]:
    """The app under real uvicorn: Date is added by the server, not by the app, so the
    TestClient cannot show it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_the_served_response_has_a_readable_date_header(live_server: str) -> None:
    origin = get_settings().cors_origin_list[0]

    response = httpx.get(f"{live_server}/health", headers={"Origin": origin})

    assert response.headers.get("date")  # e.g. "Sat, 21 Nov 2026 10:00:00 GMT"
    assert {"date", "retry-after"} <= set(exposed(response))
