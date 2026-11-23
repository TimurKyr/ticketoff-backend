"""Organizer contacts and statuses on /organizer/profile, and the public organizer on events
(BE-29), over HTTP on the rolled-back session."""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.identity.enums import PayoutAccountStatus, SystemRole
from app.modules.identity.models import OrganizerProfile, PayoutAccount, User
from app.modules.identity.security import create_access_token
from tests.factories import create_event, create_free_ticket_type
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
CONTACTS = {
    "contact_email": "Info@Jazz-Club.KZ",
    "contact_phone": " +7 (701) 123-45-67 ",
    "website": "https://jazz-club.kz/events",
}
STORED = {
    "contact_email": "info@jazz-club.kz",
    "contact_phone": "+77011234567",
    "website": "https://jazz-club.kz/events",
}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def login(api: TestClient, db_session: Session) -> Callable[[], tuple[Headers, User]]:
    def make() -> tuple[Headers, User]:
        user = User(email=f"user-{uuid.uuid4().hex[:12]}@example.com", password_hash="x")
        db_session.add(user)
        db_session.flush()
        return {"Authorization": f"Bearer {create_access_token(user.id)}"}, user

    return make


def become_organizer(api: TestClient, headers: Headers, **contacts: object):
    return api.post(
        "/organizer/profile", json={"display_name": "Jazz Club"} | contacts, headers=headers
    )


# --- create and read ---


def test_create_with_contacts_stores_normalized_forms(
    api: TestClient, login: Callable, db_session: Session
) -> None:
    headers, _ = login()

    created = become_organizer(api, headers, **CONTACTS)

    assert created.status_code == 201, created.text
    body = created.json()
    assert {k: body[k] for k in STORED} == STORED
    assert (body["verification_status"], body["payout_status"]) == ("unverified", None)
    assert api.get("/organizer/profile", headers=headers).json() == body
    profile = db_session.get_one(OrganizerProfile, body["id"])
    assert (profile.contact_email, profile.contact_phone) == (
        STORED["contact_email"],
        STORED["contact_phone"],
    )


def test_create_without_contacts_still_works(api: TestClient, login: Callable) -> None:
    headers, _ = login()

    body = become_organizer(api, headers).json()

    assert (body["contact_email"], body["contact_phone"], body["website"]) == (None, None, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contact_email", "not-an-email"),
        ("contact_phone", "12345"),  # too few digits
        ("contact_phone", "+7 701 123 45 67 890 12"),  # too many
        ("contact_phone", "+7 701 CALL ME"),
        ("contact_phone", "7+7011234567"),  # + only at the start
        ("website", "ftp://jazz-club.kz"),
        ("website", "jazz-club.kz"),
        ("website", "https://"),
        ("website", "https://jazz-club.kz/" + "a" * 260),
    ],
)
def test_invalid_contacts_are_422(api: TestClient, login: Callable, field: str, value: str) -> None:
    headers, _ = login()
    assert become_organizer(api, headers, **{field: value}).status_code == 422

    assert become_organizer(api, headers).status_code == 201
    assert api.patch("/organizer/profile", json={field: value}, headers=headers).status_code == 422


# --- partial PATCH ---


def test_patch_changes_only_what_is_sent(api: TestClient, login: Callable) -> None:
    headers, _ = login()
    become_organizer(api, headers, **CONTACTS)

    body = api.patch(
        "/organizer/profile", json={"website": "http://new.kz"}, headers=headers
    ).json()

    assert body["website"] == "http://new.kz"
    assert (body["display_name"], body["contact_email"], body["contact_phone"]) == (
        "Jazz Club",
        STORED["contact_email"],
        STORED["contact_phone"],
    )


def test_explicit_null_clears_a_contact(api: TestClient, login: Callable) -> None:
    headers, _ = login()
    become_organizer(api, headers, **CONTACTS)

    body = api.patch(
        "/organizer/profile", json={"contact_phone": None, "website": None}, headers=headers
    ).json()

    assert (body["contact_phone"], body["website"]) == (None, None)
    assert body["contact_email"] == STORED["contact_email"]


def test_display_name_cannot_be_cleared(api: TestClient, login: Callable) -> None:
    headers, _ = login()
    become_organizer(api, headers)

    assert (
        api.patch("/organizer/profile", json={"display_name": None}, headers=headers).status_code
        == 422
    )
    renamed = api.patch("/organizer/profile", json={"display_name": " Jazz "}, headers=headers)
    assert renamed.json()["display_name"] == "Jazz"


def test_patch_only_touches_my_own_profile(
    api: TestClient, login: Callable, db_session: Session
) -> None:
    owner_headers, _ = login()
    owner = become_organizer(api, owner_headers, **CONTACTS).json()
    other_headers, _ = login()

    # There is no profile id in the path: another user can only reach their own profile.
    assert (
        api.patch("/organizer/profile", json={"website": None}, headers=other_headers).status_code
        == 404
    )
    become_organizer(api, other_headers)
    api.patch("/organizer/profile", json={"website": None}, headers=other_headers)

    db_session.expire_all()
    assert db_session.get_one(OrganizerProfile, owner["id"]).website == STORED["website"]


def test_patch_needs_a_login(api: TestClient) -> None:
    assert api.patch("/organizer/profile", json={"website": None}).status_code == 401


# --- read-only statuses ---


@pytest.mark.parametrize(
    ("accounts", "expected"),
    [
        ([], None),
        ([PayoutAccountStatus.PENDING], "pending"),
        ([PayoutAccountStatus.REJECTED, PayoutAccountStatus.VERIFIED], "verified"),
        ([PayoutAccountStatus.REJECTED, PayoutAccountStatus.PENDING], "pending"),
    ],
)
def test_payout_status_from_the_payout_account(
    api: TestClient, login: Callable, db_session: Session, accounts: list, expected: str | None
) -> None:
    headers, _ = login()
    profile_id = become_organizer(api, headers).json()["id"]
    for status in accounts:
        db_session.add(PayoutAccount(organizer_id=profile_id, status=status))
    db_session.flush()

    assert api.get("/organizer/profile", headers=headers).json()["payout_status"] == expected


def test_statuses_cannot_be_set_through_the_api(
    api: TestClient, login: Callable, db_session: Session
) -> None:
    headers, _ = login()
    become_organizer(api, headers)

    body = api.patch(
        "/organizer/profile",
        json={"verification_status": "verified", "payout_status": "verified"},
        headers=headers,
    ).json()

    assert (body["verification_status"], body["payout_status"]) == ("unverified", None)


# --- the public organizer on events ---


def _published(db_session: Session, profile_id: str) -> Event:
    event = create_event(db_session, db_session.get_one(OrganizerProfile, profile_id))
    event.title = f"Contacts {uuid.uuid4().hex}"  # findable with q, whatever else is listed
    create_free_ticket_type(db_session, event)
    db_session.flush()
    return event


def test_public_event_shows_name_and_email_never_the_phone(
    api: TestClient, login: Callable, db_session: Session
) -> None:
    headers, _ = login()
    profile_id = become_organizer(api, headers, **CONTACTS).json()["id"]
    event = _published(db_session, profile_id)

    page = api.get(f"/events/{event.id}").json()
    [listed] = api.get("/events", params={"q": event.title}).json()["items"]

    expected = {"id": profile_id, "display_name": "Jazz Club", "contact_email": "info@jazz-club.kz"}
    for body in (page, listed):
        assert body["organizer"] == expected
        assert "+77011234567" not in str(body) and "jazz-club.kz/events" not in str(body)
        assert "verification_status" not in str(body) and "payout" not in str(body)


def test_organizer_without_contact_email_is_null_email(
    api: TestClient, login: Callable, db_session: Session
) -> None:
    headers, _ = login()
    profile_id = become_organizer(api, headers).json()["id"]
    event = _published(db_session, profile_id)

    assert api.get(f"/events/{event.id}").json()["organizer"] == {
        "id": profile_id,
        "display_name": "Jazz Club",
        "contact_email": None,
    }


def test_admin_event_responses_carry_the_organizer(
    api: TestClient, login: Callable, db_session: Session
) -> None:
    headers, _ = login()
    profile_id = become_organizer(api, headers, **CONTACTS).json()["id"]
    event = _published(db_session, profile_id)
    admin_headers, admin = login()
    admin.system_role = SystemRole.PLATFORM_ADMIN
    db_session.flush()

    for action in ("suspend", "unsuspend"):
        body = api.post(f"/admin/events/{event.id}/{action}", headers=admin_headers).json()
        assert body["organizer"]["contact_email"] == "info@jazz-club.kz"
        assert "contact_phone" not in body["organizer"]


# --- phone normalization rules ---


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("+7 (701) 123-45-67", "+77011234567"),  # international: kept
        ("+44 20 7946 0958", "+442079460958"),
        ("8 701 123 45 67", "+77011234567"),  # 11 digits with the 8 trunk prefix
        ("8(701)123-45-67", "+77011234567"),
        ("701 123 45 67", "+77011234567"),  # 10 digits starting with 7
    ],
)
def test_phone_is_stored_in_international_form(
    api: TestClient, login: Callable, typed: str, stored: str
) -> None:
    headers, _ = login()

    body = become_organizer(api, headers, contact_phone=typed).json()

    assert body["contact_phone"] == stored


@pytest.mark.parametrize(
    "typed",
    [
        "8 701 123 45 6",  # 10 digits starting with 8
        "8 701 123 45 678",  # 12 digits starting with 8
        "601 123 45 67",  # 10 digits, not starting with 7
        "77 701 123 45 67",  # 12 digits without +
        "020 7946 0958",  # a local number of another country
    ],
)
def test_other_numbers_without_plus_are_refused(
    api: TestClient, login: Callable, typed: str
) -> None:
    headers, _ = login()

    response = become_organizer(api, headers, contact_phone=typed)

    assert response.status_code == 422
    assert "Use the international format, e.g. +7 701 123 45 67" in response.text
