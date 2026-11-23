import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.modules.events.dependencies import require_event_owner
from app.modules.events.models import Event
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.promotions import service
from app.modules.promotions.schemas import (
    CampaignCreate,
    CampaignOut,
    CodeCreate,
    CodeOut,
    CodeUpdate,
    PromoCheckIn,
    PromoCheckOut,
    PromoQrOut,
)

campaigns_router = APIRouter(prefix="/events/{event_id}/campaigns", tags=["campaigns"])
promo_router = APIRouter(tags=["promo codes"])

# The path's event, if the logged-in organizer owns it; otherwise 404 (403 if not an organizer).
OwnedEventDep = Annotated[Event, Depends(require_event_owner)]


@campaigns_router.post(
    "",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a promo campaign for my event",
)
def create_campaign(
    payload: CampaignCreate, event: OwnedEventDep, user: CurrentUser, session: SessionDep
) -> CampaignOut:
    return service.create_campaign(session, user, event, payload)


@campaigns_router.get("", response_model=list[CampaignOut], summary="Campaigns of my event")
def list_campaigns(event: OwnedEventDep, session: SessionDep) -> list[CampaignOut]:
    return service.list_campaigns(session, event)


@campaigns_router.post(
    "/{campaign_id}/codes",
    response_model=CodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a promo code (made up if you do not send one) with its campaign QR token",
)
def create_code(
    campaign_id: uuid.UUID,
    payload: CodeCreate,
    event: OwnedEventDep,
    user: CurrentUser,
    session: SessionDep,
) -> CodeOut:
    return service.create_code(session, user, event, campaign_id, payload)


@campaigns_router.get(
    "/{campaign_id}/codes",
    response_model=list[CodeOut],
    summary="Codes of a campaign, with their status and how many times each was used",
)
def list_codes(campaign_id: uuid.UUID, event: OwnedEventDep, session: SessionDep) -> list[CodeOut]:
    return service.list_codes(session, event, campaign_id)


@campaigns_router.patch(
    "/{campaign_id}/codes/{code_id}",
    response_model=CodeOut,
    summary="Disable or re-enable a promo code",
)
def update_code(
    campaign_id: uuid.UUID,
    code_id: uuid.UUID,
    payload: CodeUpdate,
    event: OwnedEventDep,
    user: CurrentUser,
    session: SessionDep,
) -> CodeOut:
    return service.update_code(session, user, event, campaign_id, code_id, payload)


@campaigns_router.get(
    "/{campaign_id}/codes/{code_id}/qr.png",
    response_class=Response,
    responses={
        200: {
            "description": "The campaign QR as a PNG picture (not an entry ticket)",
            "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
    summary="Campaign QR of a promo code as a PNG",
)
def get_code_qr(
    campaign_id: uuid.UUID, code_id: uuid.UUID, event: OwnedEventDep, session: SessionDep
) -> Response:
    return Response(
        content=service.code_qr_png(session, event, campaign_id, code_id),
        media_type="image/png",
    )


@promo_router.post(
    "/events/{event_id}/promo-codes/check",
    response_model=PromoCheckOut,
    # Without `items` the answer has exactly the fields it had before BE-29, plus
    # ticket_type_ids; eligible_subtotal_kzt and discount_kzt come only with items.
    response_model_exclude_none=True,
    summary="Check a promo code before paying (does not use it up)",
)
def check_code(
    event_id: uuid.UUID, payload: PromoCheckIn, _: CurrentUser, session: SessionDep
) -> PromoCheckOut:
    return service.check_code(session, event_id, payload.code, payload.items)


@promo_router.get(
    "/promo-qr/{qr_token}",
    response_model=PromoQrOut,
    summary="Where a scanned campaign QR leads (no login)",
)
def resolve_qr(qr_token: str, session: SessionDep) -> PromoQrOut:
    return service.resolve_qr(session, qr_token)
