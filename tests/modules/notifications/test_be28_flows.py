"""BE-28 over HTTP on the rolled-back session: payout_status and support_assignment
notifications, a Kazakh user end to end, and R-9 (publishing needs a verified email)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.enums import Language, SystemRole
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token
from app.modules.ops.enums import NotificationType
from app.modules.ops.models import Notification
from app.modules.orders.enums import TicketKind
from tests.emails import RecordingEmailProvider, confirm_email
from tests.factories import create_event, create_organizer, create_ticket_type, create_user
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
T = NotificationType


def headers(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def rows(db: Session, user: User, type_: NotificationType) -> list[Notification]:
    stmt = select(Notification).where(Notification.user_id == user.id, Notification.type == type_)
    return list(db.scalars(stmt))


def organizer_with(db: Session, language: Language = Language.RU) -> tuple[OrganizerProfile, User]:
    profile = create_organizer(db)
    user = db.get_one(User, profile.user_id)
    user.language = language
    db.flush()
    return profile, user


def paid_event(db: Session, profile: OrganizerProfile) -> Event:
    event = create_event(db, profile)
    create_ticket_type(db, event, kind=TicketKind.PAID, price_kzt=5000, quantity_total=50)
    return event


def activate(api: TestClient, event: Event, owner: User, step: str):
    response = api.post(f"/events/{event.id}/activation/{step}", headers=headers(owner))
    assert response.status_code == 200, response.text
    return response.json()


# --- payout_status ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "subject", "word"),
    [
        (Language.RU, "Выплаты подключены (симуляция)", "симуляция"),
        (Language.EN, "Payouts set up (simulation)", "simulation"),
        (Language.KK, "Төлемдер қосылды (симуляция)", "симуляция"),
    ],
)
def test_simulated_verification_tells_the_organizer_once(
    session_api: TestClient,
    db_session: Session,
    emails: RecordingEmailProvider,
    language: Language,
    subject: str,
    word: str,
) -> None:
    profile, owner = organizer_with(db_session, language)
    event = paid_event(db_session, profile)

    activate(session_api, event, owner, "simulate-verification")

    [email] = emails.to(owner.email)
    assert email.subject == subject and word in email.text
    assert "https://ticketoff.store/organizer" in email.text
    [row] = rows(db_session, owner, T.PAYOUT_STATUS)
    assert row.payload == {"stage": "account_verified"}

    # Again: nothing new (the account was already ready).
    activate(session_api, event, owner, "simulate-verification")
    another = paid_event(db_session, profile)  # verification is organizer-wide
    activate(session_api, another, owner, "simulate-verification")
    assert len(emails.to(owner.email)) == 1
    assert len(rows(db_session, owner, T.PAYOUT_STATUS)) == 1


def test_switching_paid_sales_on_tells_the_organizer_with_the_event_link(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider
) -> None:
    profile, owner = organizer_with(db_session)
    event = paid_event(db_session, profile)
    activate(session_api, event, owner, "simulate-verification")
    activate(session_api, event, owner, "accept-terms")
    emails.sent.clear()

    checklist = activate(session_api, event, owner, "pay")  # the last step switches sales on

    assert checklist["active"] is True
    [email] = emails.to(owner.email)
    assert email.subject == f"Платные продажи включены (симуляция): {event.title}"
    assert f"https://ticketoff.store/organizer/events/{event.id}" in email.text
    [row] = [
        r
        for r in rows(db_session, owner, T.PAYOUT_STATUS)
        if r.payload["stage"] != "account_verified"
    ]
    assert row.payload == {"stage": "sales_activated", "event_id": str(event.id)}

    # Repeating any step: sales are already on, nothing is sent.
    for step in ("pay", "accept-terms", "simulate-verification"):
        activate(session_api, event, owner, step)
    assert len(emails.to(owner.email)) == 1


def test_another_organizer_gets_nothing(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider
) -> None:
    profile, owner = organizer_with(db_session)
    _, stranger = organizer_with(db_session)
    event = paid_event(db_session, profile)

    for step in ("simulate-verification", "accept-terms", "pay"):
        activate(session_api, event, owner, step)

    assert emails.to(stranger.email) == []
    assert rows(db_session, stranger, T.PAYOUT_STATUS) == []
    assert len(emails.to(owner.email)) == 2  # account ready + sales on


# --- support_assignment ------------------------------------------------------------------------


@pytest.fixture
def case_setup(session_api: TestClient, db_session: Session):
    profile, handler = organizer_with(db_session)
    event = create_event(db_session, profile)
    customer = create_user(db_session)
    customer.language = Language.KK
    db_session.flush()
    opened = session_api.post(
        "/support/cases",
        json={"kind": "attendee", "category": "ticket", "body": "Help", "event_id": str(event.id)},
        headers=headers(customer),
    )
    assert opened.status_code == 201, opened.text
    return handler, customer, opened.json()["id"]


def test_taking_a_case_tells_the_opener_without_naming_the_specialist(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider, case_setup
) -> None:
    handler, customer, case_id = case_setup

    response = session_api.post(f"/support/cases/{case_id}/assign", headers=headers(handler))

    assert response.status_code == 200
    [email] = emails.to(customer.email)  # one notification for one action
    assert email.subject.startswith("Өтінішіңіз жұмысқа алынды")  # the opener's language: kk
    assert f"/support/cases/{case_id}" in email.text
    assert handler.email not in email.subject + email.text + (email.html or "")
    [row] = rows(db_session, customer, T.SUPPORT_ASSIGNMENT)
    assert row.payload == {"case_id": case_id}
    assert rows(db_session, customer, T.SUPPORT_STATUS) == []  # not a second notice
    assert emails.to(handler.email) == []


def test_first_reply_that_assigns_sends_only_the_reply(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider, case_setup
) -> None:
    handler, customer, case_id = case_setup

    response = session_api.post(
        f"/support/cases/{case_id}/messages",
        data={"body": "Билет отправлен"},
        headers=headers(handler),
    )

    assert response.status_code == 201, response.text
    assert [m.subject.split(":")[0] for m in emails.to(customer.email)] == [
        "Қолдау қызметінің жауабы"
    ]
    assert rows(db_session, customer, T.SUPPORT_ASSIGNMENT) == []


def test_admin_taking_over_an_assigned_case_sends_nothing(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider, case_setup
) -> None:
    handler, customer, case_id = case_setup
    session_api.post(f"/support/cases/{case_id}/assign", headers=headers(handler))
    emails.sent.clear()
    admin = create_user(db_session)
    admin.system_role = SystemRole.PLATFORM_ADMIN
    db_session.flush()

    response = session_api.post(f"/support/cases/{case_id}/assign", headers=headers(admin))

    assert response.status_code == 200 and response.json()["assignee_id"] == str(admin.id)
    assert emails.sent == []
    assert len(rows(db_session, customer, T.SUPPORT_ASSIGNMENT)) == 1  # only the first one


def test_status_change_that_assigns_sends_only_the_status(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider, case_setup
) -> None:
    handler, customer, case_id = case_setup

    session_api.patch(
        f"/support/cases/{case_id}/status", json={"status": "resolved"}, headers=headers(handler)
    )

    assert [m.subject.split(":")[0] for m in emails.to(customer.email)] == ["Өтініш күйі"]
    assert rows(db_session, customer, T.SUPPORT_ASSIGNMENT) == []


# --- a Kazakh user end to end ------------------------------------------------------------------


def test_kk_user_gets_kk_verification_and_order_emails(
    session_api: TestClient, db_session: Session, emails: RecordingEmailProvider
) -> None:
    email = f"kk-{uuid.uuid4().hex[:10]}@example.com"
    registered = session_api.post(
        "/auth/register",
        json={"email": email, "password": "correct-horse-battery", "language": "kk"},
    )
    assert registered.status_code == 201 and registered.json()["language"] == "kk"
    [verification] = emails.to(email)
    assert verification.subject == "ticketoff-та email-ді растаңыз"
    confirm_email(session_api, email)

    profile = create_organizer(db_session)
    event = create_event(db_session, profile)
    free = create_ticket_type(
        db_session, event, kind=TicketKind.FREE, price_kzt=0, quantity_total=10
    )
    buyer = db_session.scalar(select(User).where(User.email == email))
    registered_order = session_api.post(
        f"/events/{event.id}/registrations",
        json={
            "items": [{"ticket_type_id": str(free.id), "quantity": 1}],
            "attendees": [{"name": "Айгерім"}],
        },
        headers=headers(buyer),
    )
    assert registered_order.status_code == 201, registered_order.text
    order_email = emails.to(email)[-1]
    assert order_email.subject == f"Тіркелу расталды: {event.title}"
    assert "Тіркелуіңіз расталды." in order_email.text and "Іс-шара: " in order_email.text
    kk_months = ("қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым", "шілде", "тамыз",
                 "қыркүйек", "қазан", "қараша", "желтоқсан")  # fmt: skip
    assert any(month in order_email.text for month in kk_months)  # kk date format
    assert '<html lang="kk">' in (order_email.html or "")


# --- R-9: publishing needs a verified email (done in #35; checked here) -----------------------


def test_publish_needs_a_verified_email_but_a_draft_does_not(
    session_api: TestClient, db_session: Session
) -> None:
    profile = create_organizer(db_session)
    owner = db_session.get_one(User, profile.user_id)
    owner.email_verified = False
    db_session.flush()
    venue_event = create_event(db_session, profile)  # for its venue
    draft = session_api.post(
        "/events",
        json={
            "title": "Черновик",
            "venue_id": str(venue_event.venue_id),
            "start_at": venue_event.start_at.isoformat(),
        },
        headers=headers(owner),
    )
    assert draft.status_code == 201, draft.text  # creating a draft stays unchecked
    event_id = draft.json()["id"]
    session_api.post(
        f"/events/{event_id}/ticket-types",
        json={"name": "Free", "kind": "free", "quantity_total": 10},
        headers=headers(owner),
    )

    refused = session_api.post(f"/events/{event_id}/publish", headers=headers(owner))
    assert refused.status_code == 403
    assert refused.json()["code"] == "EMAIL_NOT_VERIFIED"
    assert db_session.get_one(Event, event_id).status == EventStatus.DRAFT

    owner.email_verified = True
    db_session.flush()
    published = session_api.post(f"/events/{event_id}/publish", headers=headers(owner))
    assert published.status_code == 200 and published.json()["status"] == "published"
