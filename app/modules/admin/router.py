"""Platform admin endpoints (BE-22, BE-26). Every route needs system_role = platform_admin:
no token → 401, any other user → 403."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.common.pagination import Limit, Offset
from app.modules.admin import service
from app.modules.admin.schemas import (
    AdminUserOut,
    PaymentPage,
    PlatformSettingsOut,
    PlatformSettingsUpdate,
    PromoActivityPage,
    RefundPage,
    SearchPage,
    SearchType,
)
from app.modules.events import service as events_service
from app.modules.events.models import Event
from app.modules.events.schemas import EventOut
from app.modules.identity.dependencies import SessionDep, require_platform_admin
from app.modules.identity.models import User
from app.modules.payments.enums import PaymentPurpose, PaymentStatus, RefundStatus

AdminDep = Annotated[User, Depends(require_platform_admin)]

admin_router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)]
)


@admin_router.post(
    "/events/{event_id}/suspend", response_model=EventOut, summary="Stop new sales of an event"
)
def suspend_event(event_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> Event:
    """Free registration, checkout and pay answer 409 until unsuspended. Tickets already
    issued stay valid and scan at the door. Repeating it changes nothing."""
    event = service.set_event_sales_suspended(session, admin, event_id, True)
    events_service.annotate_on_sale(session, [event])
    return event


@admin_router.post(
    "/events/{event_id}/unsuspend", response_model=EventOut, summary="Restore sales of an event"
)
def unsuspend_event(event_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> Event:
    event = service.set_event_sales_suspended(session, admin, event_id, False)
    events_service.annotate_on_sale(session, [event])
    return event


@admin_router.post(
    "/users/{user_id}/suspend", response_model=AdminUserOut, summary="Block a user account"
)
def suspend_user(user_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> User:
    """Login and every token of the user answer 403. Not allowed on your own account (409).

    Account only: the events of a suspended organizer keep selling and their tickets stay
    valid. Stop an event's sales separately with POST /admin/events/{event_id}/suspend.
    """
    return service.set_user_suspended(session, admin, user_id, True)


@admin_router.post(
    "/users/{user_id}/unsuspend", response_model=AdminUserOut, summary="Unblock a user account"
)
def unsuspend_user(user_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> User:
    return service.set_user_suspended(session, admin, user_id, False)


@admin_router.get("/search", response_model=SearchPage, summary="Find users, events or orders")
def search(
    session: SessionDep,
    q: Annotated[
        str,
        Query(min_length=1, max_length=100, description="Part of email or title, or an order id"),
    ],
    type: Annotated[
        SearchType,
        Query(
            description="user (by email), event (by title) or order (by order id, buyer's "
            "email or a guest's email)"
        ),
    ],
    limit: Limit = 20,
    offset: Offset = 0,
) -> SearchPage:
    return service.search(session, q, type, limit, offset)


@admin_router.get("/payments", response_model=PaymentPage, summary="All payments, newest first")
def list_payments(
    session: SessionDep,
    status: PaymentStatus | None = None,
    purpose: PaymentPurpose | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaymentPage:
    return service.list_payments(
        session, purpose=purpose, status=status, limit=limit, offset=offset
    )


@admin_router.get("/refunds", response_model=RefundPage, summary="All refunds, newest first")
def list_refunds(
    session: SessionDep,
    status: RefundStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> RefundPage:
    return service.list_refunds(session, status=status, limit=limit, offset=offset)


@admin_router.get(
    "/activations", response_model=PaymentPage, summary="Activation fee payments, newest first"
)
def list_activations(
    session: SessionDep,
    status: PaymentStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PaymentPage:
    return service.list_payments(
        session, purpose=PaymentPurpose.ACTIVATION, status=status, limit=limit, offset=offset
    )


@admin_router.get("/settings", response_model=PlatformSettingsOut, summary="Platform fees")
def read_settings(session: SessionDep) -> PlatformSettingsOut:
    return service.get_settings_out(session)


@admin_router.patch("/settings", response_model=PlatformSettingsOut, summary="Change platform fees")
def update_settings(
    data: PlatformSettingsUpdate, admin: AdminDep, session: SessionDep
) -> PlatformSettingsOut:
    """Applies from the next request on: new activations pay the new fee (paid ones keep
    theirs), new checkouts hold for the new time. Audited as admin.settings_updated."""
    return service.update_settings(session, admin, data)


ReportDate = Annotated[
    date | None, Query(description="Day in Kazakhstan time (Asia/Almaty), inclusive")
]


@admin_router.get(
    "/reports/{kind}.csv",
    response_class=Response,
    summary="Download payments, refunds or orders as CSV",
    responses={200: {"content": {"text/csv": {}}, "description": "UTF-8 CSV with a BOM"}},
)
def download_report(
    kind: service.ReportKind,
    session: SessionDep,
    from_date: ReportDate = None,
    to_date: ReportDate = None,
) -> Response:
    """One row per record created in the period (oldest first); no dates = everything.
    Timestamps are UTC. Every row has is_simulated: all money on the platform is simulated."""
    filename, body = service.build_report(session, kind, from_date, to_date)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_router.get(
    "/promo-activity",
    response_model=PromoActivityPage,
    summary="Promo code use per campaign, biggest discount first",
)
def promo_activity(
    session: SessionDep,
    from_date: ReportDate = None,
    to_date: ReportDate = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> PromoActivityPage:
    """Campaigns with at least one redemption in the period, counted on paid or free
    orders only (as in the organizer's analytics)."""
    return service.promo_activity(session, from_date, to_date, limit, offset)
