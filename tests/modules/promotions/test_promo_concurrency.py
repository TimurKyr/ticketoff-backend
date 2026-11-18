"""Two buyers race for the last use of a campaign, on separate real connections (BE-16).

Rows are committed (the threads cannot see a rolled-back test session) and deleted after.
The event row lock taken by checkout makes the use count exact: exactly one order gets the
discount, the other is refused.
"""

import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.common.errors import BusinessRuleError
from app.core.db import SessionLocal
from app.modules.audit.models import AuditLog
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.orders import service as orders_service
from app.modules.orders.enums import TicketKind
from app.modules.orders.models import Attendee, Order, OrderItem, TicketType
from app.modules.orders.schemas import CheckoutCreate
from app.modules.promotions.enums import DiscountType, PromoCodeStatus
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
from tests.markers import postgres_only

pytestmark = [postgres_only, pytest.mark.slow]

CODE = "LASTONE"
PRICE_KZT = 15000


@dataclass
class LastUse:
    event_id: uuid.UUID
    ticket_type_id: uuid.UUID
    campaign_id: uuid.UUID
    buyer_ids: list[uuid.UUID]


@pytest.fixture
def last_use() -> Iterator[LastUse]:
    """A committed paid event with a campaign that can be used exactly once."""
    with SessionLocal() as session:
        organizer_user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
        buyers = [User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x") for _ in "ab"]
        venue = Venue(name=f"Hall {uuid.uuid4().hex[:6]}")
        session.add_all([organizer_user, *buyers, venue])
        session.flush()
        organizer = OrganizerProfile(user_id=organizer_user.id, display_name="Organizer")
        session.add(organizer)
        session.flush()
        event = Event(
            organizer_id=organizer.id,
            venue_id=venue.id,
            title="Last use",
            status=EventStatus.PUBLISHED,
            paid_sales_active=True,
            start_at=datetime.now(UTC) + timedelta(days=30),
        )
        session.add(event)
        session.flush()
        ticket_type = TicketType(
            event_id=event.id,
            name="Standard",
            kind=TicketKind.PAID,
            price_kzt=PRICE_KZT,
            quantity_total=10,
            max_per_order=10,
        )
        campaign = PromotionalCampaign(
            event_id=event.id,
            name="One use",
            discount_type=DiscountType.PERCENT,
            discount_value=20,
            max_redemptions=1,
        )
        session.add_all([ticket_type, campaign])
        session.flush()
        session.add(
            PromoCode(
                campaign_id=campaign.id,
                code=CODE,
                qr_token=f"pc1.{uuid.uuid4().hex}",
                status=PromoCodeStatus.ACTIVE,
            )
        )
        session.commit()
        fixture = LastUse(event.id, ticket_type.id, campaign.id, [b.id for b in buyers])
        cleanup_ids = (venue.id, organizer.id, organizer_user.id)

    yield fixture

    venue_id, organizer_id, organizer_user_id = cleanup_ids
    with SessionLocal() as cleanup:
        order_ids = select(Order.id).where(Order.event_id == fixture.event_id)
        item_ids = select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        code_ids = select(PromoCode.id).where(PromoCode.campaign_id == fixture.campaign_id)
        # Test rows only: audit_logs is append-only in the app, but these were made here.
        cleanup.execute(delete(AuditLog).where(AuditLog.event_id == fixture.event_id))
        cleanup.execute(delete(PromoRedemption).where(PromoRedemption.promo_code_id.in_(code_ids)))
        cleanup.execute(delete(Attendee).where(Attendee.order_id.in_(order_ids)))
        cleanup.execute(delete(OrderItem).where(OrderItem.id.in_(item_ids)))
        cleanup.execute(delete(Order).where(Order.event_id == fixture.event_id))
        cleanup.execute(delete(PromoCode).where(PromoCode.campaign_id == fixture.campaign_id))
        cleanup.execute(
            delete(PromotionalCampaign).where(PromotionalCampaign.id == fixture.campaign_id)
        )
        cleanup.execute(delete(TicketType).where(TicketType.event_id == fixture.event_id))
        cleanup.execute(delete(Event).where(Event.id == fixture.event_id))
        cleanup.execute(delete(Venue).where(Venue.id == venue_id))
        cleanup.execute(delete(OrganizerProfile).where(OrganizerProfile.id == organizer_id))
        cleanup.execute(delete(User).where(User.id.in_([organizer_user_id, *fixture.buyer_ids])))
        cleanup.commit()


def _checkout_with_code(buyer_id: uuid.UUID, last: LastUse, barrier: threading.Barrier) -> str:
    """Check out one ticket with the code in a session of its own: "won" or "rejected"."""
    data = CheckoutCreate.model_validate(
        {
            "items": [{"ticket_type_id": str(last.ticket_type_id), "quantity": 1}],
            "attendees": [{"name": "Guest"}],
            "promo_code": CODE,
        }
    )
    with SessionLocal() as session:
        buyer = session.get_one(User, buyer_id)
        barrier.wait()
        try:
            order = orders_service.checkout(session, buyer, last.event_id, data)
        except BusinessRuleError as exc:
            return "rejected" if "fully used" in str(exc) else f"other: {exc}"
        return "won" if order.discount_kzt > 0 else "no discount"


def test_two_buyers_race_for_the_last_use_and_exactly_one_gets_the_discount(
    last_use: LastUse,
) -> None:
    barrier = threading.Barrier(2)
    results: list[str] = []

    def run(buyer_id: uuid.UUID) -> None:
        results.append(_checkout_with_code(buyer_id, last_use, barrier))

    threads = [threading.Thread(target=run, args=(b,)) for b in last_use.buyer_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == ["rejected", "won"]
    with SessionLocal() as check:
        code_ids = select(PromoCode.id).where(PromoCode.campaign_id == last_use.campaign_id)
        redemptions = check.scalar(
            select(func.count())
            .select_from(PromoRedemption)
            .where(PromoRedemption.promo_code_id.in_(code_ids))
        )
        discounted = check.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.event_id == last_use.event_id, Order.discount_kzt > 0)
        )
        orders = check.scalar(
            select(func.count()).select_from(Order).where(Order.event_id == last_use.event_id)
        )
    assert (redemptions, discounted, orders) == (1, 1, 1)
