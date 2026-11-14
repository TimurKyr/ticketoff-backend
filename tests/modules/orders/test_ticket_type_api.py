import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile
from tests.factories import create_event, create_organizer
from tests.markers import postgres_only

# The capacity check locks the event (FOR UPDATE); sales use free registration.
pytestmark = postgres_only

Headers = dict[str, str]


@dataclass
class Organizer:
    headers: Headers
    profile: OrganizerProfile


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    """Client whose requests use the rolled-back test session, so nothing persists."""
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def login(api: TestClient) -> Callable[[], Headers]:
    """Register a fresh user through the API and return their auth headers."""

    def make() -> Headers:
        email, password = f"user-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"
        assert (
            api.post("/auth/register", json={"email": email, "password": password}).status_code
            == 201
        )
        token = api.post("/auth/login", data={"username": email, "password": password}).json()[
            "access_token"
        ]
        return {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def make_organizer(
    api: TestClient, login: Callable[[], Headers], db_session: Session
) -> Callable[[], Organizer]:
    def make() -> Organizer:
        headers = login()
        response = api.post("/organizer/profile", json={"display_name": "Org"}, headers=headers)
        assert response.status_code == 201, response.text
        return Organizer(headers, db_session.get_one(OrganizerProfile, response.json()["id"]))

    return make


@pytest.fixture
def owner(make_organizer: Callable[[], Organizer]) -> Organizer:
    return make_organizer()


@pytest.fixture
def event(db_session: Session, owner: Organizer) -> Event:
    """A published public event of `owner`, without capacity."""
    return create_event(db_session, owner.profile)


def paid(**overrides: object) -> dict:
    return {"name": "General", "kind": "paid", "price_kzt": 5000, "quantity_total": 100} | overrides


def free(**overrides: object) -> dict:
    return {"name": "Free", "kind": "free", "price_kzt": 0, "quantity_total": 100} | overrides


def create(api: TestClient, owner: Organizer, event: Event, body: dict):
    return api.post(f"/events/{event.id}/ticket-types", json=body, headers=owner.headers)


def created(api: TestClient, owner: Organizer, event: Event, body: dict) -> dict:
    response = create(api, owner, event, body)
    assert response.status_code == 201, response.text
    return response.json()


def patch(api: TestClient, owner: Organizer, event: Event, ticket_type_id: str, body: dict):
    return api.patch(
        f"/events/{event.id}/ticket-types/{ticket_type_id}", json=body, headers=owner.headers
    )


def public(api: TestClient, event: Event):
    return api.get(f"/events/{event.id}/ticket-types/public")


def register(api: TestClient, headers: Headers, event: Event, ticket_type_id: str, n: int):
    response = api.post(
        f"/events/{event.id}/registrations",
        json={
            "items": [{"ticket_type_id": ticket_type_id, "quantity": n}],
            "attendees": [{"name": f"Guest {i}", "email": None} for i in range(n)],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text


def set_capacity(db_session: Session, event: Event, capacity: int | None) -> None:
    event.capacity = capacity
    db_session.flush()


# --- create ---


def test_create_returns_201_with_inventory(api: TestClient, owner: Organizer, event: Event) -> None:
    response = create(api, owner, event, paid(description="Main hall", max_per_order=4))

    assert response.status_code == 201
    body = response.json()
    assert body.pop("id")
    assert body == {
        "event_id": str(event.id),
        "name": "General",
        "description": "Main hall",
        "kind": "paid",
        "price_kzt": 5000,
        "quantity_total": 100,
        "max_per_order": 4,
        "sales_start_at": None,
        "sales_end_at": None,
        "is_hidden": False,
        "sold": 0,
        "available": 100,
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (paid(price_kzt=0), "Paid ticket types must have price_kzt > 0"),
        (free(price_kzt=500), "Free ticket types must have price_kzt = 0"),
    ],
)
def test_create_price_must_match_kind_is_422(
    api: TestClient, owner: Organizer, event: Event, body: dict, message: str
) -> None:
    response = create(api, owner, event, body)

    assert response.status_code == 422
    assert message in response.json()["detail"][0]["msg"]


def test_create_requires_login(api: TestClient, event: Event) -> None:
    assert api.post(f"/events/{event.id}/ticket-types", json=paid()).status_code == 401


def test_create_requires_organizer(
    api: TestClient, login: Callable[[], Headers], event: Event
) -> None:
    response = api.post(f"/events/{event.id}/ticket-types", json=paid(), headers=login())

    assert response.status_code == 403
    assert response.json() == {"detail": "Organizer account required"}


def test_create_on_another_organizers_event_is_404(
    api: TestClient, make_organizer: Callable[[], Organizer], event: Event
) -> None:
    response = create(api, make_organizer(), event, paid())

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_create_on_cancelled_event_is_409(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    event.status = EventStatus.CANCELLED
    db_session.flush()

    response = create(api, owner, event, paid())

    assert response.status_code == 409
    assert response.json() == {"detail": "Ticket types cannot be changed on a cancelled event"}


# --- capacity ---


def test_capacity_sum_is_enforced_on_create(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    set_capacity(db_session, event, 100)
    created(api, owner, event, paid(name="A", quantity_total=60))
    created(api, owner, event, paid(name="B", quantity_total=40))

    response = create(api, owner, event, paid(name="C", quantity_total=1))

    assert response.status_code == 409
    assert response.json() == {"detail": "Total ticket quantity (101) exceeds event capacity (100)"}


def test_capacity_counts_hidden_types(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    set_capacity(db_session, event, 100)
    created(api, owner, event, paid(name="Hidden", quantity_total=60, is_hidden=True))

    response = create(api, owner, event, paid(name="Visible", quantity_total=41))

    assert response.status_code == 409
    assert response.json() == {"detail": "Total ticket quantity (101) exceeds event capacity (100)"}


def test_capacity_sum_is_enforced_on_patch(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    set_capacity(db_session, event, 100)
    a = created(api, owner, event, paid(name="A", quantity_total=60))
    created(api, owner, event, paid(name="B", quantity_total=40))

    response = patch(api, owner, event, a["id"], {"quantity_total": 61})

    assert response.status_code == 409
    assert response.json() == {"detail": "Total ticket quantity (101) exceeds event capacity (100)"}


def test_lowering_is_allowed_when_already_over_capacity(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    a = created(api, owner, event, paid(name="A", quantity_total=60))
    created(api, owner, event, paid(name="B", quantity_total=40))
    set_capacity(db_session, event, 50)  # update_event can still lower capacity

    response = patch(api, owner, event, a["id"], {"quantity_total": 30})

    assert response.status_code == 200
    assert response.json()["quantity_total"] == 30


def test_no_capacity_means_no_limit(api: TestClient, owner: Organizer, event: Event) -> None:
    created(api, owner, event, paid(name="A", quantity_total=100_000, max_per_order=10))
    created(api, owner, event, paid(name="B", quantity_total=100_000, max_per_order=10))


# --- owner list ---


def test_owner_list_includes_hidden_types_with_inventory(
    api: TestClient, owner: Organizer, event: Event, login: Callable[[], Headers]
) -> None:
    on_sale = created(api, owner, event, free(name="On sale", quantity_total=10))
    hidden = created(api, owner, event, free(name="Hidden", quantity_total=5, is_hidden=True))
    register(api, login(), event, on_sale["id"], 3)

    response = api.get(f"/events/{event.id}/ticket-types", headers=owner.headers)

    assert response.status_code == 200
    by_id = {tt["id"]: tt for tt in response.json()}
    assert set(by_id) == {on_sale["id"], hidden["id"]}
    assert (by_id[on_sale["id"]]["sold"], by_id[on_sale["id"]]["available"]) == (3, 7)
    assert (by_id[hidden["id"]]["sold"], by_id[hidden["id"]]["available"]) == (0, 5)
    assert by_id[hidden["id"]]["is_hidden"] is True


def test_owner_list_of_another_organizers_event_is_404(
    api: TestClient, make_organizer: Callable[[], Organizer], event: Event
) -> None:
    response = api.get(f"/events/{event.id}/ticket-types", headers=make_organizer().headers)

    assert response.status_code == 404


# --- patch ---


def test_patch_renames(api: TestClient, owner: Organizer, event: Event) -> None:
    ticket_type = created(api, owner, event, paid())

    response = patch(api, owner, event, ticket_type["id"], {"name": "VIP"})

    assert response.status_code == 200
    assert response.json() == ticket_type | {"name": "VIP"}


def test_patch_type_of_another_event_is_404(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    """Both events are the owner's; the path's event must still match the type."""
    other_event = create_event(db_session, owner.profile)
    foreign = created(api, owner, other_event, paid(name="Other"))

    response = patch(api, owner, event, foreign["id"], {"name": "Hijacked"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket type not found"}


def test_patch_another_organizers_type_through_own_event_is_404(
    api: TestClient,
    owner: Organizer,
    event: Event,
    make_organizer: Callable[[], Organizer],
    db_session: Session,
) -> None:
    stranger = make_organizer()
    strangers_event = create_event(db_session, stranger.profile)
    strangers_type = created(api, stranger, strangers_event, paid())

    response = patch(api, owner, event, strangers_type["id"], {"name": "Hijacked"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Ticket type not found"}


def test_patch_on_another_organizers_event_is_404(
    api: TestClient, owner: Organizer, event: Event, make_organizer: Callable[[], Organizer]
) -> None:
    ticket_type = created(api, owner, event, paid())

    response = patch(api, make_organizer(), event, ticket_type["id"], {"name": "X"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_patch_unknown_type_is_404(api: TestClient, owner: Organizer, event: Event) -> None:
    assert patch(api, owner, event, str(uuid.uuid4()), {"name": "X"}).status_code == 404


def test_patch_price_rule_is_409(api: TestClient, owner: Organizer, event: Event) -> None:
    ticket_type = created(api, owner, event, paid())

    response = patch(api, owner, event, ticket_type["id"], {"price_kzt": 0})

    assert response.status_code == 409
    assert response.json() == {"detail": "Paid ticket types must have price_kzt > 0"}


def test_patch_quantity_below_sold_is_409(
    api: TestClient, owner: Organizer, event: Event, login: Callable[[], Headers]
) -> None:
    ticket_type = created(api, owner, event, free(quantity_total=10))
    register(api, login(), event, ticket_type["id"], 2)

    # max_per_order goes down too, so only the "already sold" rule can reject it.
    response = patch(
        api, owner, event, ticket_type["id"], {"quantity_total": 1, "max_per_order": 1}
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "quantity_total cannot be lower than tickets already sold (2)"
    }


def test_patch_kind_after_sales_is_409(
    api: TestClient, owner: Organizer, event: Event, login: Callable[[], Headers]
) -> None:
    ticket_type = created(api, owner, event, free())
    register(api, login(), event, ticket_type["id"], 1)

    # A matching price is sent too, so only the "already sold" rule can reject it.
    response = patch(api, owner, event, ticket_type["id"], {"kind": "paid", "price_kzt": 5000})

    assert response.status_code == 409
    assert response.json() == {"detail": "Cannot change kind after tickets have been sold"}


def test_patch_null_for_required_field_is_422(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    ticket_type = created(api, owner, event, paid())

    assert patch(api, owner, event, ticket_type["id"], {"name": None}).status_code == 422


def test_no_delete_route(api: TestClient, owner: Organizer, event: Event) -> None:
    ticket_type = created(api, owner, event, paid())

    response = api.delete(
        f"/events/{event.id}/ticket-types/{ticket_type['id']}", headers=owner.headers
    )

    assert response.status_code == 405


# --- public list ---


def test_public_list_needs_no_login_and_shows_only_types_on_sale(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    now = datetime.now(UTC)
    on_sale = created(api, owner, event, paid(name="On sale"))
    window = created(
        api,
        owner,
        event,
        paid(
            name="Window",
            sales_start_at=(now - timedelta(days=1)).isoformat(),
            sales_end_at=(now + timedelta(days=1)).isoformat(),
        ),
    )
    created(api, owner, event, paid(name="Hidden", is_hidden=True))
    created(
        api, owner, event, paid(name="Later", sales_start_at=(now + timedelta(days=1)).isoformat())
    )
    created(
        api, owner, event, paid(name="Over", sales_end_at=(now - timedelta(hours=1)).isoformat())
    )

    response = public(api, event)

    assert response.status_code == 200
    assert {tt["name"] for tt in response.json()} == {on_sale["name"], window["name"]}
    assert "sold" not in response.json()[0]  # organizer-only numbers stay private


def test_public_list_includes_sold_out_with_zero_available(
    api: TestClient, owner: Organizer, event: Event, login: Callable[[], Headers]
) -> None:
    ticket_type = created(api, owner, event, free(quantity_total=1))
    register(api, login(), event, ticket_type["id"], 1)

    [out] = public(api, event).json()

    assert (out["id"], out["available"]) == (ticket_type["id"], 0)


def test_hiding_with_patch_removes_from_public_list(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    ticket_type = created(api, owner, event, paid())

    assert patch(api, owner, event, ticket_type["id"], {"is_hidden": True}).status_code == 200
    assert public(api, event).json() == []


@pytest.mark.parametrize(
    ("status", "visibility"),
    [
        (EventStatus.DRAFT, EventVisibility.PUBLIC),
        (EventStatus.CANCELLED, EventVisibility.PUBLIC),
        (EventStatus.PUBLISHED, EventVisibility.PRIVATE),
    ],
)
def test_public_list_of_non_public_event_is_404(
    api: TestClient,
    db_session: Session,
    status: EventStatus,
    visibility: EventVisibility,
) -> None:
    event = create_event(db_session, create_organizer(db_session), status=status)
    event.visibility = visibility
    db_session.flush()

    response = public(api, event)

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}


def test_public_list_of_unlisted_event_is_shown(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    ticket_type = created(api, owner, event, paid())
    event.visibility = EventVisibility.UNLISTED
    db_session.flush()

    assert [tt["id"] for tt in public(api, event).json()] == [ticket_type["id"]]


def test_public_list_unknown_event_is_404(api: TestClient) -> None:
    assert api.get(f"/events/{uuid.uuid4()}/ticket-types/public").status_code == 404
