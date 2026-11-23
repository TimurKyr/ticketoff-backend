"""BE-26: order search, CSV reports, promo activity, editable settings, admin timeline."""

import csv
import io
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.admin.platform_settings import get_platform_settings
from app.modules.audit.models import AuditLog
from app.modules.events.models import Event
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import User
from app.modules.identity.security import create_access_token
from app.modules.orders import repository as orders_repository
from app.modules.orders.enums import OrderStatus
from app.modules.orders.models import Attendee, Order
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus
from app.modules.payments.models import Payment, Refund
from app.modules.promotions.enums import DiscountType
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
from tests.factories import (
    create_event,
    create_order_item,
    create_organizer,
    create_ticket_type,
    create_user,
)
from tests.markers import postgres_only

pytestmark = postgres_only

Headers = dict[str, str]
ALMATY = ZoneInfo("Asia/Almaty")


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def _bearer(user: User) -> Headers:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def admin_user(db_session: Session) -> User:
    user = create_user(db_session)
    user.system_role = SystemRole.PLATFORM_ADMIN
    db_session.flush()
    return user


@pytest.fixture
def admin(admin_user: User) -> Headers:
    return _bearer(admin_user)


@pytest.fixture
def event(db_session: Session) -> Event:
    event = create_event(db_session, create_organizer(db_session))
    event.title = "Report Fest"
    db_session.flush()
    return event


def _order(
    session: Session,
    event: Event,
    *,
    status: OrderStatus = OrderStatus.PAID,
    created_at: datetime | None = None,
    price: int = 5000,
    quantity: int = 2,
) -> Order:
    ticket_type = create_ticket_type(session, event, price_kzt=price)
    item = create_order_item(session, ticket_type, quantity, status, created_at=created_at)
    order = session.get_one(Order, item.order_id)
    order.total_kzt = price * quantity
    session.flush()
    return order


def _paid(session: Session, order: Order, created_at: datetime | None = None) -> Payment:
    payment = Payment(
        purpose=PaymentPurpose.TICKET_ORDER,
        order_id=order.id,
        amount_kzt=order.total_kzt - order.discount_kzt,
        status=PaymentStatus.SUCCEEDED,
    )
    if created_at is not None:
        payment.created_at = created_at
    session.add(payment)
    session.flush()
    return payment


def _csv(response) -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=ALMATY)


# --- order search -------------------------------------------------------------------------


def test_search_order_by_buyer_email_guest_email_and_id(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    order = _order(db_session, event)
    order.discount_kzt = 1000
    buyer = db_session.get_one(User, order.buyer_user_id)
    tag = uuid.uuid4().hex[:8]
    buyer.email = f"Buyer-{tag}@Example.com"
    db_session.add(Attendee(order_id=order.id, name="Guest", email=f"guest-{tag}@mail.kz"))
    db_session.flush()

    def hits(q: str) -> list[dict]:
        body = api.get("/admin/search", params={"q": q, "type": "order"}, headers=admin).json()
        return body["items"]

    [hit] = hits(f"buyer-{tag}")
    assert hit == {
        "type": "order",
        "id": str(order.id),
        "label": f"Buyer-{tag}@Example.com · 9000 KZT · paid",
        "status": "paid",
        "suspended": False,
    }
    assert [h["id"] for h in hits(f"guest-{tag}")] == [str(order.id)]
    assert [h["id"] for h in hits(str(order.id))] == [str(order.id)]
    assert hits(f"nobody-{tag}") == []


# --- CSV reports --------------------------------------------------------------------------


def test_payments_csv_counts_rows_of_the_period(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    day = date(2031, 3, 10)
    inside = [_paid(db_session, _order(db_session, event), _at(day, h)) for h in (0, 23)]
    _paid(db_session, _order(db_session, event), _at(day + timedelta(days=1), 0))
    _paid(db_session, _order(db_session, event), _at(day - timedelta(days=1), 23))

    response = api.get(
        "/admin/reports/payments.csv",
        params={"from_date": day.isoformat(), "to_date": day.isoformat()},
        headers=admin,
    )

    rows = _csv(response)
    assert [r["payment_id"] for r in rows] == [str(p.id) for p in inside]
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="ticketoff-payments-2031-03-10_2031-03-10.csv"'
    )
    row = rows[0]
    assert row["is_simulated"] == "true"
    assert row["purpose"] == "ticket_order"
    assert row["event_id"] == str(event.id)
    assert row["event_title"] == "Report Fest"
    assert row["amount_kzt"] == "10000"
    assert "@" in row["buyer_email"]


def test_refunds_and_orders_csv(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    day = date(2031, 4, 1)
    event.title = "=HYPERLINK(1) Концерт"
    order = _order(db_session, event, created_at=_at(day))
    payment = _paid(db_session, order, _at(day))
    refund = Refund(
        payment_id=payment.id, amount_kzt=10000, status=RefundStatus.SUCCEEDED, reason="Отмена"
    )
    refund.created_at = _at(day, 15)
    db_session.add(refund)
    _order(db_session, event, created_at=_at(day + timedelta(days=5)))
    db_session.flush()
    params = {"from_date": day.isoformat(), "to_date": day.isoformat()}

    refunds = _csv(api.get("/admin/reports/refunds.csv", params=params, headers=admin))
    orders = _csv(api.get("/admin/reports/orders.csv", params=params, headers=admin))

    assert [(r["refund_id"], r["reason"], r["is_simulated"]) for r in refunds] == [
        (str(refund.id), "Отмена", "true")
    ]
    assert refunds[0]["order_id"] == str(order.id)
    [row] = orders
    assert row["order_id"] == str(order.id)
    assert row["tickets"] == "2"
    assert row["paid_kzt"] == "10000"
    assert row["is_simulated"] == "true"
    assert row["event_title"] == "'=HYPERLINK(1) Концерт"  # never run as a formula


def test_report_without_dates_has_everything_and_bad_input_is_422(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    order = _order(db_session, event)
    rows = _csv(api.get("/admin/reports/orders.csv", headers=admin))
    assert str(order.id) in {r["order_id"] for r in rows}
    bad = {"from_date": "2031-01-02", "to_date": "2031-01-01"}
    assert api.get("/admin/reports/orders.csv", params=bad, headers=admin).status_code == 422
    assert api.get("/admin/reports/users.csv", headers=admin).status_code == 422


# --- promo activity -----------------------------------------------------------------------


def _campaign(session: Session, event: Event, name: str, codes: int = 1) -> list[PromoCode]:
    campaign = PromotionalCampaign(
        event_id=event.id, name=name, discount_type=DiscountType.PERCENT, discount_value=10
    )
    session.add(campaign)
    session.flush()
    result = []
    for _ in range(codes):
        code = PromoCode(
            campaign_id=campaign.id,
            code=uuid.uuid4().hex[:10],
            qr_token=f"pc1.{uuid.uuid4().hex}",
        )
        session.add(code)
        result.append(code)
    session.flush()
    return result


def _redeem(session: Session, code: PromoCode, order: Order, discount: int, at: datetime) -> None:
    order.discount_kzt = discount
    redemption = PromoRedemption(promo_code_id=code.id, order_id=order.id, discount_kzt=discount)
    redemption.created_at = at
    session.add(redemption)
    session.flush()


def test_promo_activity_matches_the_discounts(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    day = date(2031, 5, 5)
    spring = _campaign(db_session, event, "Spring", codes=2)
    autumn = _campaign(db_session, event, "Autumn")
    _campaign(db_session, event, "Unused")
    _redeem(db_session, spring[0], _order(db_session, event), 1000, _at(day))
    _redeem(db_session, spring[1], _order(db_session, event), 500, _at(day))
    _redeem(db_session, autumn[0], _order(db_session, event), 2000, _at(day))
    # Not counted: an abandoned checkout, and a redemption outside the period.
    pending = _order(db_session, event, status=OrderStatus.PENDING)
    _redeem(db_session, autumn[0], pending, 700, _at(day))
    _redeem(db_session, spring[0], _order(db_session, event), 300, _at(day + timedelta(days=1)))
    params = {"from_date": day.isoformat(), "to_date": day.isoformat()}

    body = api.get("/admin/promo-activity", params=params, headers=admin).json()

    items = [i for i in body["items"] if i["event_id"] == str(event.id)]
    assert [(i["campaign_name"], i["codes"], i["redemptions"]) for i in items] == [
        ("Autumn", 1, 1),
        ("Spring", 2, 2),
    ]
    assert [i["discount_total_kzt"] for i in items] == [2000, 1500]
    assert [i["gross_kzt"] for i in items] == [8000, 18500]
    assert items[0]["event_title"] == "Report Fest"
    expected = db_session.scalar(
        select(func.coalesce(func.sum(PromoRedemption.discount_kzt), 0))
        .join(Order, Order.id == PromoRedemption.order_id)
        .where(
            Order.status.in_([OrderStatus.PAID, OrderStatus.FREE_CONFIRMED]),
            PromoRedemption.created_at >= _at(day, 0),
            PromoRedemption.created_at < _at(day + timedelta(days=1), 0),
        )
    )
    assert body["total_discount_kzt"] == expected == 3500
    assert body["total_redemptions"] == 3


# --- editable settings --------------------------------------------------------------------


def _activation_payment(db_session: Session, event: Event) -> Payment:
    return db_session.scalars(
        select(Payment).where(
            Payment.event_id == event.id, Payment.purpose == PaymentPurpose.ACTIVATION
        )
    ).one()


def test_patch_settings_new_fee_for_unpaid_activations_only(
    api: TestClient, db_session: Session, admin: Headers, admin_user: User
) -> None:
    old_fee = get_settings().activation_fee_kzt
    paid_before = create_event(db_session, create_organizer(db_session))
    later = create_event(db_session, create_organizer(db_session))
    owner = {
        e.id: _bearer(db_session.get_one(User, e.organizer.user_id)) for e in (paid_before, later)
    }

    def pay(e: Event) -> int:
        return api.post(f"/events/{e.id}/activation/pay", headers=owner[e.id]).status_code

    assert pay(paid_before) == 200

    response = api.patch(
        "/admin/settings", json={"activation_fee_kzt": old_fee + 1234}, headers=admin
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["activation_fee_kzt"] == old_fee + 1234
    assert body["read_only"] is False
    assert body["updated_at"] is not None
    checklist = api.get(f"/events/{later.id}/activation", headers=owner[later.id]).json()
    assert checklist["activation_fee_kzt"] == old_fee + 1234
    assert pay(later) == 200
    assert _activation_payment(db_session, later).amount_kzt == old_fee + 1234
    assert _activation_payment(db_session, paid_before).amount_kzt == old_fee
    [row] = db_session.scalars(
        select(AuditLog).where(AuditLog.action_type == "admin.settings_updated")
    ).all()
    assert row.actor_user_id == admin_user.id
    assert row.details == {"activation_fee_kzt": {"old": old_fee, "new": old_fee + 1234}}


def test_patch_settings_drives_hold_and_fee_percent(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    body = {"checkout_hold_minutes": 42, "processing_fee_percent": 7}
    assert api.patch("/admin/settings", json=body, headers=admin).status_code == 200
    assert orders_repository.checkout_hold(db_session) == timedelta(minutes=42)
    assert get_platform_settings(db_session).processing_fee_percent == 7
    assert api.get("/admin/settings", headers=admin).json()["checkout_hold_minutes"] == 42
    # The same values again: nothing changes, nothing is logged.
    assert api.patch("/admin/settings", json=body, headers=admin).status_code == 200
    logged = db_session.scalars(
        select(AuditLog).where(AuditLog.action_type == "admin.settings_updated")
    ).all()
    assert len(logged) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"activation_fee_kzt": 0},
        {"processing_fee_percent": 101},
        {"checkout_hold_minutes": 0},
        {"activation_fee_kzt": None},
        {"unknown": 1},
    ],
)
def test_patch_settings_invalid_is_422(api: TestClient, admin: Headers, body: dict) -> None:
    assert api.patch("/admin/settings", json=body, headers=admin).status_code == 422


# --- timeline -----------------------------------------------------------------------------


def test_admin_sees_any_events_timeline_others_get_404(
    api: TestClient, db_session: Session, admin: Headers, event: Event
) -> None:
    db_session.add(
        AuditLog(
            action_type="event.published",
            entity_type="event",
            entity_id=event.id,
            event_id=event.id,
        )
    )
    db_session.flush()
    url = f"/events/{event.id}/timeline"

    response = api.get(url, headers=admin)

    assert response.status_code == 200
    assert [i["action_type"] for i in response.json()["items"]] == ["event.published"]
    attendee = _bearer(create_user(db_session))
    stranger = _bearer(db_session.get_one(User, create_organizer(db_session).user_id))
    owner = _bearer(db_session.get_one(User, event.organizer.user_id))
    assert api.get(url, headers=attendee).status_code == 404
    assert api.get(url, headers=stranger).status_code == 404
    assert api.get(url, headers=owner).status_code == 200
    assert api.get(f"/events/{uuid.uuid4()}/timeline", headers=admin).status_code == 404
