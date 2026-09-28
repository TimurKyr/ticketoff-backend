from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import get_session
from app.main import app


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


class _BrokenSession:
    def execute(self, *args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("database unavailable")


def _broken_session() -> Iterator[_BrokenSession]:
    yield _BrokenSession()


def test_health_db_down(client: TestClient) -> None:
    app.dependency_overrides[get_session] = _broken_session

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
