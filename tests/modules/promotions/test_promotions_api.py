"""Campaigns, promo codes and the campaign QR (BE-16) over HTTP, on the rolled-back session."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Order, TicketType
from app.modules.promotions.models import PromoCode, PromoRedemption
from tests.factories import create_event, create_ticket_type
from tests.markers import postgres_only
from tests.qr import decode_qr_png

pytestmark = postgres_only

Headers = dict[str, str]


@dataclass
class Organizer:
    headers: Headers
    profile: OrganizerProfile


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def login(api: TestClient) -> Callable[[], Headers]:
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
    event = create_event(db_session, owner.profile)
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, price_kzt=10000, quantity_total=50)


def new_campaign(api: TestClient, owner: Organizer, event: Event, **overrides: object) -> dict:
    body = {"name": "Spring", "discount_type": "percent", "discount_value": 20} | overrides
    response = api.post(f"/events/{event.id}/campaigns", json=body, headers=owner.headers)
    assert response.status_code == 201, response.text
    return response.json()


def new_code(
    api: TestClient, owner: Organizer, event: Event, campaign: dict, code: str | None = None
) -> dict:
    response = api.post(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes",
        json={"code": code} if code else {},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def checkout(api: TestClient, headers: Headers, event: Event, tt: TicketType, qty=2, promo=None):
    body = {
        "items": [{"ticket_type_id": str(tt.id), "quantity": qty}],
        "attendees": [{"name": f"Guest {n}", "email": None} for n in range(qty)],
    }
    if promo is not None:
        body["promo_code"] = promo
    return api.post(f"/events/{event.id}/checkout", json=body, headers=headers)


def check(api: TestClient, headers: Headers, event: Event, code: str):
    return api.post(f"/events/{event.id}/promo-codes/check", json={"code": code}, headers=headers)


# --- organizer: campaigns -------------------------------------------------------------------


def test_create_campaign(api: TestClient, owner: Organizer, event: Event) -> None:
    campaign = new_campaign(api, owner, event, max_redemptions=100)

    assert campaign["event_id"] == str(event.id)
    assert (campaign["discount_type"], campaign["discount_value"]) == ("percent", 20)
    assert campaign["max_redemptions"] == 100
    listed = api.get(f"/events/{event.id}/campaigns", headers=owner.headers).json()
    assert [c["id"] for c in listed] == [campaign["id"]]


@pytest.mark.parametrize(
    "body",
    [
        {"discount_type": "percent", "discount_value": 101},
        {"discount_type": "fixed", "discount_value": 0},
        {"discount_type": "percent", "discount_value": 10, "max_redemptions": 0},
        {
            "discount_type": "percent",
            "discount_value": 10,
            "starts_at": "2030-02-01T00:00:00Z",
            "ends_at": "2030-01-01T00:00:00Z",
        },
        {"discount_type": "percent", "discount_value": 10, "starts_at": "2030-01-01T00:00:00"},
    ],
)
def test_invalid_campaign_is_422(
    api: TestClient, owner: Organizer, event: Event, body: dict
) -> None:
    response = api.post(
        f"/events/{event.id}/campaigns", json={"name": "X"} | body, headers=owner.headers
    )

    assert response.status_code == 422


def test_other_organizer_gets_404(
    api: TestClient, make_organizer: Callable[[], Organizer], owner: Organizer, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    other = make_organizer()

    assert api.get(f"/events/{event.id}/campaigns", headers=other.headers).status_code == 404
    assert (
        api.post(
            f"/events/{event.id}/campaigns/{campaign['id']}/codes", json={}, headers=other.headers
        ).status_code
        == 404
    )


def test_buyer_is_not_an_organizer(
    api: TestClient, login: Callable[[], Headers], event: Event
) -> None:
    response = api.get(f"/events/{event.id}/campaigns", headers=login())

    assert response.status_code == 403


# --- organizer: codes -----------------------------------------------------------------------


def test_code_is_generated_with_a_campaign_qr_token(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    code = new_code(api, owner, event, new_campaign(api, owner, event))

    assert len(code["code"]) == 8
    assert code["code"] == code["code"].upper()
    assert code["qr_token"].startswith("pc1.")
    assert (code["status"], code["redemptions"]) == ("active", 0)


def test_custom_code_is_stored_upper_case_and_unique(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    assert new_code(api, owner, event, campaign, "spring20")["code"] == "SPRING20"

    again = api.post(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes",
        json={"code": "Spring20"},
        headers=owner.headers,
    )

    assert again.status_code == 409


def test_disable_and_enable_a_code(api: TestClient, owner: Organizer, event: Event) -> None:
    campaign = new_campaign(api, owner, event)
    code = new_code(api, owner, event, campaign, "SPRING20")
    url = f"/events/{event.id}/campaigns/{campaign['id']}/codes/{code['id']}"

    off = api.patch(url, json={"status": "disabled"}, headers=owner.headers)
    on = api.patch(url, json={"status": "active"}, headers=owner.headers)
    bad = api.patch(url, json={"status": "exhausted"}, headers=owner.headers)

    assert (off.json()["status"], on.json()["status"], bad.status_code) == (
        "disabled",
        "active",
        422,
    )


# --- buyer: check a code --------------------------------------------------------------------


def test_check_code_is_case_insensitive(
    api: TestClient, owner: Organizer, login: Callable[[], Headers], event: Event
) -> None:
    new_code(api, owner, event, new_campaign(api, owner, event), "SPRING20")

    response = check(api, login(), event, " spring20 ")

    assert response.status_code == 200
    assert response.json() == {
        "code": "SPRING20",
        "campaign_name": "Spring",
        "discount_type": "percent",
        "discount_value": 20,
    }


def test_check_requires_login(api: TestClient, event: Event) -> None:
    response = api.post(f"/events/{event.id}/promo-codes/check", json={"code": "X"})

    assert response.status_code == 401


def test_unknown_code_and_code_of_another_event_are_404(
    api: TestClient,
    login: Callable[[], Headers],
    make_organizer: Callable[[], Organizer],
    db_session: Session,
    event: Event,
) -> None:
    other_owner = make_organizer()
    other_event = create_event(db_session, other_owner.profile)
    new_code(api, other_owner, other_event, new_campaign(api, other_owner, other_event), "THEIRS")
    buyer = login()

    assert check(api, buyer, event, "NOPE").status_code == 404
    assert check(api, buyer, event, "THEIRS").status_code == 404


def test_disabled_not_started_and_expired_codes_are_409(
    api: TestClient, owner: Organizer, login: Callable[[], Headers], event: Event
) -> None:
    now = datetime.now(UTC)
    disabled = new_campaign(api, owner, event, name="A")
    code = new_code(api, owner, event, disabled, "OFF")
    api.patch(
        f"/events/{event.id}/campaigns/{disabled['id']}/codes/{code['id']}",
        json={"status": "disabled"},
        headers=owner.headers,
    )
    future = new_campaign(
        api, owner, event, name="B", starts_at=(now + timedelta(days=1)).isoformat()
    )
    new_code(api, owner, event, future, "LATER")
    past = new_campaign(
        api,
        owner,
        event,
        name="C",
        starts_at=(now - timedelta(days=5)).isoformat(),
        ends_at=(now - timedelta(days=1)).isoformat(),
    )
    new_code(api, owner, event, past, "OLD")
    buyer = login()

    assert check(api, buyer, event, "OFF").status_code == 409
    assert "not active yet" in check(api, buyer, event, "LATER").json()["detail"]
    assert "expired" in check(api, buyer, event, "OLD").json()["detail"]


# --- checkout -------------------------------------------------------------------------------


def test_percent_discount_at_checkout(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
    paid_type: TicketType,
) -> None:
    new_code(api, owner, event, new_campaign(api, owner, event), "SPRING20")

    response = checkout(api, login(), event, paid_type, 2, promo="spring20")

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["total_kzt"], body["discount_kzt"]) == (20000, 4000)
    redemption = db_session.scalars(select(PromoRedemption)).one()
    assert (str(redemption.order_id), redemption.discount_kzt) == (body["id"], 4000)


def test_fixed_discount_never_makes_the_order_free(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    event: Event,
    paid_type: TicketType,
) -> None:
    campaign = new_campaign(api, owner, event, discount_type="fixed", discount_value=50000)
    new_code(api, owner, event, campaign, "BIG")

    body = checkout(api, login(), event, paid_type, 1, promo="BIG").json()

    assert (body["total_kzt"], body["discount_kzt"]) == (10000, 9999)


def test_checkout_without_a_code_has_no_discount(
    api: TestClient, login: Callable[[], Headers], event: Event, paid_type: TicketType
) -> None:
    body = checkout(api, login(), event, paid_type).json()

    assert body["discount_kzt"] == 0


def test_unknown_code_at_checkout_creates_no_order(
    api: TestClient,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
    paid_type: TicketType,
) -> None:
    before = db_session.scalar(select(func.count()).select_from(Order))

    response = checkout(api, login(), event, paid_type, promo="NOPE")

    assert response.status_code == 404
    # Compared with "before": the database may already hold orders from other tests.
    assert db_session.scalar(select(func.count()).select_from(Order)) == before


def test_payment_charges_the_discounted_amount(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    event: Event,
    paid_type: TicketType,
) -> None:
    new_code(api, owner, event, new_campaign(api, owner, event), "SPRING20")
    buyer = login()
    order = checkout(api, buyer, event, paid_type, 2, promo="SPRING20").json()

    paid = api.post(f"/orders/{order['id']}/pay", json={"simulate": "success"}, headers=buyer)

    assert paid.status_code == 200, paid.text
    assert paid.json()["payment"]["amount_kzt"] == 16000


def test_a_fully_used_campaign_refuses_the_next_buyer(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    event: Event,
    paid_type: TicketType,
) -> None:
    new_code(api, owner, event, new_campaign(api, owner, event, max_redemptions=1), "ONCE")

    first = checkout(api, login(), event, paid_type, promo="ONCE")
    second = checkout(api, login(), event, paid_type, promo="ONCE")

    assert (first.status_code, second.status_code) == (201, 409)
    assert "fully used" in second.json()["detail"]


def test_a_cancelled_order_gives_the_use_back(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
    paid_type: TicketType,
) -> None:
    new_code(api, owner, event, new_campaign(api, owner, event, max_redemptions=1), "ONCE")
    first = checkout(api, login(), event, paid_type, promo="ONCE").json()
    db_session.execute(
        update(Order).where(Order.id == uuid.UUID(first["id"])).values(status=OrderStatus.CANCELLED)
    )

    again = checkout(api, login(), event, paid_type, promo="ONCE")

    assert again.status_code == 201


def test_an_abandoned_checkout_gives_the_use_back(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
    paid_type: TicketType,
) -> None:
    new_code(api, owner, event, new_campaign(api, owner, event, max_redemptions=1), "ONCE")
    first = checkout(api, login(), event, paid_type, promo="ONCE").json()
    db_session.execute(
        update(Order)
        .where(Order.id == uuid.UUID(first["id"]))
        .values(created_at=datetime.now(UTC) - timedelta(days=1))
    )

    again = checkout(api, login(), event, paid_type, promo="ONCE")

    assert again.status_code == 201


def test_code_list_shows_uses_and_exhausted_status(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    event: Event,
    paid_type: TicketType,
) -> None:
    campaign = new_campaign(api, owner, event, max_redemptions=1)
    new_code(api, owner, event, campaign, "ONCE")
    checkout(api, login(), event, paid_type, promo="ONCE")

    codes = api.get(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes", headers=owner.headers
    ).json()

    assert [(c["code"], c["redemptions"], c["status"]) for c in codes] == [("ONCE", 1, "exhausted")]


# --- campaign QR ----------------------------------------------------------------------------


def test_qr_png_encodes_a_link_to_the_event_with_the_token(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    code = new_code(api, owner, event, campaign)

    response = api.get(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes/{code['id']}/qr.png",
        headers=owner.headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    # A phone camera must open something: a link to the event page, carrying the token.
    expected = (
        f"{get_settings().frontend_url.rstrip('/')}/events/{event.id}?promo={code['qr_token']}"
    )
    assert decode_qr_png(response.content) == expected


def test_qr_png_of_another_organizers_event_is_404(
    api: TestClient, make_organizer: Callable[[], Organizer], owner: Organizer, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    code = new_code(api, owner, event, campaign)

    response = api.get(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes/{code['id']}/qr.png",
        headers=make_organizer().headers,
    )

    assert response.status_code == 404


def test_scanned_campaign_qr_leads_to_the_event_without_login(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    code = new_code(api, owner, event, new_campaign(api, owner, event), "SPRING20")

    response = api.get(f"/promo-qr/{code['qr_token']}")

    assert response.status_code == 200
    assert response.json() == {"event_id": str(event.id), "code": "SPRING20"}
    assert api.get("/promo-qr/pc1.unknown").status_code == 404


@pytest.mark.parametrize("form", ["token", "link"])
def test_the_door_rejects_a_campaign_qr(
    api: TestClient, owner: Organizer, event: Event, form: str
) -> None:
    code = new_code(api, owner, event, new_campaign(api, owner, event))
    scanned = (
        code["qr_token"]
        if form == "token"
        else f"https://ticketoff.store/events/{event.id}?promo={code['qr_token']}"
    )

    response = api.post(
        "/checkin/scan",
        json={"event_id": str(event.id), "qr_token": scanned},
        headers=owner.headers,
    )

    assert response.status_code == 200
    assert response.json()["result"] == "campaign_qr_rejected"


# --- review follow-ups ----------------------------------------------------------------------


def make_unusable_codes(api: TestClient, owner: Organizer, event: Event) -> None:
    """A disabled code, one that has not started and one that has expired."""
    now = datetime.now(UTC)
    off = new_campaign(api, owner, event, name="A")
    code = new_code(api, owner, event, off, "OFF")
    api.patch(
        f"/events/{event.id}/campaigns/{off['id']}/codes/{code['id']}",
        json={"status": "disabled"},
        headers=owner.headers,
    )
    later = new_campaign(
        api, owner, event, name="B", starts_at=(now + timedelta(days=1)).isoformat()
    )
    new_code(api, owner, event, later, "LATER")
    old = new_campaign(
        api,
        owner,
        event,
        name="C",
        starts_at=(now - timedelta(days=5)).isoformat(),
        ends_at=(now - timedelta(days=1)).isoformat(),
    )
    new_code(api, owner, event, old, "OLD")


@pytest.mark.parametrize("code", ["OFF", "LATER", "OLD"])
def test_checkout_refuses_a_code_that_cannot_be_used(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
    paid_type: TicketType,
    code: str,
) -> None:
    make_unusable_codes(api, owner, event)
    before = db_session.scalar(select(func.count()).select_from(Order))

    response = checkout(api, login(), event, paid_type, promo=code)

    assert response.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(Order)) == before


@pytest.mark.parametrize("code", ["OFF", "LATER", "OLD"])
def test_scanned_qr_of_a_code_that_cannot_be_used_is_409(
    api: TestClient, owner: Organizer, db_session: Session, event: Event, code: str
) -> None:
    make_unusable_codes(api, owner, event)
    token = db_session.scalars(select(PromoCode.qr_token).where(PromoCode.code == code)).one()

    assert api.get(f"/promo-qr/{token}").status_code == 409


def test_a_discount_that_rounds_to_zero_is_refused(
    api: TestClient,
    owner: Organizer,
    login: Callable[[], Headers],
    db_session: Session,
    event: Event,
) -> None:
    cheap = create_ticket_type(db_session, event, price_kzt=50, quantity_total=10)
    new_code(api, owner, event, new_campaign(api, owner, event, discount_value=1), "TINY")

    response = checkout(api, login(), event, cheap, 1, promo="TINY")

    assert response.status_code == 409
    assert "no discount" in response.json()["detail"]


def test_a_campaign_of_another_organizer_is_not_reachable_through_my_event(
    api: TestClient,
    make_organizer: Callable[[], Organizer],
    owner: Organizer,
    db_session: Session,
    event: Event,
) -> None:
    other = make_organizer()
    other_event = create_event(db_session, other.profile)
    theirs = new_campaign(api, other, other_event)

    response = api.post(
        f"/events/{event.id}/campaigns/{theirs['id']}/codes", json={}, headers=owner.headers
    )

    assert response.status_code == 404


def test_codes_are_closed_on_a_cancelled_event(
    api: TestClient, owner: Organizer, db_session: Session, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    code = new_code(api, owner, event, campaign, "SPRING20")
    event.status = EventStatus.CANCELLED
    db_session.flush()
    base = f"/events/{event.id}/campaigns/{campaign['id']}/codes"

    new = api.post(base, json={}, headers=owner.headers)
    on = api.patch(f"{base}/{code['id']}", json={"status": "active"}, headers=owner.headers)
    off = api.patch(f"{base}/{code['id']}", json={"status": "disabled"}, headers=owner.headers)

    assert (new.status_code, on.status_code, off.status_code) == (409, 409, 200)


def test_a_campaign_that_has_not_started_is_not_redeemable_now(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    campaign = new_campaign(api, owner, event, starts_at=future)
    new_code(api, owner, event, campaign, "LATER")

    codes = api.get(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes", headers=owner.headers
    ).json()

    assert (codes[0]["status"], codes[0]["redeemable_now"]) == ("active", False)


def test_a_taken_code_does_not_say_whose_it_is(
    api: TestClient,
    make_organizer: Callable[[], Organizer],
    owner: Organizer,
    db_session: Session,
    event: Event,
) -> None:
    other = make_organizer()
    other_event = create_event(db_session, other.profile)
    new_code(api, other, other_event, new_campaign(api, other, other_event), "SPRING20")
    mine = new_campaign(api, owner, event)

    response = api.post(
        f"/events/{event.id}/campaigns/{mine['id']}/codes",
        json={"code": "spring20"},
        headers=owner.headers,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "This promo code is not available, choose another"}


# --- audit (BE-19 timeline) -----------------------------------------------------------------


def test_promo_actions_show_up_in_the_event_timeline(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    code = new_code(api, owner, event, campaign, "SPRING20")
    url = f"/events/{event.id}/campaigns/{campaign['id']}/codes/{code['id']}"
    api.patch(url, json={"status": "disabled"}, headers=owner.headers)
    api.patch(url, json={"status": "disabled"}, headers=owner.headers)  # no change, no row
    api.patch(url, json={"status": "active"}, headers=owner.headers)

    page = api.get(
        f"/events/{event.id}/timeline", params={"action_type": "promo."}, headers=owner.headers
    ).json()

    # Not compared in order: the test session is one transaction, so every row has the same
    # created_at and "oldest first" has nothing to sort by here.
    by_action = {item["action_type"]: item for item in page["items"]}
    assert page["total"] == 4  # the repeated "disabled" wrote nothing
    assert sorted(by_action) == [
        "promo.campaign_created",
        "promo.code_created",
        "promo.code_disabled",
        "promo.code_enabled",
    ]
    created = by_action["promo.code_created"]
    assert created["entity_id"] == code["id"]
    assert created["details"] == {"campaign_id": campaign["id"], "code": "SPRING20"}
    assert created["actor_user_id"] is not None


def test_a_failed_duplicate_code_leaves_no_audit_row(
    api: TestClient, owner: Organizer, event: Event
) -> None:
    campaign = new_campaign(api, owner, event)
    new_code(api, owner, event, campaign, "SPRING20")
    api.post(
        f"/events/{event.id}/campaigns/{campaign['id']}/codes",
        json={"code": "SPRING20"},
        headers=owner.headers,
    )

    page = api.get(
        f"/events/{event.id}/timeline",
        params={"action_type": "promo.code_created"},
        headers=owner.headers,
    ).json()

    assert page["total"] == 1
