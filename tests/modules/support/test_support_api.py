"""Support cases and messages (BE-17) over HTTP, on the rolled-back test session.

Messages are not compared in order: the test session is one transaction, so rows written in
it share one created_at.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import OrganizerProfile, User
from app.modules.ops.models import Notification
from app.modules.orders import repository as orders_repository
from app.modules.orders.enums import OrderStatus, TicketKind
from app.modules.orders.models import Attendee, Order, OrderItem, Ticket
from tests.emails import RecordingEmailProvider
from tests.factories import create_event, create_ticket_type
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]


@dataclass
class Account:
    headers: Headers
    user: User
    organizer: OrganizerProfile | None = None


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


@pytest.fixture
def make_account(api: TestClient, db_session: Session) -> Callable[..., Account]:
    def make(*, organizer: bool = False, admin: bool = False) -> Account:
        email, password = f"user-{uuid.uuid4().hex[:12]}@example.com", "correct-horse-battery"
        assert (
            api.post("/auth/register", json={"email": email, "password": password}).status_code
            == 201
        )
        token = api.post("/auth/login", data={"username": email, "password": password}).json()[
            "access_token"
        ]
        headers = {"Authorization": f"Bearer {token}"}
        user = db_session.scalars(select(User).where(User.email == email)).one()
        profile = None
        if organizer:
            created = api.post("/organizer/profile", json={"display_name": "Org"}, headers=headers)
            assert created.status_code == 201, created.text
            profile = db_session.get_one(OrganizerProfile, created.json()["id"])
        if admin:
            user.system_role = SystemRole.PLATFORM_ADMIN
            db_session.flush()
        return Account(headers, user, profile)

    return make


@pytest.fixture
def organizer(make_account: Callable[..., Account]) -> Account:
    return make_account(organizer=True)


@pytest.fixture
def buyer(make_account: Callable[..., Account]) -> Account:
    return make_account()


@pytest.fixture
def admin(make_account: Callable[..., Account]) -> Account:
    return make_account(admin=True)


@pytest.fixture
def event(db_session: Session, organizer: Account) -> Event:
    assert organizer.organizer is not None
    return create_event(db_session, organizer.organizer)


def order_for(db_session: Session, buyer: Account, event: Event) -> tuple[Order, Ticket]:
    """A confirmed free order of the buyer with one issued ticket."""
    ticket_type = create_ticket_type(
        db_session, event, kind=TicketKind.FREE, price_kzt=0, quantity_total=10
    )
    order = Order(buyer_user_id=buyer.user.id, event_id=event.id, status=OrderStatus.FREE_CONFIRMED)
    item = OrderItem(ticket_type_id=ticket_type.id, quantity=1, unit_price_kzt=0)
    order.items.append(item)
    ticket = Ticket(attendee=Attendee(name="Guest", order=order), qr_token=uuid.uuid4().hex)
    item.tickets.append(ticket)
    orders_repository.add_order(db_session, order)
    return order, ticket


def open_case(api: TestClient, who: Account, **overrides: object) -> dict:
    body = {"kind": "attendee", "category": "ticket", "body": "Help please"} | overrides
    response = api.post("/support/cases", json=body, headers=who.headers)
    assert response.status_code == 201, response.text
    return response.json()


def attendee_case(api: TestClient, buyer: Account, event: Event, **overrides: object) -> dict:
    return open_case(api, buyer, event_id=str(event.id), **overrides)


def get_case(api: TestClient, who: Account, case_id: str):
    return api.get(f"/support/cases/{case_id}", headers=who.headers)


def say(api: TestClient, who: Account, case_id: str, body: str = "Hello"):
    return api.post(f"/support/cases/{case_id}/messages", json={"body": body}, headers=who.headers)


def set_status(api: TestClient, who: Account, case_id: str, status: str):
    return api.patch(
        f"/support/cases/{case_id}/status", json={"status": status}, headers=who.headers
    )


# --- opening a case -------------------------------------------------------------------------


def test_attendee_opens_a_case_about_an_event(
    api: TestClient, buyer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event, body="  Is there parking?  ")

    assert (case["kind"], case["status"], case["assignee_id"]) == ("attendee", "open", None)
    assert case["opened_by"] == str(buyer.user.id)
    assert case["event_id"] == str(event.id)
    assert [m["body"] for m in case["messages"]] == ["Is there parking?"]  # trimmed
    mine = api.get("/support/cases", headers=buyer.headers).json()
    assert [c["id"] for c in mine["items"]] == [case["id"]]


def test_an_attendee_case_needs_an_event_order_or_ticket(api: TestClient, buyer: Account) -> None:
    response = api.post(
        "/support/cases",
        json={"kind": "attendee", "category": "other", "body": "Hi"},
        headers=buyer.headers,
    )

    assert response.status_code == 422


def test_a_draft_event_cannot_be_asked_about(
    api: TestClient, buyer: Account, db_session: Session, organizer: Account
) -> None:
    assert organizer.organizer is not None
    draft = create_event(db_session, organizer.organizer, status=EventStatus.DRAFT)

    response = api.post(
        "/support/cases",
        json={"kind": "attendee", "category": "event", "body": "Hi", "event_id": str(draft.id)},
        headers=buyer.headers,
    )

    assert response.status_code == 404


def test_the_event_is_found_from_my_order_and_ticket(
    api: TestClient, buyer: Account, db_session: Session, event: Event
) -> None:
    order, ticket = order_for(db_session, buyer, event)

    by_order = open_case(api, buyer, category="payment", order_id=str(order.id))
    by_ticket = open_case(api, buyer, ticket_id=str(ticket.id))

    assert by_order["event_id"] == by_ticket["event_id"] == str(event.id)
    assert by_order["order_id"] == str(order.id)
    assert by_ticket["ticket_id"] == str(ticket.id)


def test_someone_elses_order_or_ticket_is_404(
    api: TestClient,
    make_account: Callable[..., Account],
    buyer: Account,
    db_session: Session,
    event: Event,
) -> None:
    other = make_account()
    order, ticket = order_for(db_session, other, event)

    for target in ({"order_id": str(order.id)}, {"ticket_id": str(ticket.id)}):
        response = api.post(
            "/support/cases",
            json={"kind": "attendee", "category": "ticket", "body": "Hi"} | target,
            headers=buyer.headers,
        )
        assert response.status_code == 404


def test_an_order_and_an_event_that_do_not_match_are_422(
    api: TestClient,
    buyer: Account,
    db_session: Session,
    organizer: Account,
    event: Event,
) -> None:
    assert organizer.organizer is not None
    order, _ = order_for(db_session, buyer, event)
    other_event = create_event(db_session, organizer.organizer)

    response = api.post(
        "/support/cases",
        json={
            "kind": "attendee",
            "category": "ticket",
            "body": "Hi",
            "order_id": str(order.id),
            "event_id": str(other_event.id),
        },
        headers=buyer.headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("body", ["", "   ", "x" * 5001])
def test_an_empty_or_huge_message_is_422(
    api: TestClient, buyer: Account, event: Event, body: str
) -> None:
    response = api.post(
        "/support/cases",
        json={"kind": "attendee", "category": "other", "body": body, "event_id": str(event.id)},
        headers=buyer.headers,
    )

    assert response.status_code == 422


def test_an_organizer_case_needs_an_organizer_account(api: TestClient, buyer: Account) -> None:
    response = api.post(
        "/support/cases",
        json={"kind": "organizer", "category": "account", "body": "Hi"},
        headers=buyer.headers,
    )

    assert response.status_code == 403


def test_an_organizer_opens_a_case_for_the_platform(
    api: TestClient, organizer: Account, event: Event
) -> None:
    plain = open_case(api, organizer, kind="organizer", category="account")
    about_own = open_case(api, organizer, kind="organizer", event_id=str(event.id))

    assert (plain["kind"], plain["event_id"]) == ("organizer", None)
    assert about_own["event_id"] == str(event.id)


def test_an_organizer_cannot_open_a_case_about_someone_elses_event(
    api: TestClient, make_account: Callable[..., Account], db_session: Session, event: Event
) -> None:
    stranger = make_account(organizer=True)

    response = api.post(
        "/support/cases",
        json={"kind": "organizer", "category": "event", "body": "Hi", "event_id": str(event.id)},
        headers=stranger.headers,
    )

    assert response.status_code == 404


# --- who can see a case ---------------------------------------------------------------------


def test_who_can_read_an_attendee_case(
    api: TestClient,
    make_account: Callable[..., Account],
    buyer: Account,
    organizer: Account,
    admin: Account,
    event: Event,
) -> None:
    case = attendee_case(api, buyer, event)
    other_organizer = make_account(organizer=True)
    random_user = make_account()

    assert get_case(api, buyer, case["id"]).status_code == 200
    assert get_case(api, organizer, case["id"]).status_code == 200  # owner of the event
    assert get_case(api, admin, case["id"]).status_code == 200
    assert get_case(api, other_organizer, case["id"]).status_code == 404
    assert get_case(api, random_user, case["id"]).status_code == 404
    assert api.get(f"/support/cases/{case['id']}").status_code == 401


def test_an_organizer_case_is_for_admins_only(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    case = open_case(api, organizer, kind="organizer", event_id=str(event.id))

    assert get_case(api, organizer, case["id"]).status_code == 200  # the opener
    assert get_case(api, admin, case["id"]).status_code == 200
    assert get_case(api, buyer, case["id"]).status_code == 404


def test_my_cases_are_only_mine_and_can_be_filtered(
    api: TestClient, make_account: Callable[..., Account], buyer: Account, event: Event
) -> None:
    mine = attendee_case(api, buyer, event)
    done = attendee_case(api, buyer, event)
    set_status(api, buyer, done["id"], "resolved")
    attendee_case(api, make_account(), event)  # somebody else's

    everything = api.get("/support/cases", headers=buyer.headers).json()
    resolved = api.get("/support/cases?status=resolved", headers=buyer.headers).json()

    assert everything["total"] == 2
    assert {c["id"] for c in everything["items"]} == {mine["id"], done["id"]}
    assert [c["id"] for c in resolved["items"]] == [done["id"]]


# --- the inbox ------------------------------------------------------------------------------


def test_an_organizer_inbox_has_only_attendee_cases_of_their_events(
    api: TestClient,
    make_account: Callable[..., Account],
    db_session: Session,
    buyer: Account,
    organizer: Account,
    event: Event,
) -> None:
    other_organizer = make_account(organizer=True)
    assert other_organizer.organizer is not None
    other_event = create_event(db_session, other_organizer.organizer)
    mine = attendee_case(api, buyer, event)
    attendee_case(api, buyer, other_event)  # another organizer's event
    open_case(api, organizer, kind="organizer", event_id=str(event.id))  # for the platform

    inbox = api.get("/support/inbox", headers=organizer.headers).json()

    assert inbox["total"] == 1
    assert [c["id"] for c in inbox["items"]] == [mine["id"]]


def test_an_admin_inbox_has_every_case_and_can_be_filtered(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    attendee_case(api, buyer, event)
    open_case(api, organizer, kind="organizer")

    everything = api.get("/support/inbox", headers=admin.headers).json()
    only_organizer = api.get("/support/inbox?kind=organizer", headers=admin.headers).json()
    page = api.get("/support/inbox?limit=1&offset=1", headers=admin.headers).json()

    assert everything["total"] >= 2
    assert [c["kind"] for c in only_organizer["items"]] == ["organizer"]
    assert (len(page["items"]), page["limit"], page["offset"]) == (1, 1, 1)


def test_a_plain_user_has_no_inbox(api: TestClient, buyer: Account) -> None:
    assert api.get("/support/inbox", headers=buyer.headers).status_code == 403


# --- messages and the status flow -----------------------------------------------------------


def test_the_conversation_moves_the_status(
    api: TestClient, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    # The customer writes again before anybody answers: still open and unassigned.
    assert say(api, buyer, case["id"], "Anyone?").status_code == 201
    first = get_case(api, buyer, case["id"]).json()
    assert (first["status"], first["assignee_id"]) == ("open", None)

    # The organizer answers: takes the case, which now waits for the customer.
    assert say(api, organizer, case["id"], "Yes, parking is free").status_code == 201
    answered = get_case(api, buyer, case["id"]).json()
    assert (answered["status"], answered["assignee_id"]) == (
        "waiting_customer",
        str(organizer.user.id),
    )

    # The customer replies: back to the handler.
    say(api, buyer, case["id"], "Thanks, one more thing")
    assert get_case(api, buyer, case["id"]).json()["status"] == "in_progress"

    # Resolved, then the customer comes back: reopened, not a new case.
    assert set_status(api, organizer, case["id"], "resolved").json()["status"] == "resolved"
    say(api, buyer, case["id"], "It happened again")
    final = get_case(api, buyer, case["id"]).json()
    assert final["status"] == "in_progress"
    assert sorted(m["body"] for m in final["messages"]) == sorted(
        [
            "Help please",
            "Anyone?",
            "Yes, parking is free",
            "Thanks, one more thing",
            "It happened again",
        ]
    )


def test_a_stranger_cannot_write_in_a_case(
    api: TestClient, make_account: Callable[..., Account], buyer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    assert say(api, make_account(organizer=True), case["id"]).status_code == 404
    assert say(api, make_account(), case["id"]).status_code == 404
    unauthenticated = api.post(f"/support/cases/{case['id']}/messages", json={"body": "x"})
    assert unauthenticated.status_code == 401


def test_a_handler_takes_a_case(
    api: TestClient, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    taken = api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers)

    assert taken.status_code == 200
    assert (taken.json()["status"], taken.json()["assignee_id"]) == (
        "in_progress",
        str(organizer.user.id),
    )


def test_the_customer_cannot_take_their_own_case(
    api: TestClient, buyer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    assert api.post(f"/support/cases/{case['id']}/assign", headers=buyer.headers).status_code == 403


def test_an_admin_can_take_over_but_an_organizer_cannot_take_from_an_admin(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)
    api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers)

    by_admin = api.post(f"/support/cases/{case['id']}/assign", headers=admin.headers)
    back = api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers)

    assert by_admin.json()["assignee_id"] == str(admin.user.id)
    assert back.status_code == 409


def test_status_rules(api: TestClient, buyer: Account, organizer: Account, event: Event) -> None:
    case = attendee_case(api, buyer, event)

    assert set_status(api, buyer, case["id"], "in_progress").status_code == 403
    assert set_status(api, buyer, case["id"], "open").status_code == 422
    assert set_status(api, organizer, case["id"], "open").status_code == 422
    # A handler changing the status takes the case.
    waiting = set_status(api, organizer, case["id"], "waiting_customer").json()
    assert (waiting["status"], waiting["assignee_id"]) == (
        "waiting_customer",
        str(organizer.user.id),
    )
    # The customer may close their own case.
    assert set_status(api, buyer, case["id"], "resolved").json()["status"] == "resolved"


def test_a_stranger_cannot_change_a_status(
    api: TestClient, make_account: Callable[..., Account], buyer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    assert set_status(api, make_account(organizer=True), case["id"], "resolved").status_code == 404


# --- audit ----------------------------------------------------------------------------------


def test_case_actions_show_up_in_the_event_timeline(
    api: TestClient, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)
    say(api, organizer, case["id"], "On it")  # takes the case and waits for the customer
    say(api, organizer, case["id"], "Still on it")  # no change, so no new audit row

    page = api.get(
        f"/events/{event.id}/timeline",
        params={"action_type": "support."},
        headers=organizer.headers,
    ).json()

    assert sorted(item["action_type"] for item in page["items"]) == [
        "support.case_assigned",
        "support.case_opened",
        "support.case_status_changed",
    ]
    changed = next(i for i in page["items"] if i["action_type"] == "support.case_status_changed")
    assert changed["entity_id"] == case["id"]
    assert changed["details"] == {"from": "open", "to": "waiting_customer"}


# --- notifications (BE-18) ------------------------------------------------------------------


def notification_types(db_session: Session, who: Account) -> list[str]:
    rows = db_session.scalars(select(Notification).where(Notification.user_id == who.user.id))
    return sorted(row.type.value for row in rows)


def support_notifications(db_session: Session, who: Account) -> list[str]:
    return [t for t in notification_types(db_session, who) if t.startswith("support_")]


def test_a_handler_reply_notifies_the_customer_once(
    api: TestClient,
    db_session: Session,
    buyer: Account,
    organizer: Account,
    event: Event,
    emails: RecordingEmailProvider,
) -> None:
    case = attendee_case(api, buyer, event)
    buyer_mails = len(emails.to(buyer.user.email))
    organizer_mails = len(emails.to(organizer.user.email))

    assert say(api, organizer, case["id"], "Parking is free").status_code == 201

    # The reply also flips the status to waiting_customer, but only the reply is sent.
    assert support_notifications(db_session, buyer) == ["support_reply"]
    assert support_notifications(db_session, organizer) == []
    assert len(emails.to(buyer.user.email)) == buyer_mails + 1
    assert len(emails.to(organizer.user.email)) == organizer_mails


def test_a_customer_message_notifies_nobody(
    api: TestClient,
    db_session: Session,
    buyer: Account,
    organizer: Account,
    event: Event,
    emails: RecordingEmailProvider,
) -> None:
    case = attendee_case(api, buyer, event)
    mails_before = len(emails.sent)

    say(api, buyer, case["id"], "Anyone there?")

    assert support_notifications(db_session, buyer) == []
    assert support_notifications(db_session, organizer) == []
    assert len(emails.sent) == mails_before


def test_a_status_change_by_a_handler_notifies_the_customer(
    api: TestClient, db_session: Session, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    set_status(api, organizer, case["id"], "in_progress")
    assert support_notifications(db_session, buyer) == ["support_status"]

    # The same status again changes nothing, so it sends nothing.
    set_status(api, organizer, case["id"], "in_progress")
    assert support_notifications(db_session, buyer) == ["support_status"]

    # A customer closing their own case is not told about their own action.
    set_status(api, buyer, case["id"], "resolved")
    assert support_notifications(db_session, buyer) == ["support_status"]
    assert support_notifications(db_session, organizer) == []


def test_taking_a_case_notifies_the_customer(
    api: TestClient, db_session: Session, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers)
    assert support_notifications(db_session, buyer) == ["support_status"]

    # Taking it again changes nothing.
    api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers)
    assert support_notifications(db_session, buyer) == ["support_status"]


def test_an_admin_reply_notifies_the_organizer_who_opened_the_case(
    api: TestClient, db_session: Session, organizer: Account, admin: Account
) -> None:
    case = open_case(api, organizer, kind="organizer", category="account")

    say(api, admin, case["id"], "We are looking into it")

    assert support_notifications(db_session, organizer) == ["support_reply"]
    assert support_notifications(db_session, admin) == []
