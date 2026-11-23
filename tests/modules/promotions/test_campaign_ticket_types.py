"""Campaigns limited to some ticket types (BE-29), over HTTP on the rolled-back session.

The discount comes only from the matching items; a code that matches none of the chosen
tickets is a 409 at check and at checkout, and a refused checkout leaves nothing behind.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile
from app.modules.orders import service as orders_service
from app.modules.orders.models import Order, OrderItem, TicketType
from app.modules.promotions.models import CampaignTicketType, PromoRedemption, PromotionalCampaign
from tests.emails import confirm_email
from tests.factories import create_event, create_ticket_type
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
NOT_APPLICABLE = "This promo code does not apply to the selected tickets"
# The 409 body: the same English detail plus the machine-readable code (#35 DomainError.code).
NOT_APPLICABLE_BODY = {"detail": NOT_APPLICABLE, "code": "code_not_applicable"}


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
        confirm_email(api, email)  # #35: buying needs a confirmed email
        token = api.post("/auth/login", data={"username": email, "password": password}).json()[
            "access_token"
        ]
        return {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def owner(api: TestClient, login: Callable[[], Headers], db_session: Session) -> Organizer:
    headers = login()
    response = api.post("/organizer/profile", json={"display_name": "Org"}, headers=headers)
    assert response.status_code == 201, response.text
    return Organizer(headers, db_session.get_one(OrganizerProfile, response.json()["id"]))


@pytest.fixture
def event(db_session: Session, owner: Organizer) -> Event:
    event = create_event(db_session, owner.profile)
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def vip(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, name="VIP", price_kzt=10000, quantity_total=50)


@pytest.fixture
def standard(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, name="Standard", price_kzt=5000, quantity_total=50)


def campaign(api: TestClient, owner: Organizer, event: Event, **overrides: object):
    body = {"name": "VIP only", "discount_type": "percent", "discount_value": 20} | overrides
    return api.post(f"/events/{event.id}/campaigns", json=body, headers=owner.headers)


def code_for(api: TestClient, owner: Organizer, event: Event, **overrides: object) -> str:
    created = campaign(api, owner, event, **overrides)
    assert created.status_code == 201, created.text
    code = api.post(
        f"/events/{event.id}/campaigns/{created.json()['id']}/codes", json={}, headers=owner.headers
    )
    assert code.status_code == 201, code.text
    return code.json()["code"]


def lines(*pairs: tuple[TicketType, int]) -> list[dict]:
    return [{"ticket_type_id": str(tt.id), "quantity": qty} for tt, qty in pairs]


def checkout(api: TestClient, headers: Headers, event: Event, items: list[dict], promo: str):
    count = sum(i["quantity"] for i in items)
    body = {
        "items": items,
        "attendees": [{"name": f"Guest {n}"} for n in range(count)],
        "promo_code": promo,
    }
    return api.post(f"/events/{event.id}/checkout", json=body, headers=headers)


def check(api: TestClient, headers: Headers, event: Event, code: str, items=None):
    body = {"code": code} | ({"items": items} if items is not None else {})
    return api.post(f"/events/{event.id}/promo-codes/check", json=body, headers=headers)


def ids(*types: TicketType) -> list[str]:
    return sorted(str(t.id) for t in types)


# --- the campaign's ticket types ---


def test_campaign_with_its_own_types_order_independent(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType, standard: TicketType
) -> None:
    created = campaign(api, owner, event, ticket_type_ids=[str(standard.id), str(vip.id)])

    assert created.status_code == 201, created.text
    assert created.json()["ticket_type_ids"] == ids(vip, standard)
    [listed] = api.get(f"/events/{event.id}/campaigns", headers=owner.headers).json()
    assert listed["ticket_type_ids"] == ids(vip, standard)


def test_duplicate_ids_are_collapsed(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType, db_session: Session
) -> None:
    created = campaign(api, owner, event, ticket_type_ids=[str(vip.id)] * 3)

    assert created.status_code == 201 and created.json()["ticket_type_ids"] == [str(vip.id)]
    rows = db_session.scalars(
        select(CampaignTicketType).where(CampaignTicketType.campaign_id == created.json()["id"])
    ).all()
    assert len(rows) == 1


@pytest.mark.parametrize("body", [{}, {"ticket_type_ids": []}])
def test_empty_or_omitted_means_all_types(
    api: TestClient, owner: Organizer, event: Event, body: dict
) -> None:
    created = campaign(api, owner, event, **body)

    assert created.status_code == 201 and created.json()["ticket_type_ids"] == []


def test_hidden_types_may_be_targeted(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    hidden = create_ticket_type(db_session, event, name="Friends", price_kzt=3000, is_hidden=True)

    created = campaign(api, owner, event, ticket_type_ids=[str(hidden.id)])

    assert created.status_code == 201 and created.json()["ticket_type_ids"] == [str(hidden.id)]


def test_another_events_type_is_422(
    api: TestClient, owner: Organizer, event: Event, db_session: Session
) -> None:
    other = create_ticket_type(db_session, create_event(db_session, owner.profile), name="X")

    response = campaign(api, owner, event, ticket_type_ids=[str(other.id)])

    assert response.status_code == 422
    assert str(other.id) in response.json()["detail"]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(PromotionalCampaign)
            .where(PromotionalCampaign.event_id == event.id)
        )
        == 0
    )  # nothing half-created


def test_unknown_type_is_422(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType
) -> None:
    ghost = uuid.uuid4()

    response = campaign(api, owner, event, ticket_type_ids=[str(vip.id), str(ghost)])

    assert response.status_code == 422
    assert response.json()["detail"] == (
        f"ticket_type_ids must be ticket types of this event; not found: {ghost}"
    )


def test_list_campaigns_has_no_query_per_campaign(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType, standard: TicketType
) -> None:
    from sqlalchemy import event as sa_event

    from app.core.db import engine

    def count_list() -> int:
        n: list[int] = []
        hook = lambda *a: n.append(1)  # noqa: E731
        sa_event.listen(engine, "before_cursor_execute", hook)
        try:
            assert (
                api.get(f"/events/{event.id}/campaigns", headers=owner.headers).status_code == 200
            )
        finally:
            sa_event.remove(engine, "before_cursor_execute", hook)
        return len(n)

    campaign(api, owner, event, name="a", ticket_type_ids=[str(vip.id)])
    few = count_list()
    for i in range(6):
        campaign(api, owner, event, name=f"b{i}", ticket_type_ids=[str(standard.id), str(vip.id)])
    assert count_list() == few


# --- the discount on a mixed order (2 VIP x 10 000 + 1 Standard x 5 000 = 25 000) ---


@pytest.mark.parametrize(
    ("discount", "expected"),
    [
        ({"discount_type": "percent", "discount_value": 20}, 4000),  # 20% of 20 000 only
        ({"discount_type": "fixed", "discount_value": 3000}, 3000),
        ({"discount_type": "fixed", "discount_value": 22000}, 20000),  # capped at the VIP part
    ],
)
def test_discount_only_from_matching_items(
    api: TestClient,
    owner: Organizer,
    event: Event,
    vip: TicketType,
    standard: TicketType,
    login: Callable[[], Headers],
    discount: dict,
    expected: int,
) -> None:
    code = code_for(api, owner, event, ticket_type_ids=[str(vip.id)], **discount)
    items = lines((vip, 2), (standard, 1))
    buyer = login()

    checked = check(api, buyer, event, code, items).json()
    order = checkout(api, buyer, event, items, code)

    assert (checked["eligible_subtotal_kzt"], checked["discount_kzt"]) == (20000, expected)
    assert checked["ticket_type_ids"] == [str(vip.id)]
    assert order.status_code == 201, order.text
    assert (order.json()["total_kzt"], order.json()["discount_kzt"]) == (25000, expected)


def test_all_types_campaign_is_unchanged(
    api: TestClient,
    owner: Organizer,
    event: Event,
    vip: TicketType,
    standard: TicketType,
    login: Callable[[], Headers],
) -> None:
    code = code_for(api, owner, event, discount_type="percent", discount_value=20)
    items = lines((vip, 2), (standard, 1))
    buyer = login()

    checked = check(api, buyer, event, code, items).json()
    order = checkout(api, buyer, event, items, code).json()

    # 20% of the whole 25 000, as before BE-29.
    assert (checked["eligible_subtotal_kzt"], checked["discount_kzt"]) == (25000, 5000)
    assert order["discount_kzt"] == 5000 and checked["ticket_type_ids"] == []


# --- check with and without items ---


def test_check_without_items_is_unchanged_plus_ticket_type_ids(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType, login: Callable[[], Headers]
) -> None:
    code = code_for(api, owner, event, ticket_type_ids=[str(vip.id)])

    response = check(api, login(), event, code)

    assert response.status_code == 200
    assert response.json() == {
        "code": code,
        "campaign_name": "VIP only",
        "discount_type": "percent",
        "discount_value": 20,
        "ticket_type_ids": [str(vip.id)],
    }


def test_check_with_no_matching_item_is_409(
    api: TestClient,
    owner: Organizer,
    event: Event,
    vip: TicketType,
    standard: TicketType,
    login: Callable[[], Headers],
) -> None:
    code = code_for(api, owner, event, ticket_type_ids=[str(vip.id)])

    response = check(api, login(), event, code, lines((standard, 2)))

    assert (response.status_code, response.json()) == (409, NOT_APPLICABLE_BODY)


def test_check_item_of_another_event_is_422(
    api: TestClient,
    owner: Organizer,
    event: Event,
    vip: TicketType,
    login: Callable[[], Headers],
    db_session: Session,
) -> None:
    code = code_for(api, owner, event)
    other = create_ticket_type(db_session, create_event(db_session, owner.profile), name="X")

    response = check(api, login(), event, code, lines((other, 1)))

    assert response.status_code == 422


def test_check_same_type_twice_is_one_line(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType, login: Callable[[], Headers]
) -> None:
    code = code_for(api, owner, event, ticket_type_ids=[str(vip.id)])

    body = check(api, login(), event, code, lines((vip, 1), (vip, 2))).json()

    assert (body["eligible_subtotal_kzt"], body["discount_kzt"]) == (30000, 6000)


# --- checkout with a code that applies to nothing ---


def test_checkout_not_applicable_is_409_and_leaves_nothing(
    api: TestClient,
    owner: Organizer,
    event: Event,
    vip: TicketType,
    standard: TicketType,
    login: Callable[[], Headers],
    db_session: Session,
) -> None:
    code = code_for(api, owner, event, ticket_type_ids=[str(vip.id)], max_redemptions=1)
    buyer = login()

    def counts() -> tuple[int, int, int]:
        orders = db_session.scalar(
            select(func.count()).select_from(Order).where(Order.event_id == event.id)
        )
        redemptions = db_session.scalar(select(func.count()).select_from(PromoRedemption))
        held = orders_service.get_availability(db_session, [standard])[standard.id]
        return orders, redemptions, held

    before = counts()
    response = checkout(api, buyer, event, lines((standard, 2)), code)

    assert (response.status_code, response.json()) == (409, NOT_APPLICABLE_BODY)
    assert counts() == before  # no order, no redemption, no tickets held
    # The single use is still free: a matching checkout gets it.
    ok = checkout(api, buyer, event, lines((vip, 1)), code)
    assert ok.status_code == 201 and ok.json()["discount_kzt"] == 2000
    assert db_session.scalar(select(func.count()).select_from(OrderItem).join(Order).where(
        Order.event_id == event.id)) == 1  # fmt: skip


# --- duplicate does not copy campaigns ---


def test_duplicate_copies_no_campaigns_or_links(
    api: TestClient, owner: Organizer, event: Event, vip: TicketType, db_session: Session
) -> None:
    code_for(api, owner, event, ticket_type_ids=[str(vip.id)])

    copy = api.post(f"/events/{event.id}/duplicate", headers=owner.headers)

    assert copy.status_code == 201, copy.text
    copy_id = copy.json()["id"]
    assert api.get(f"/events/{copy_id}/campaigns", headers=owner.headers).json() == []
    copied_types = db_session.scalars(
        select(TicketType.id).where(TicketType.event_id == copy_id)
    ).all()
    assert copied_types  # the ticket types are copied...
    links = db_session.scalar(
        select(func.count())
        .select_from(CampaignTicketType)
        .where(CampaignTicketType.ticket_type_id.in_(copied_types))
    )
    assert links == 0  # ...but no campaign points at them


# --- only the not-applicable refusal carries a code ---


def test_other_promo_errors_have_no_code(
    api: TestClient,
    owner: Organizer,
    event: Event,
    vip: TicketType,
    login: Callable[[], Headers],
    db_session: Session,
) -> None:
    from app.modules.promotions.enums import PromoCodeStatus
    from app.modules.promotions.models import PromoCode

    buyer = login()
    unknown = check(api, buyer, event, "NOPE-NOPE", lines((vip, 1)))
    code = code_for(api, owner, event, ticket_type_ids=[str(vip.id)])
    db_session.query(PromoCode).filter(PromoCode.code == code).update(
        {PromoCode.status: PromoCodeStatus.DISABLED}
    )
    db_session.flush()
    disabled = check(api, buyer, event, code, lines((vip, 1)))
    disabled_at_checkout = checkout(api, buyer, event, lines((vip, 1)), code)

    assert unknown.status_code == 404 and "code" not in unknown.json()
    assert disabled.json() == {"detail": "This promo code is disabled"}
    assert disabled_at_checkout.status_code == 409 and "code" not in disabled_at_checkout.json()
