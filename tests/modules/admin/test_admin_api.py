"""Platform admin API (BE-22) over HTTP, on the rolled-back test session."""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.audit.models import AuditLog
from app.modules.events.models import Event
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import User
from app.modules.identity.security import create_access_token, hash_password
from app.modules.orders.models import TicketType
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from tests.factories import (
    create_event,
    create_free_ticket_type,
    create_order_item,
    create_organizer,
    create_ticket_type,
)
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
PASSWORD = "correct-horse-battery"


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def _bearer(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def make_user(db_session: Session) -> Callable[..., User]:
    def make(role: SystemRole = SystemRole.ATTENDEE, email: str | None = None) -> User:
        user = User(
            email=email or f"user-{uuid.uuid4().hex[:12]}@example.com",
            password_hash=hash_password(PASSWORD),
            system_role=role,
            email_verified=True,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return make


@pytest.fixture
def admin_user(make_user: Callable[..., User]) -> User:
    return make_user(SystemRole.PLATFORM_ADMIN)


@pytest.fixture
def admin(admin_user: User) -> Headers:
    return _bearer(admin_user)


@pytest.fixture
def event(db_session: Session) -> Event:
    event = create_event(db_session, create_organizer(db_session))
    event.paid_sales_active = True
    db_session.flush()
    return event


@pytest.fixture
def free_type(db_session: Session, event: Event) -> TicketType:
    return create_free_ticket_type(db_session, event)


@pytest.fixture
def paid_type(db_session: Session, event: Event) -> TicketType:
    return create_ticket_type(db_session, event, price_kzt=10000)


def _order_body(ticket_type: TicketType) -> dict:
    return {
        "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        "attendees": [{"name": "Aruzhan"}],
    }


def register(api: TestClient, headers: Headers, free_type: TicketType):
    return api.post(
        f"/events/{free_type.event_id}/registrations", json=_order_body(free_type), headers=headers
    )


def checkout(api: TestClient, headers: Headers, paid_type: TicketType):
    return api.post(
        f"/events/{paid_type.event_id}/checkout", json=_order_body(paid_type), headers=headers
    )


def pay(api: TestClient, headers: Headers, order_id: str):
    return api.post(f"/orders/{order_id}/pay", json={"simulate": "success"}, headers=headers)


def _audit(db_session: Session, action_type: str, entity_id: uuid.UUID) -> list[AuditLog]:
    return list(
        db_session.scalars(
            select(AuditLog).where(
                AuditLog.action_type == action_type, AuditLog.entity_id == entity_id
            )
        )
    )


# --- access -------------------------------------------------------------------------------

ID = uuid.uuid4()
ADMIN_ROUTES = [
    ("post", f"/admin/events/{ID}/suspend"),
    ("post", f"/admin/events/{ID}/unsuspend"),
    ("post", f"/admin/users/{ID}/suspend"),
    ("post", f"/admin/users/{ID}/unsuspend"),
    ("get", "/admin/search?q=a&type=user"),
    ("get", "/admin/payments"),
    ("get", "/admin/refunds"),
    ("get", "/admin/activations"),
    ("get", "/admin/settings"),
    ("patch", "/admin/settings"),
    ("get", "/admin/search?q=a&type=order"),
    ("get", "/admin/reports/payments.csv"),
    ("get", "/admin/reports/refunds.csv"),
    ("get", "/admin/reports/orders.csv"),
    ("get", "/admin/promo-activity"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_without_token_is_401(api: TestClient, method: str, path: str) -> None:
    assert api.request(method, path).status_code == 401


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_attendee_is_403(
    api: TestClient, make_user: Callable[..., User], method: str, path: str
) -> None:
    assert api.request(method, path, headers=_bearer(make_user())).status_code == 403


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_organizer_is_403(
    api: TestClient, db_session: Session, event: Event, method: str, path: str
) -> None:
    organizer_user = db_session.get(User, event.organizer.user_id)
    path = path.replace(str(ID), str(event.id))  # even on their own event
    assert api.request(method, path, headers=_bearer(organizer_user)).status_code == 403


# --- event suspension ---------------------------------------------------------------------


def test_suspend_event_blocks_registration_checkout_and_pay(
    api: TestClient,
    db_session: Session,
    admin: Headers,
    admin_user: User,
    make_user: Callable[..., User],
    event: Event,
    free_type: TicketType,
    paid_type: TicketType,
) -> None:
    buyer = _bearer(make_user())
    pending = checkout(api, buyer, paid_type)
    assert pending.status_code == 201

    response = api.post(f"/admin/events/{event.id}/suspend", headers=admin)
    assert response.status_code == 200
    assert response.json()["sales_suspended"] is True

    for blocked in (
        register(api, buyer, free_type),
        checkout(api, buyer, paid_type),
        pay(api, buyer, pending.json()["id"]),
    ):
        assert blocked.status_code == 409
        assert "suspended" in blocked.json()["detail"]

    # The organizer sees it on their event.
    assert api.get(f"/events/{event.id}").json()["sales_suspended"] is True

    [row] = _audit(db_session, "event.sales_suspended", event.id)
    assert row.event_id == event.id
    assert row.actor_user_id == admin_user.id


def test_suspended_event_existing_ticket_still_scans(
    api: TestClient,
    db_session: Session,
    admin: Headers,
    make_user: Callable[..., User],
    event: Event,
    free_type: TicketType,
) -> None:
    order = register(api, _bearer(make_user()), free_type)
    assert order.status_code == 201
    token = order.json()["tickets"][0]["qr_token"]

    assert api.post(f"/admin/events/{event.id}/suspend", headers=admin).status_code == 200

    owner = _bearer(db_session.get(User, event.organizer.user_id))
    scan = api.post(
        "/checkin/scan", json={"event_id": str(event.id), "qr_token": token}, headers=owner
    )
    assert scan.status_code == 200
    assert scan.json()["result"] == "checked_in"


def test_unsuspend_restores_sales(
    api: TestClient,
    db_session: Session,
    admin: Headers,
    make_user: Callable[..., User],
    event: Event,
    free_type: TicketType,
    paid_type: TicketType,
) -> None:
    buyer = _bearer(make_user())
    api.post(f"/admin/events/{event.id}/suspend", headers=admin)
    response = api.post(f"/admin/events/{event.id}/unsuspend", headers=admin)
    assert response.status_code == 200
    assert response.json()["sales_suspended"] is False

    assert register(api, buyer, free_type).status_code == 201
    order = checkout(api, buyer, paid_type)
    assert order.status_code == 201
    assert pay(api, buyer, order.json()["id"]).status_code == 200
    assert len(_audit(db_session, "event.sales_unsuspended", event.id)) == 1


def test_repeated_suspend_changes_nothing(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    for _ in range(2):
        assert api.post(f"/admin/events/{event.id}/suspend", headers=admin).status_code == 200
    assert len(_audit(db_session, "event.sales_suspended", event.id)) == 1
    # Unsuspending a non-suspended event logs nothing either.
    api.post(f"/admin/events/{event.id}/unsuspend", headers=admin)
    api.post(f"/admin/events/{event.id}/unsuspend", headers=admin)
    assert len(_audit(db_session, "event.sales_unsuspended", event.id)) == 1


def test_suspend_unknown_event_is_404(api: TestClient, admin: Headers) -> None:
    assert api.post(f"/admin/events/{uuid.uuid4()}/suspend", headers=admin).status_code == 404


# --- user suspension ----------------------------------------------------------------------


def _login(api: TestClient, email: str, password: str = PASSWORD):
    return api.post("/auth/login", data={"username": email, "password": password})


def test_suspended_user_cannot_log_in_and_old_token_is_403(
    api: TestClient,
    db_session: Session,
    admin: Headers,
    admin_user: User,
    make_user: Callable[..., User],
) -> None:
    user = make_user()
    old_token = _bearer(user)
    assert api.get("/auth/me", headers=old_token).status_code == 200

    response = api.post(f"/admin/users/{user.id}/suspend", headers=admin)
    assert response.status_code == 200
    assert response.json()["is_suspended"] is True

    assert _login(api, user.email).status_code == 403
    assert _login(api, user.email, "wrong-password-123").status_code == 401  # not revealed
    assert api.get("/auth/me", headers=old_token).status_code == 403

    [row] = _audit(db_session, "user.suspended", user.id)
    assert row.actor_user_id == admin_user.id
    assert row.event_id is None


def test_unsuspended_user_can_log_in_again(
    api: TestClient, db_session: Session, admin: Headers, make_user: Callable[..., User]
) -> None:
    user = make_user()
    api.post(f"/admin/users/{user.id}/suspend", headers=admin)
    response = api.post(f"/admin/users/{user.id}/unsuspend", headers=admin)
    assert response.status_code == 200
    assert response.json()["is_suspended"] is False
    assert _login(api, user.email).status_code == 200
    assert api.get("/auth/me", headers=_bearer(user)).status_code == 200
    assert len(_audit(db_session, "user.unsuspended", user.id)) == 1


def test_admin_cannot_suspend_self(api: TestClient, admin: Headers, admin_user: User) -> None:
    assert api.post(f"/admin/users/{admin_user.id}/suspend", headers=admin).status_code == 409
    assert api.get("/auth/me", headers=admin).status_code == 200


def test_suspend_unknown_user_is_404(api: TestClient, admin: Headers) -> None:
    assert api.post(f"/admin/users/{uuid.uuid4()}/suspend", headers=admin).status_code == 404


# --- search -------------------------------------------------------------------------------


def test_search_users_by_email_part(
    api: TestClient, admin: Headers, make_user: Callable[..., User]
) -> None:
    tag = uuid.uuid4().hex[:10]
    first = make_user(email=f"a-{tag}@example.com")
    make_user(email=f"b-{tag}@example.com")
    response = api.get(f"/admin/search?q={tag.upper()}&type=user&limit=1", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert body["items"] == [
        {
            "type": "user",
            "id": str(first.id),
            "label": first.email,
            "status": "attendee",
            "suspended": False,
        }
    ]


def test_search_escapes_like_wildcards(
    api: TestClient, admin: Headers, make_user: Callable[..., User]
) -> None:
    tag = uuid.uuid4().hex[:10]
    make_user(email=f"x{tag}@example.com")
    body = api.get(f"/admin/search?q=%25{tag}&type=user", headers=admin).json()  # q = "%<tag>"
    assert body["total"] == 0


def test_search_events_by_title(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    event.title = f"Jazz night {uuid.uuid4().hex[:10]}"
    db_session.flush()
    body = api.get(f"/admin/search?q={event.title[-10:]}&type=event", headers=admin).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(event.id)
    assert body["items"][0]["status"] == "published"


@pytest.mark.parametrize("query", ["q=a", "type=user", "q=&type=user", "q=a&type=ticket"])
def test_search_bad_params_are_422(api: TestClient, admin: Headers, query: str) -> None:
    assert api.get(f"/admin/search?{query}", headers=admin).status_code == 422


# --- lists and settings -------------------------------------------------------------------


def _payment(db_session: Session, event: Event, purpose: PaymentPurpose, status: PaymentStatus):
    payment = Payment(event_id=event.id, purpose=purpose, status=status, amount_kzt=5000)
    db_session.add(payment)
    db_session.flush()
    return payment


def test_payments_filter_by_status(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    failed = _payment(db_session, event, PaymentPurpose.ACTIVATION, PaymentStatus.FAILED)
    _payment(db_session, event, PaymentPurpose.ACTIVATION, PaymentStatus.SUCCEEDED)
    body = api.get("/admin/payments?status=failed&limit=100", headers=admin).json()
    assert str(failed.id) in {p["id"] for p in body["items"]}
    assert {p["status"] for p in body["items"]} == {"failed"}
    assert body["total"] >= 1


def test_activations_are_activation_payments_only(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    activation = _payment(db_session, event, PaymentPurpose.ACTIVATION, PaymentStatus.SUCCEEDED)
    body = api.get("/admin/activations?limit=100", headers=admin).json()
    assert str(activation.id) in {p["id"] for p in body["items"]}
    assert {p["purpose"] for p in body["items"]} == {"activation"}


def test_refunds_list_has_order_and_event(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    order = create_order_item(db_session, create_ticket_type(db_session, event), 1).order
    payment = Payment(
        order_id=order.id,
        purpose=PaymentPurpose.TICKET_ORDER,
        status=PaymentStatus.SUCCEEDED,
        amount_kzt=5000,
    )
    db_session.add(payment)
    db_session.flush()
    refund = Refund(
        payment_id=payment.id, amount_kzt=5000, status=RefundStatus.SUCCEEDED, reason="dup"
    )
    db_session.add(refund)
    db_session.flush()
    body = api.get("/admin/refunds?limit=100", headers=admin).json()
    # A ticket payment's event comes from its order.
    [listed] = [
        p
        for p in api.get("/admin/payments?limit=100", headers=admin).json()["items"]
        if p["id"] == str(payment.id)
    ]
    assert listed["event_id"] == str(event.id)
    [item] = [r for r in body["items"] if r["id"] == str(refund.id)]
    assert item["payment_id"] == str(payment.id)
    assert item["order_id"] == str(order.id)
    assert item["event_id"] == str(event.id)
    assert item["reason"] == "dup"


def test_settings_default_to_env_and_are_editable(api: TestClient, admin: Headers) -> None:
    body = api.get("/admin/settings", headers=admin).json()
    env = get_settings()
    assert body == {
        "activation_fee_kzt": env.activation_fee_kzt,
        "processing_fee_percent": env.processing_fee_percent,
        "checkout_hold_minutes": env.checkout_hold_minutes,
        "currency": "KZT",
        "read_only": False,
        "updated_at": None,
    }
    assert api.put("/admin/settings", json={}, headers=admin).status_code == 405
