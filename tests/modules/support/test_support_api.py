"""Support cases and messages (BE-17) over HTTP, on the rolled-back test session.

Messages are not compared in order: the test session is one transaction, so rows written in
it share one created_at.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import engine, get_session
from app.main import app
from app.modules.events.enums import EventStatus, EventVisibility
from app.modules.events.models import Event
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import OrganizerProfile, User
from app.modules.ops.enums import SupportCategory, SupportKind
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


def test_support_emails_and_notifications_point_to_the_case(
    api: TestClient,
    db_session: Session,
    buyer: Account,
    organizer: Account,
    event: Event,
    emails: RecordingEmailProvider,
) -> None:
    case = attendee_case(api, buyer, event)
    case_url = f"https://ticketoff.store/support/cases/{case['id']}"

    api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers)  # status email
    say(api, organizer, case["id"], "Parking is free")  # reply email

    [status_mail, reply_mail] = emails.to(buyer.user.email)[-2:]
    for mail in (status_mail, reply_mail):
        assert case_url in mail.text
        assert f'href="{case_url}"' in mail.html
    # The in-app rows carry the case id; the web app builds the same /support/cases/<id> link.
    rows = db_session.scalars(
        select(Notification).where(Notification.user_id == buyer.user.id)
    ).all()
    support_rows = [r for r in rows if r.type.value.startswith("support_")]
    assert sorted(r.type.value for r in support_rows) == ["support_reply", "support_status"]
    assert all(r.payload == {"case_id": case["id"]} for r in support_rows)


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


# --- display fields: opener_email and event_title (FE-8) ---


def items(api: TestClient, who: Account, path: str, **params: object) -> list[dict]:
    response = api.get(path, params=params, headers=who.headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def by_id(rows: list[dict]) -> dict[str, dict]:
    return {row["id"]: row for row in rows}


# --- the fields on every case response ---


def test_my_cases_and_detail_show_opener_email_and_event_title(
    api: TestClient, buyer: Account, event: Event
) -> None:
    opened = attendee_case(api, buyer, event)

    assert (opened["opener_email"], opened["event_title"]) == (buyer.user.email, event.title)
    [mine] = items(api, buyer, "/support/cases")
    assert (mine["opener_email"], mine["event_title"]) == (buyer.user.email, event.title)
    detail = get_case(api, buyer, opened["id"]).json()
    assert (detail["opener_email"], detail["event_title"]) == (buyer.user.email, event.title)
    assert len(detail["messages"]) == 1


def test_organizer_inbox_shows_the_buyers_email_and_event_title(
    api: TestClient, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    [row] = items(api, organizer, "/support/inbox")
    assert row["id"] == case["id"]
    assert (row["opener_email"], row["event_title"]) == (buyer.user.email, event.title)


def test_admin_inbox_shows_both_kinds_and_null_title_without_event(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    attendee = attendee_case(api, buyer, event)
    # An organizer asking the platform about nothing in particular: no event.
    platform = open_case(api, organizer, kind="organizer", category="payment")
    assert platform["event_id"] is None and platform["event_title"] is None

    rows = by_id(items(api, admin, "/support/inbox", limit=100))
    assert rows[attendee["id"]]["opener_email"] == buyer.user.email
    assert rows[attendee["id"]]["event_title"] == event.title
    assert rows[platform["id"]]["opener_email"] == organizer.user.email
    assert rows[platform["id"]]["event_title"] is None
    detail = get_case(api, admin, platform["id"]).json()
    assert (detail["opener_email"], detail["event_title"]) == (organizer.user.email, None)


def test_assign_and_status_responses_carry_the_fields(
    api: TestClient, buyer: Account, organizer: Account, event: Event
) -> None:
    case = attendee_case(api, buyer, event)

    assigned = api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers).json()
    resolved = api.patch(
        f"/support/cases/{case['id']}/status",
        json={"status": "resolved"},
        headers=organizer.headers,
    ).json()

    for out in (assigned, resolved):
        assert (out["opener_email"], out["event_title"]) == (buyer.user.email, event.title)


def test_another_organizers_inbox_shows_nothing_of_it(
    api: TestClient, buyer: Account, event: Event, make_account: Callable[..., Account]
) -> None:
    attendee_case(api, buyer, event)
    stranger = make_account(organizer=True)

    assert items(api, stranger, "/support/inbox") == []


# --- no query per case ---


def _list_statements(api: TestClient, who: Account, path: str) -> int:
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        rows = items(api, who, path, limit=100)
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert all(r["opener_email"] and r["event_title"] for r in rows)
    return len(statements)


@pytest.mark.parametrize("path", ["/support/cases", "/support/inbox"])
def test_listing_10_cases_runs_as_many_queries_as_2(
    api: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    path: str,
) -> None:
    counts = []
    for n in (2, 10):
        org = make_account(organizer=True)
        me = make_account()
        assert org.organizer is not None
        for i in range(n):
            # Each case has its own event, and in the inbox its own opener too, so a load per
            # case (of the title or of the opener's email) would add statements. My own
            # cases all have me as the opener by definition.
            ev = create_event(db_session, org.organizer)
            ev.title = f"Event {i}"
            db_session.flush()
            attendee_case(api, me if path == "/support/cases" else make_account(), ev)
        db_session.expire_all()
        who = me if path == "/support/cases" else org
        counts.append(_list_statements(api, who, path))

    assert counts[0] == counts[1], f"{counts[0]} statements for 2 cases, {counts[1]} for 10"


# --- categories and event reports (BE-27) ---------------------------------------------------


def report(api: TestClient, who: Account, event: Event, **overrides: object):
    body = {"kind": "report", "category": "event", "body": "Looks like a scam"}
    body |= {"event_id": str(event.id)} | overrides
    return api.post("/support/cases", json=body, headers=who.headers)


def case_ids(api: TestClient, who: Account, path: str = "/support/inbox", **params: object):
    return {row["id"] for row in items(api, who, path, **params)}


@pytest.mark.parametrize("category", ["seating", "check_in", "technical"])
def test_the_new_categories_are_accepted(
    api: TestClient, buyer: Account, event: Event, category: str
) -> None:
    case = attendee_case(api, buyer, event, category=category)

    assert case["category"] == category


def test_an_organizer_case_may_use_the_new_categories(api: TestClient, organizer: Account) -> None:
    assert open_case(api, organizer, kind="organizer", category="technical")["kind"] == "organizer"


def test_the_database_has_every_kind_and_category(db_session: Session) -> None:
    """alembic check can't see enum values; this catches a missing ALTER TYPE."""

    def labels(type_name: str) -> set[str]:
        return set(
            db_session.scalars(
                text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
                    " WHERE t.typname = :name"
                ),
                {"name": type_name},
            ).all()
        )

    assert labels("supportkind") == {k.value for k in SupportKind}
    assert labels("supportcategory") == {c.value for c in SupportCategory}


def test_anyone_can_report_a_published_event_without_buying(
    api: TestClient, buyer: Account, event: Event
) -> None:
    response = report(api, buyer, event)

    assert response.status_code == 201, response.text
    case = response.json()
    assert (case["kind"], case["category"], case["status"]) == ("report", "event", "open")
    assert case["event_id"] == str(event.id) and case["opened_by"] == str(buyer.user.id)
    assert [m["body"] for m in case["messages"]] == ["Looks like a scam"]


def test_a_report_may_use_the_category_other(api: TestClient, buyer: Account, event: Event) -> None:
    assert report(api, buyer, event, category="other").status_code == 201


def test_a_report_needs_an_event_id(api: TestClient, buyer: Account) -> None:
    response = api.post(
        "/support/cases",
        json={"kind": "report", "category": "event", "body": "Scam"},
        headers=buyer.headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("category", ["payment", "refund", "ticket", "account", "seating"])
def test_a_report_only_takes_the_category_event_or_other(
    api: TestClient, buyer: Account, event: Event, category: str
) -> None:
    assert report(api, buyer, event, category=category).status_code == 422


def test_a_report_takes_no_order_or_ticket(
    api: TestClient, db_session: Session, buyer: Account, event: Event
) -> None:
    order, ticket = order_for(db_session, buyer, event)

    assert report(api, buyer, event, order_id=str(order.id)).status_code == 422
    assert report(api, buyer, event, ticket_id=str(ticket.id)).status_code == 422


def test_only_a_published_public_event_can_be_reported(
    api: TestClient, db_session: Session, buyer: Account, organizer: Account
) -> None:
    assert organizer.organizer is not None
    draft = create_event(db_session, organizer.organizer, status=EventStatus.DRAFT)
    private = create_event(db_session, organizer.organizer)
    private.visibility = EventVisibility.PRIVATE
    db_session.flush()
    unlisted = create_event(db_session, organizer.organizer)
    unlisted.visibility = EventVisibility.UNLISTED
    db_session.flush()

    assert report(api, buyer, draft).status_code == 404
    assert report(api, buyer, private).status_code == 404
    assert report(api, buyer, unlisted).status_code == 201
    unknown = api.post(
        "/support/cases",
        json={"kind": "report", "category": "event", "body": "x", "event_id": str(uuid.uuid4())},
        headers=buyer.headers,
    )
    assert unknown.status_code == 404


def test_the_events_organizer_never_sees_a_report(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    case = report(api, buyer, event).json()
    assert say(api, admin, case["id"], "We are looking into it").status_code == 201

    for params in ({}, {"kind": "report"}, {"kind": "attendee"}):
        assert case["id"] not in case_ids(api, organizer, **params)
    assert get_case(api, organizer, case["id"]).status_code == 404
    assert say(api, organizer, case["id"]).status_code == 404
    assert set_status(api, organizer, case["id"], "resolved").status_code == 404
    assert (
        api.post(f"/support/cases/{case['id']}/assign", headers=organizer.headers).status_code
        == 404
    )


def test_a_report_leaves_no_trace_in_the_organizers_event_timeline(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    case = report(api, buyer, event).json()
    say(api, admin, case["id"], "Checking")
    set_status(api, admin, case["id"], "resolved")

    timeline = api.get(f"/events/{event.id}/timeline", headers=organizer.headers).json()

    assert case["id"] not in {item["entity_id"] for item in timeline["items"]}


def test_an_admin_finds_reports_with_the_kind_filter(
    api: TestClient, buyer: Account, organizer: Account, admin: Account, event: Event
) -> None:
    reported = report(api, buyer, event).json()["id"]
    attendee = attendee_case(api, buyer, event)["id"]
    organizers = open_case(api, organizer, kind="organizer")["id"]

    assert reported in case_ids(api, admin, kind="report")
    assert attendee not in case_ids(api, admin, kind="report")
    assert organizers not in case_ids(api, admin, kind="report")
    assert reported not in case_ids(api, admin, kind="attendee")
    assert reported not in case_ids(api, admin, kind="organizer")


def test_a_plain_user_cannot_read_someone_elses_report(
    api: TestClient, make_account: Callable[..., Account], buyer: Account, event: Event
) -> None:
    case = report(api, buyer, event).json()

    assert get_case(api, make_account(), case["id"]).status_code == 404
    assert get_case(api, buyer, case["id"]).status_code == 200  # the reporter


def test_the_reporter_sees_their_report_and_the_admins_reply(
    api: TestClient, db_session: Session, buyer: Account, admin: Account, event: Event
) -> None:
    case = report(api, buyer, event).json()

    assert case["id"] in case_ids(api, buyer, "/support/cases")
    assert say(api, admin, case["id"], "Thanks, we checked it").status_code == 201
    detail = get_case(api, buyer, case["id"]).json()

    assert detail["status"] == "waiting_customer" and detail["assignee_id"] == str(admin.user.id)
    assert support_notifications(db_session, buyer) == ["support_reply"]


def test_an_admin_resolves_a_report_and_can_still_suspend_the_event(
    api: TestClient, buyer: Account, admin: Account, event: Event
) -> None:
    case = report(api, buyer, event).json()

    assert set_status(api, admin, case["id"], "resolved").json()["status"] == "resolved"
    suspended = api.post(f"/admin/events/{event.id}/suspend", headers=admin.headers)
    assert suspended.status_code == 200, suspended.text


def test_a_second_open_report_about_the_same_event_is_409(
    api: TestClient, buyer: Account, admin: Account, event: Event
) -> None:
    first = report(api, buyer, event).json()

    assert report(api, buyer, event).status_code == 409
    # Still unresolved while the admin has answered and waits for the reporter.
    say(api, admin, first["id"], "More details please")
    assert report(api, buyer, event, category="other").status_code == 409


def test_reports_about_other_events_or_from_other_users_are_fine(
    api: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    buyer: Account,
    organizer: Account,
    event: Event,
) -> None:
    assert organizer.organizer is not None
    other_event = create_event(db_session, organizer.organizer)
    report(api, buyer, event)

    assert report(api, buyer, other_event).status_code == 201
    assert report(api, make_account(), event).status_code == 201


def test_after_a_report_is_resolved_the_user_may_report_again(
    api: TestClient, buyer: Account, admin: Account, event: Event
) -> None:
    first = report(api, buyer, event).json()
    set_status(api, admin, first["id"], "resolved")

    assert report(api, buyer, event).status_code == 201


def test_a_report_does_not_block_ordinary_cases_about_the_event(
    api: TestClient, buyer: Account, event: Event
) -> None:
    report(api, buyer, event)

    assert attendee_case(api, buyer, event)["kind"] == "attendee"
