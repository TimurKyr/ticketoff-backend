from collections.abc import AsyncIterator

from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from app.core.db import get_session
from app.main import app


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


class _BrokenSession:
    async def execute(self, *args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


async def _broken_session() -> AsyncIterator[_BrokenSession]:
    yield _BrokenSession()


async def test_health_db_down(client: AsyncClient) -> None:
    app.dependency_overrides[get_session] = _broken_session

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
