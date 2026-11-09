"""Organizer profile endpoints and the role fields of /auth/me (BE-4)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.modules.events.models import Event, Venue
from app.modules.identity.models import StaffAssignment

Headers = dict[str, str]


@pytest.fixture
def auth(client: TestClient) -> tuple[str, Headers]:
    """A fresh user (unique email, the tests share one database) and its Bearer header."""
    email, password = f"user-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"
    user_id = client.post("/auth/register", json={"email": email, "password": password}).json()[
        "id"
    ]
    token = client.post("/auth/login", data={"username": email, "password": password}).json()[
        "access_token"
    ]
    return user_id, {"Authorization": f"Bearer {token}"}


def test_me_for_a_plain_user(client: TestClient, auth: tuple[str, Headers]) -> None:
    _, headers = auth

    me = client.get("/auth/me", headers=headers).json()

    assert me["is_organizer"] is False
    assert me["organizer_profile_id"] is None
    assert me["staff_event_ids"] == []


def test_create_profile_makes_user_an_organizer(
    client: TestClient, auth: tuple[str, Headers]
) -> None:
    _, headers = auth

    response = client.post(
        "/organizer/profile", json={"display_name": "  Almaty Jazz  "}, headers=headers
    )

    assert response.status_code == 201
    profile = response.json()
    assert profile["display_name"] == "Almaty Jazz"  # trimmed
    assert profile["verification_status"] == "unverified"
    me = client.get("/auth/me", headers=headers).json()
    assert me["is_organizer"] is True
    assert me["organizer_profile_id"] == profile["id"]
    assert client.get("/organizer/profile", headers=headers).json() == profile


def test_second_profile_is_409(client: TestClient, auth: tuple[str, Headers]) -> None:
    _, headers = auth
    client.post("/organizer/profile", json={"display_name": "First"}, headers=headers)

    response = client.post("/organizer/profile", json={"display_name": "Second"}, headers=headers)

    assert response.status_code == 409


def test_get_and_patch_without_profile_are_404(
    client: TestClient, auth: tuple[str, Headers]
) -> None:
    _, headers = auth

    assert client.get("/organizer/profile", headers=headers).status_code == 404
    assert (
        client.patch("/organizer/profile", json={"display_name": "X"}, headers=headers).status_code
        == 404
    )


def test_patch_renames_profile(client: TestClient, auth: tuple[str, Headers]) -> None:
    _, headers = auth
    client.post("/organizer/profile", json={"display_name": "Old"}, headers=headers)

    response = client.patch("/organizer/profile", json={"display_name": "New"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["display_name"] == "New"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_display_name_is_422(
    client: TestClient, auth: tuple[str, Headers], name: str
) -> None:
    _, headers = auth

    response = client.post("/organizer/profile", json={"display_name": name}, headers=headers)

    assert response.status_code == 422


@pytest.mark.parametrize("method", ["get", "post", "patch"])
def test_profile_requires_login(client: TestClient, method: str) -> None:
    response = client.request(method, "/organizer/profile", json={"display_name": "X"})

    assert response.status_code == 401


def test_me_lists_events_the_user_staffs(client: TestClient, auth: tuple[str, Headers]) -> None:
    user_id, headers = auth
    with SessionLocal() as db:
        venue = Venue(name=f"Arena {uuid.uuid4().hex[:6]}", city="Almaty")
        db.add(venue)
        db.commit()
        venue_id = venue.id
    # The event needs an owner with an organizer profile.
    owner_email = f"owner-{uuid.uuid4().hex[:12]}@example.com"
    client.post("/auth/register", json={"email": owner_email, "password": "correct-horse-1"})
    owner_token = client.post(
        "/auth/login", data={"username": owner_email, "password": "correct-horse-1"}
    ).json()["access_token"]
    organizer_headers = {"Authorization": f"Bearer {owner_token}"}
    organizer_id = client.post(
        "/organizer/profile", json={"display_name": "Owner"}, headers=organizer_headers
    ).json()["id"]
    with SessionLocal() as db:
        event = Event(
            organizer_id=uuid.UUID(organizer_id),
            venue_id=venue_id,
            title="Staffed",
            start_at=datetime.now(UTC) + timedelta(days=3),
        )
        db.add(event)
        db.flush()
        db.add(StaffAssignment(event_id=event.id, user_id=uuid.UUID(user_id)))
        db.commit()
        event_id = str(event.id)

    me = client.get("/auth/me", headers=headers).json()

    assert me["staff_event_ids"] == [event_id]
    assert me["is_organizer"] is False  # staffing an event does not make you an organizer
