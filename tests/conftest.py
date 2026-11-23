from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.modules.registry  # noqa: F401
from app.core.db import engine, get_session
from app.main import app
from app.modules.notifications import service as notifications
from app.modules.support.storage import LocalDiskStorage, set_storage
from tests.emails import RecordingEmailProvider


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Session inside an outer transaction that is rolled back after the test.

    session.commit() in code under test only releases a savepoint, so nothing persists.
    Requires a migrated database (uv run alembic upgrade head).
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def session_api(db_session: Session) -> Iterator[TestClient]:
    """The API on the test's own db_session: it sees rows the test made without committing
    them, and everything it writes is rolled back with the test."""

    def _session() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def emails() -> Iterator[RecordingEmailProvider]:
    """Every test sends email through a recording provider: nothing ever reaches SMTP.
    Tests that care read `emails.sent`."""
    recorder = RecordingEmailProvider()
    notifications.set_provider(recorder)
    yield recorder
    notifications.set_provider(None)


@pytest.fixture(autouse=True)
def attachment_storage(tmp_path: Path) -> Iterator[LocalDiskStorage]:
    """Support attachments go to a temporary folder in every test, never to the project's.
    Tests that care read `attachment_storage.root`."""
    storage = LocalDiskStorage(tmp_path / "attachments")
    set_storage(storage)
    yield storage
    set_storage(None)
