"""GET /events/{event_id}/orders (BE-30): the organizer's list of an event's orders and guests."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.db import engine, get_session
from app.main import app
from app.modules.events.models import Event
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.models import CheckInRecord
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders_service
from app.modules.orders.enums import TicketStatus
from app.modules.orders.models import Order, Ticket, TicketType
from app.modules.orders.schemas import CheckoutCreate, OrderOut, RegistrationCreate
from app.modules.payments import service as payments_service
from app.modules.payments.schemas import PayOrderIn, RefundIn
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
PRICE = 5000


def _headers(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def organizer(db_session: Session) -> OrganizerProfile:
    return create_organizer(db_session)


@pytest.fixture
def owner(db_session: Session, organizer: OrganizerProfile) -> User:
    return db_session.get_one(User, organizer.user_id)


def _paid_event(session: Session, organizer: OrganizerProfile) -> Event:
    event = create_event(session, organizer)
    event.paid_sales_active = True
    session.flush()
    return event


@pytest.fixture
def event(db_session: Session, organizer: OrganizerProfile) -> Event:
    return _paid_event(db_session, organizer)


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event, quantity_total=200)


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, price_kzt=PRICE, quantity_total=200)


def buyer_with_email(session: Session, email: str | None = None) -> User:
    user = create_user(session)
    if email is not None:
        user.email = email
        session.flush()
    return user


def _body(ticket_type: TicketType, guests: list[tuple[str, str | None]]) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": len(guests)}],
        "attendees": [{"name": name, "email": email} for name, email in guests],
    }


def register(
    session: Session, buyer: User, ticket_type: TicketType, guests: list[tuple[str, str | None]]
) -> OrderOut:
    data = RegistrationCreate.model_validate(_body(ticket_type, guests))
    return orders_service.register_free(session, buyer, ticket_type.event_id, data)


def checkout(
    session: Session,
    buyer: User,
    ticket_type: TicketType,
    guests: list[tuple[str, str | None]],
    promo: str | None = None,
) -> OrderOut:
    data = CheckoutCreate.model_validate(_body(ticket_type, guests) | {"promo_code": promo})
    return orders_service.checkout(session, buyer, ticket_type.event_id, data)


def buy(
    session: Session,
    buyer: User,
    ticket_type: TicketType,
    guests: list[tuple[str, str | None]],
    promo: str | None = None,
) -> OrderOut:
    pending = checkout(session, buyer, ticket_type, guests, promo)
    return payments_service.pay_order(
        session, buyer, pending.id, PayOrderIn(simulate="success")
    ).order


def check_in(session: Session, admin: User, ticket_id: uuid.UUID) -> None:
    session.get_one(Ticket, ticket_id).status = TicketStatus.CHECKED_IN
    session.add(CheckInRecord(ticket_id=ticket_id, event_admin_id=admin.id))
    session.flush()


def promo_code(api: TestClient, owner: User, event: Event, code: str, percent: int = 20) -> None:
    campaign = api.post(
        f"/events/{event.id}/campaigns",
        json={"name": "Spring", "discount_type": "percent", "discount_value": percent},
        headers=_headers(owner),
    )
    assert campaign.status_code == 201, campaign.text
    created = api.post(
        f"/events/{event.id}/campaigns/{campaign.json()['id']}/codes",
        json={"code": code},
        headers=_headers(owner),
    )
    assert created.status_code == 201, created.text


def list_orders(api: TestClient, who: User, event: Event, **params: object):
    return api.get(f"/events/{event.id}/orders", params=params, headers=_headers(who))


def page(api: TestClient, who: User, event: Event, **params: object) -> dict:
    response = list_orders(api, who, event, **params)
    assert response.status_code == 200, response.text
    return response.json()


def by_id(body: dict) -> dict[str, dict]:
    return {item["id"]: item for item in body["items"]}


def start_event_in_the_past(session: Session, event: Event) -> None:
    event.start_at = datetime.now(UTC) - timedelta(hours=1)
    session.flush()


# --- who may see it ---------------------------------------------------------------------------


def test_owner_sees_all_orders_of_the_event_and_none_of_another(
    api: TestClient,
    db_session: Session,
    organizer: OrganizerProfile,
    owner: User,
    event: Event,
    free_type: TicketType,
    paid_type: TicketType,
) -> None:
    free = register(db_session, create_user(db_session), free_type, [("Ann", None)])
    paid = buy(db_session, create_user(db_session), paid_type, [("Bob", None)])
    pending = checkout(db_session, create_user(db_session), paid_type, [("Cid", None)])
    # Another event of the same organizer, and an event of another organizer.
    mine_too = _paid_event(db_session, organizer)
    register(
        db_session,
        create_user(db_session),
        create_free_ticket_type(db_session, mine_too),
        [("Elsewhere", None)],
    )
    foreign = _paid_event(db_session, create_organizer(db_session))
    register(
        db_session,
        create_user(db_session),
        create_free_ticket_type(db_session, foreign),
        [("Foreign", None)],
    )

    body = page(api, owner, event)

    assert body["total"] == 3
    assert set(by_id(body)) == {str(free.id), str(paid.id), str(pending.id)}
    assert (body["limit"], body["offset"]) == (20, 0)


def test_who_gets_what(
    api: TestClient, db_session: Session, owner: User, event: Event, free_type: TicketType
) -> None:
    register(db_session, create_user(db_session), free_type, [("Ann", None)])
    other_organizer = db_session.get_one(User, create_organizer(db_session).user_id)
    regular_user = create_user(db_session)
    path = f"/events/{event.id}/orders"

    assert api.get(path).status_code == 401  # no token
    assert list_orders(api, regular_user, event).status_code == 403  # not an organizer
    assert list_orders(api, regular_user, event).json() == {"detail": "Organizer account required"}
    response = list_orders(api, other_organizer, event)  # someone else's event: not revealed
    assert (response.status_code, response.json()) == (404, {"detail": "Event not found"})
    unknown = api.get(f"/events/{uuid.uuid4()}/orders", headers=_headers(owner))
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "Event not found"})


@pytest.mark.parametrize(
    "params", [{"status": "bogus"}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"q": "x" * 101}]
)
def test_invalid_parameters_are_422(
    api: TestClient, owner: User, event: Event, params: dict
) -> None:
    assert list_orders(api, owner, event, **params).status_code == 422


# --- what an order shows ----------------------------------------------------------------------


def test_order_item_shape_guests_tickets_and_check_in(
    api: TestClient,
    db_session: Session,
    owner: User,
    event: Event,
    free_type: TicketType,
    paid_type: TicketType,
) -> None:
    buyer = buyer_with_email(db_session, "dana@example.com")
    free = register(
        db_session, buyer, free_type, [("Zed Guest", "zed@example.com"), ("Amy Guest", None)]
    )
    zed_ticket = next(t for t in free.tickets if t.attendee.name == "Zed Guest")
    check_in(db_session, owner, zed_ticket.id)
    pending = checkout(db_session, buyer, paid_type, [("Pat Pending", "pat@example.com")])

    items = by_id(page(api, owner, event))

    item = items[str(free.id)]
    assert {k: item[k] for k in item if k != "guests"} == {
        "id": str(free.id),
        "status": "free_confirmed",
        "buyer_email": "dana@example.com",
        "total_kzt": 0,
        "discount_kzt": 0,
        "promo_code": None,
        "created_at": item["created_at"],
        "hold_expired": False,
        "can_refund": False,
        # A guest was already admitted, so the cancel action would refuse.
        "can_cancel": False,
    }
    amy, zed = item["guests"]  # sorted by ticket type, then name
    assert (amy["name"], amy["email"], amy["ticket"]["status"]) == ("Amy Guest", None, "valid")
    assert amy["ticket"]["checked_in_at"] is None
    assert (zed["name"], zed["email"], zed["ticket"]["status"]) == (
        "Zed Guest",
        "zed@example.com",
        "checked_in",
    )
    assert zed["ticket"]["id"] == str(zed_ticket.id)
    assert zed["ticket"]["checked_in_at"] is not None
    assert {g["ticket_type_id"] for g in item["guests"]} == {str(free_type.id)}
    assert {g["ticket_type_name"] for g in item["guests"]} == {free_type.name}
    assert "qr_token" not in zed["ticket"]  # the list never hands out entry credentials

    # A paid order issues tickets only on payment: the guest is there, the ticket is not yet.
    [pat] = items[str(pending.id)]["guests"]
    assert (pat["name"], pat["ticket_type_name"], pat["ticket"]) == (
        "Pat Pending",
        paid_type.name,
        None,
    )


def test_discount_and_promo_code_of_an_order_paid_with_a_code(
    api: TestClient, db_session: Session, owner: User, event: Event, paid_type: TicketType
) -> None:
    promo_code(api, owner, event, "SAVE20", percent=20)
    with_code = buy(db_session, create_user(db_session), paid_type, [("A", None)], "save20")
    without = buy(db_session, create_user(db_session), paid_type, [("B", None)])

    items = by_id(page(api, owner, event))

    assert (
        items[str(with_code.id)]["total_kzt"],
        items[str(with_code.id)]["discount_kzt"],
        items[str(with_code.id)]["promo_code"],
    ) == (PRICE, PRICE // 5, "SAVE20")
    assert (items[str(without.id)]["discount_kzt"], items[str(without.id)]["promo_code"]) == (
        0,
        None,
    )


def test_hold_expired_marks_an_abandoned_checkout_but_keeps_its_status(
    api: TestClient, db_session: Session, owner: User, event: Event, paid_type: TicketType
) -> None:
    fresh = checkout(db_session, create_user(db_session), paid_type, [("Fresh", None)])
    old = checkout(db_session, create_user(db_session), paid_type, [("Old", None)])
    long_ago = (
        datetime.now(UTC) - orders_repository.checkout_hold(db_session) - timedelta(minutes=1)
    )
    db_session.execute(update(Order).where(Order.id == old.id).values(created_at=long_ago))
    db_session.expire_all()

    items = by_id(page(api, owner, event))

    assert (items[str(fresh.id)]["status"], items[str(fresh.id)]["hold_expired"]) == (
        "pending",
        False,
    )
    assert (items[str(old.id)]["status"], items[str(old.id)]["hold_expired"]) == ("pending", True)


# --- can_refund / can_cancel agree with the real actions ----------------------------------------


def _orders_in_every_state(
    session: Session, owner: User, free_type: TicketType, paid_type: TicketType
) -> dict[str, uuid.UUID]:
    buyer = create_user(session)
    used = register(session, buyer, free_type, [("Used", None)])
    check_in(session, owner, used.tickets[0].id)
    cancelled = register(session, buyer, free_type, [("Gone", None)])
    orders_service.cancel_order(session, buyer, cancelled.id)
    refunded = buy(session, buyer, paid_type, [("Back", None)])
    payments_service.refund_order(session, owner, refunded.id, RefundIn(reason=None))
    return {
        "free": register(session, buyer, free_type, [("Free", None)]).id,
        "free_checked_in": used.id,
        "paid": buy(session, buyer, paid_type, [("Paid", None)]).id,
        "pending": checkout(session, buyer, paid_type, [("Pending", None)]).id,
        # The organizer bought a ticket to their own event: they are its buyer.
        "own_pending": checkout(session, owner, paid_type, [("Mine", None)]).id,
        "cancelled": cancelled.id,
        "refunded": refunded.id,
    }


@pytest.mark.parametrize("started", [False, True], ids=["upcoming", "started"])
def test_can_refund_agrees_with_post_refund(
    api: TestClient,
    db_session: Session,
    owner: User,
    event: Event,
    free_type: TicketType,
    paid_type: TicketType,
    started: bool,
) -> None:
    orders = _orders_in_every_state(db_session, owner, free_type, paid_type)
    if started:
        start_event_in_the_past(db_session, event)

    flags = {
        name: by_id(page(api, owner, event))[str(oid)]["can_refund"] for name, oid in orders.items()
    }

    # Only paid orders; refunds have no time rule, so after the start as well.
    assert flags == {name: name == "paid" for name in orders}
    for name, order_id in orders.items():
        response = api.post(f"/orders/{order_id}/refund", json={}, headers=_headers(owner))
        assert response.status_code == (200 if flags[name] else 409), (name, response.text)


@pytest.mark.parametrize("started", [False, True], ids=["upcoming", "started"])
def test_can_cancel_agrees_with_post_cancel(
    api: TestClient,
    db_session: Session,
    owner: User,
    event: Event,
    free_type: TicketType,
    paid_type: TicketType,
    started: bool,
) -> None:
    orders = _orders_in_every_state(db_session, owner, free_type, paid_type)
    if started:
        start_event_in_the_past(db_session, event)

    flags = {
        name: by_id(page(api, owner, event))[str(oid)]["can_cancel"] for name, oid in orders.items()
    }

    expected = set() if started else {"free", "own_pending"}
    assert {name for name, can in flags.items() if can} == expected
    for name, order_id in orders.items():
        response = api.post(f"/orders/{order_id}/cancel", headers=_headers(owner))
        assert response.status_code == (200 if flags[name] else 409), (name, response.text)


# --- search, filter, pages ----------------------------------------------------------------------


@pytest.fixture
def searchable(
    db_session: Session, free_type: TicketType, paid_type: TicketType
) -> dict[str, OrderOut]:
    alice = buyer_with_email(db_session, "Alice.Buyer@Example.com")
    bob = buyer_with_email(db_session, "bob@example.com")
    return {
        "alice": register(
            db_session,
            alice,
            free_type,
            # Two guests that both match "guest": the order must still appear once.
            [("Guest One", "one@guest.kz"), ("Guest Two", "two@guest.kz")],
        ),
        "bob": register(db_session, bob, free_type, [("Carol", "carol@mail.kz")]),
        "bob_paid": buy(db_session, bob, paid_type, [("Dave", None)]),
    }


def _ids(body: dict) -> set[str]:
    return {item["id"] for item in body["items"]}


def test_search_by_buyer_email_partial_and_any_case(
    api: TestClient, owner: User, event: Event, searchable: dict[str, OrderOut]
) -> None:
    body = page(api, owner, event, q="  alice.BUYER@ex ")

    assert (_ids(body), body["total"]) == ({str(searchable["alice"].id)}, 1)


def test_search_by_guest_email_and_name(
    api: TestClient, owner: User, event: Event, searchable: dict[str, OrderOut]
) -> None:
    assert _ids(page(api, owner, event, q="carol@mail")) == {str(searchable["bob"].id)}
    assert _ids(page(api, owner, event, q="dave")) == {str(searchable["bob_paid"].id)}
    by_guest = page(api, owner, event, q="guest")  # two matching guests, one order
    assert (by_guest["total"], len(by_guest["items"])) == (1, 1)


def test_search_by_full_order_id_and_short_prefix(
    api: TestClient, owner: User, event: Event, searchable: dict[str, OrderOut]
) -> None:
    order_id = str(searchable["bob_paid"].id)

    assert _ids(page(api, owner, event, q=order_id)) == {order_id}
    assert _ids(page(api, owner, event, q=order_id.upper())) == {order_id}
    # The web shows the first 8 hex digits upper-case as the order number.
    short = page(api, owner, event, q=order_id[:8].upper())
    assert order_id in _ids(short)
    assert all(item_id.startswith(order_id[:8]) for item_id in _ids(short))


def test_no_match_is_an_empty_page_and_like_wildcards_are_literal(
    api: TestClient, owner: User, event: Event, searchable: dict[str, OrderOut]
) -> None:
    for q in ["nobody-here", "%", "_", "%@%"]:
        body = page(api, owner, event, q=q)
        assert (body["items"], body["total"]) == ([], 0), q
    # An empty (or blank) q is no filter at all.
    assert page(api, owner, event, q="   ")["total"] == 3


def test_status_filter_pages_and_total(
    api: TestClient, db_session: Session, owner: User, event: Event, free_type: TicketType
) -> None:
    orders = [
        register(db_session, create_user(db_session), free_type, [(f"G{n}", None)])
        for n in range(5)
    ]
    cancelled = orders[1]
    orders_service.cancel_order(db_session, owner, cancelled.id)
    # Different creation times (one test transaction gives every row the same now()).
    base = datetime.now(UTC) - timedelta(hours=1)
    for n, order in enumerate(orders):
        db_session.execute(
            update(Order).where(Order.id == order.id).values(created_at=base + timedelta(minutes=n))
        )
    db_session.expire_all()
    newest_first = [str(order.id) for order in reversed(orders)]

    everything = page(api, owner, event)
    assert [item["id"] for item in everything["items"]] == newest_first
    second_page = page(api, owner, event, limit=2, offset=2)
    assert [item["id"] for item in second_page["items"]] == newest_first[2:4]
    assert (second_page["total"], second_page["limit"], second_page["offset"]) == (5, 2, 2)
    past_the_end = page(api, owner, event, offset=10)
    assert (past_the_end["items"], past_the_end["total"]) == ([], 5)

    confirmed = page(api, owner, event, status="free_confirmed")
    assert confirmed["total"] == 4 and str(cancelled.id) not in _ids(confirmed)
    only_cancelled = page(api, owner, event, status="cancelled")
    assert (_ids(only_cancelled), only_cancelled["total"]) == ({str(cancelled.id)}, 1)
    assert page(api, owner, event, status="refunded")["total"] == 0


# --- queries ------------------------------------------------------------------------------------


def _count_queries(call: Callable[[], dict]) -> tuple[int, dict]:
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        body = call()
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    return len(statements), body


def test_listing_10_orders_runs_as_many_queries_as_2(
    api: TestClient, db_session: Session, organizer: OrganizerProfile, owner: User
) -> None:
    counts = []
    for n in (2, 10):
        event = _paid_event(db_session, organizer)
        free_type = create_free_ticket_type(db_session, event, quantity_total=200)
        paid_type = create_ticket_type(db_session, event, price_kzt=PRICE, quantity_total=200)
        promo_code(api, owner, event, f"Q{n}CODE")
        for i in range(n):
            buyer = create_user(db_session)  # a different buyer for every order
            guests = [(f"Guest {i}-{g}", f"g{i}-{g}@example.com") for g in range(3)]
            if i % 2:
                order = buy(db_session, buyer, paid_type, guests, promo=f"Q{n}CODE")
            else:
                order = register(db_session, buyer, free_type, guests)
            check_in(db_session, owner, order.tickets[0].id)

        queries, body = _count_queries(lambda event=event: page(api, owner, event, limit=100))

        assert body["total"] == n and len(body["items"]) == n
        assert all(len(item["guests"]) == 3 for item in body["items"])
        assert sum(item["promo_code"] is not None for item in body["items"]) == n // 2
        assert all(
            any(g["ticket"]["checked_in_at"] for g in item["guests"]) for item in body["items"]
        )
        counts.append(queries)

    assert counts[0] == counts[1], counts
