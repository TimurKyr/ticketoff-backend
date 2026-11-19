"""Stage 1 of BE-18: the migration's enum values and users.language."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.ops.enums import NotificationType
from tests.markers import postgres_only

pytestmark = postgres_only


def _insert_user(session: Session, returning: str, language: str | None = None):
    """A raw INSERT that leaves language out (or sets it), so the DB default/CHECK decides."""
    columns = "id, email, password_hash, system_role, email_verified"
    values = ":id, :e, 'x', 'attendee', false"
    if language is not None:
        columns, values = f"{columns}, language", f"{values}, :language"
    sql = f"INSERT INTO users ({columns}) VALUES ({values}) RETURNING {returning}"
    params = {"id": uuid.uuid4(), "e": f"{uuid.uuid4().hex}@example.com", "language": language}
    return session.scalar(text(sql), params)


def test_db_enum_matches_python_enum(db_session: Session) -> None:
    """alembic check can't see enum values; this catches a missing ALTER TYPE."""
    db_values = db_session.scalars(
        text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
            " WHERE t.typname = 'notificationtype'"
        )
    ).all()

    assert set(db_values) == {t.value for t in NotificationType}


def test_new_types_can_be_stored(db_session: Session) -> None:
    user_id = _insert_user(db_session, "id")
    for value in ("account_verification", "payment_failed", "support_status"):
        db_session.execute(
            text(
                "INSERT INTO notifications (id, user_id, type, channel)"
                " VALUES (:id, :u, :t, 'in_app')"
            ),
            {"id": uuid.uuid4(), "u": user_id, "t": value},
        )


def test_language_defaults_to_ru_in_the_database(db_session: Session) -> None:
    language = _insert_user(db_session, "language")

    assert language == "ru"


def test_unsupported_language_is_refused_by_the_database(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_users_language_supported"):
        _insert_user(db_session, "id", language="de")


# --- registration ---


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def _register(api: TestClient, **extra: object):
    body = {"email": f"{uuid.uuid4().hex[:10]}@example.com", "password": "long-password"}
    return api.post("/auth/register", json=body | extra)


def test_register_defaults_to_russian(api: TestClient) -> None:
    response = _register(api)

    assert response.status_code == 201
    assert response.json()["language"] == "ru"


def test_register_in_english_shows_on_me(api: TestClient) -> None:
    created = _register(api, language="en")
    assert (created.status_code, created.json()["language"]) == (201, "en")
    email = created.json()["email"]
    token = api.post("/auth/login", data={"username": email, "password": "long-password"})
    me = api.get("/auth/me", headers={"Authorization": f"Bearer {token.json()['access_token']}"})

    assert me.json()["language"] == "en"


def test_register_with_an_unsupported_language_is_422(api: TestClient) -> None:
    response = _register(api, language="de")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "language"]
