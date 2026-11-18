"""Promo campaigns and codes: organizer setup, the buyer's check, and the discount at checkout."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.errors import BusinessRuleError, NotFoundError
from app.core.config import get_settings
from app.modules.audit import service as audit
from app.modules.events import service as events_service
from app.modules.events.enums import EventStatus
from app.modules.events.models import Event
from app.modules.identity.models import User
from app.modules.orders.models import Order
from app.modules.promotions import repository
from app.modules.promotions.enums import DiscountType, PromoCodeStatus
from app.modules.promotions.models import PromoCode, PromoRedemption, PromotionalCampaign
from app.modules.promotions.qr_png import render_qr_png
from app.modules.promotions.schemas import (
    CampaignCreate,
    CampaignOut,
    CodeCreate,
    CodeOut,
    CodeUpdate,
    PromoCheckOut,
    PromoQrOut,
)

# The prefix tells the door scanner this is a campaign QR, never an entry ticket.
CAMPAIGN_PREFIX = "pc1."
# No 0/O, 1/I/L: a code read from a poster is typed by hand.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_CODE_TRIES = 5

CLOSED_EVENT_STATUSES = (EventStatus.CANCELLED, EventStatus.COMPLETED)


@dataclass(frozen=True, slots=True)
class AppliedPromo:
    """A code that passed every check, with the discount it gives on this order."""

    code_id: uuid.UUID
    discount_kzt: int


def normalize_code(code: str) -> str:
    return code.strip().upper()


# --- organizer: campaigns and codes (the router has already checked event ownership) --------


def _ensure_event_open(event: Event) -> None:
    if event.status in CLOSED_EVENT_STATUSES:
        raise BusinessRuleError("Promo campaigns and codes are closed for this event")


def create_campaign(
    session: Session, user: User, event: Event, data: CampaignCreate
) -> CampaignOut:
    _ensure_event_open(event)
    campaign = PromotionalCampaign(event_id=event.id, **data.model_dump())
    repository.add(session, campaign)
    audit.log(
        session,
        actor_user_id=user.id,
        action_type="promo.campaign_created",
        entity_type="promo_campaign",
        entity_id=campaign.id,
        event_id=event.id,
        details={
            "name": campaign.name,
            "discount_type": campaign.discount_type.value,
            "discount_value": campaign.discount_value,
        },
    )
    session.commit()  # the campaign and its audit row together
    return CampaignOut.model_validate(campaign)


def list_campaigns(session: Session, event: Event) -> list[CampaignOut]:
    return [CampaignOut.model_validate(c) for c in repository.list_campaigns(session, event.id)]


def _get_campaign(session: Session, event: Event, campaign_id: uuid.UUID) -> PromotionalCampaign:
    campaign = repository.get_campaign(session, event.id, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    return campaign


def _generate_code(session: Session) -> str:
    for _ in range(_CODE_TRIES):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        if not repository.code_exists(session, code):
            return code
    raise BusinessRuleError("Could not generate a free code, try again")


def _status_now(
    code: PromoCode, campaign: PromotionalCampaign, used: int, now: datetime
) -> PromoCodeStatus:
    """The status as of `now`. Disabled is the organizer's switch; the rest is worked out."""
    if code.status == PromoCodeStatus.DISABLED:
        return PromoCodeStatus.DISABLED
    if campaign.ends_at is not None and now >= campaign.ends_at:
        return PromoCodeStatus.EXPIRED
    if campaign.max_redemptions is not None and used >= campaign.max_redemptions:
        return PromoCodeStatus.EXHAUSTED
    return PromoCodeStatus.ACTIVE


def _code_out(
    code: PromoCode,
    campaign: PromotionalCampaign,
    redemptions: int,
    campaign_used: int,
    now: datetime,
) -> CodeOut:
    status = _status_now(code, campaign, campaign_used, now)
    started = campaign.starts_at is None or now >= campaign.starts_at
    return CodeOut(
        id=code.id,
        campaign_id=code.campaign_id,
        code=code.code,
        qr_token=code.qr_token,
        status=status,
        # Active AND the campaign has started: exactly what a buyer would be allowed to use.
        redeemable_now=status == PromoCodeStatus.ACTIVE and started,
        redemptions=redemptions,
    )


def create_code(
    session: Session,
    user: User,
    event: Event,
    campaign_id: uuid.UUID,
    data: CodeCreate,
    now: datetime | None = None,
) -> CodeOut:
    now = now or datetime.now(UTC)
    _ensure_event_open(event)
    campaign = _get_campaign(session, event, campaign_id)
    code = PromoCode(
        campaign_id=campaign.id,
        code=normalize_code(data.code) if data.code else _generate_code(session),
        qr_token=f"{CAMPAIGN_PREFIX}{secrets.token_urlsafe(24)}",
        status=PromoCodeStatus.ACTIVE,
    )
    try:
        repository.add(session, code)
        audit.log(
            session,
            actor_user_id=user.id,
            action_type="promo.code_created",
            entity_type="promo_code",
            entity_id=code.id,
            event_id=event.id,
            details={"campaign_id": str(campaign.id), "code": code.code},
        )
        session.commit()  # the code and its audit row together
    except IntegrityError as exc:
        session.rollback()
        raise BusinessRuleError("This promo code is not available, choose another") from exc
    used = repository.campaign_redemptions(session, campaign.id, now)
    return _code_out(code, campaign, 0, used, now)


def list_codes(
    session: Session, event: Event, campaign_id: uuid.UUID, now: datetime | None = None
) -> list[CodeOut]:
    now = now or datetime.now(UTC)
    campaign = _get_campaign(session, event, campaign_id)
    per_code = repository.code_redemptions(session, campaign.id, now)
    used = sum(per_code.values())
    return [
        _code_out(code, campaign, per_code.get(code.id, 0), used, now)
        for code in repository.list_codes(session, campaign.id)
    ]


def update_code(
    session: Session,
    user: User,
    event: Event,
    campaign_id: uuid.UUID,
    code_id: uuid.UUID,
    data: CodeUpdate,
    now: datetime | None = None,
) -> CodeOut:
    now = now or datetime.now(UTC)
    if data.status == PromoCodeStatus.ACTIVE:
        _ensure_event_open(event)  # switching a code off is always allowed, back on is not
    campaign = _get_campaign(session, event, campaign_id)
    code = repository.get_code(session, campaign.id, code_id)
    if code is None:
        raise NotFoundError("Promo code not found")
    new_status = PromoCodeStatus(data.status)
    if code.status != new_status:  # switching to the status it already has changes nothing
        code.status = new_status
        audit.log(
            session,
            actor_user_id=user.id,
            action_type=(
                "promo.code_disabled"
                if new_status == PromoCodeStatus.DISABLED
                else "promo.code_enabled"
            ),
            entity_type="promo_code",
            entity_id=code.id,
            event_id=event.id,
            details={"campaign_id": str(campaign.id), "code": code.code},
        )
    session.commit()  # the change and its audit row together
    per_code = repository.code_redemptions(session, campaign.id, now)
    return _code_out(code, campaign, per_code.get(code.id, 0), sum(per_code.values()), now)


def campaign_qr_link(event_id: uuid.UUID, qr_token: str) -> str:
    """What the campaign QR encodes: a link, so a phone camera opens the event page.

    The web app reads `?promo=` and calls GET /promo-qr/{qr_token} to pre-fill the code.
    The token is still opaque and random; the link carries no discount.
    """
    base = get_settings().frontend_url.rstrip("/")
    return f"{base}/events/{event_id}?promo={qr_token}"


def code_qr_png(
    session: Session, event: Event, campaign_id: uuid.UUID, code_id: uuid.UUID
) -> bytes:
    campaign = _get_campaign(session, event, campaign_id)
    code = repository.get_code(session, campaign.id, code_id)
    if code is None:
        raise NotFoundError("Promo code not found")
    return render_qr_png(campaign_qr_link(event.id, code.qr_token))


# --- buyer: check a code, scan a campaign QR ------------------------------------------------


def discount_for(campaign: PromotionalCampaign, total_kzt: int) -> int:
    """The discount in KZT on an order worth `total_kzt` before the discount.

    A payment must be more than 0 KZT (a database rule), so a code never makes an order
    completely free: the buyer always pays at least 1 KZT.
    """
    if total_kzt <= 1:
        return 0
    if campaign.discount_type == DiscountType.PERCENT:
        discount = total_kzt * campaign.discount_value // 100
    else:
        discount = campaign.discount_value
    return min(discount, total_kzt - 1)


def _check_redeemable(
    session: Session, event_id: uuid.UUID, raw_code: str, now: datetime
) -> tuple[PromoCode, PromotionalCampaign]:
    found = repository.find_code(session, normalize_code(raw_code))
    # A code of another event looks exactly like an unknown one.
    if found is None or found[1].event_id != event_id:
        raise NotFoundError("Promo code not found")
    code, campaign = found
    if code.status == PromoCodeStatus.DISABLED:
        raise BusinessRuleError("This promo code is disabled")
    if campaign.starts_at is not None and now < campaign.starts_at:
        raise BusinessRuleError("This promo code is not active yet")
    if campaign.ends_at is not None and now >= campaign.ends_at:
        raise BusinessRuleError("This promo code has expired")
    if (
        campaign.max_redemptions is not None
        and repository.campaign_redemptions(session, campaign.id, now) >= campaign.max_redemptions
    ):
        raise BusinessRuleError("This promo code has been fully used")
    return code, campaign


def check_code(
    session: Session, event_id: uuid.UUID, raw_code: str, now: datetime | None = None
) -> PromoCheckOut:
    """Answer a buyer's "does this code work?" without using it up."""
    now = now or datetime.now(UTC)
    events_service.get_public_event(session, event_id)  # 404 for a draft or private event
    code, campaign = _check_redeemable(session, event_id, raw_code, now)
    return PromoCheckOut(
        code=code.code,
        campaign_name=campaign.name,
        discount_type=campaign.discount_type,
        discount_value=campaign.discount_value,
    )


def resolve_qr(session: Session, qr_token: str, now: datetime | None = None) -> PromoQrOut:
    """Where a scanned campaign QR leads, if its code can still be used.

    A disabled, not yet started, expired or fully used code answers 409 with the reason,
    so the app never pre-fills a code that checkout would refuse. It never admits anyone:
    the door rejects `pc1.`.
    """
    now = now or datetime.now(UTC)
    found = repository.find_code_by_qr(session, qr_token.strip())
    if found is None:
        raise NotFoundError("Promo QR not found")
    code, campaign = found
    events_service.get_public_event(session, campaign.event_id)
    _check_redeemable(session, campaign.event_id, code.code, now)
    return PromoQrOut(event_id=campaign.event_id, code=code.code)


# --- checkout (called by the orders service) ------------------------------------------------


def apply_at_checkout(
    session: Session, event_id: uuid.UUID, raw_code: str, total_kzt: int, now: datetime
) -> AppliedPromo:
    """Check the code and work out its discount on this order.

    The caller (orders.service.checkout) holds the event row lock, taken before this runs.
    Every checkout of the event takes that lock first, so two buyers cannot both take the
    last use of a campaign: the second one counts after the first one's order exists.
    """
    code, campaign = _check_redeemable(session, event_id, raw_code, now)
    discount = discount_for(campaign, total_kzt)
    if discount <= 0:
        raise BusinessRuleError("This promo code gives no discount on this order")
    return AppliedPromo(code_id=code.id, discount_kzt=discount)


def record_redemption(session: Session, order: Order, promo: AppliedPromo) -> None:
    """Write down that the order used the code (same transaction as the order)."""
    repository.add(
        session,
        PromoRedemption(
            promo_code_id=promo.code_id, order_id=order.id, discount_kzt=promo.discount_kzt
        ),
    )
