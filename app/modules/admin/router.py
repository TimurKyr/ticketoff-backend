"""Platform admin endpoints (BE-22). Every route needs system_role = platform_admin:
no token → 401, any other user → 403."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.common.pagination import Limit, Offset
from app.modules.admin import service
from app.modules.admin.schemas import (
    AdminUserOut,
    PaymentPage,
    PlatformSettingsOut,
    RefundPage,
    SearchPage,
    SearchType,
)
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
    return service.set_event_sales_suspended(session, admin, event_id, True)


@admin_router.post(
    "/events/{event_id}/unsuspend", response_model=EventOut, summary="Restore sales of an event"
)
def unsuspend_event(event_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> Event:
    return service.set_event_sales_suspended(session, admin, event_id, False)


@admin_router.post(
    "/users/{user_id}/suspend", response_model=AdminUserOut, summary="Block a user account"
)
def suspend_user(user_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> User:
    """Login and every token of the user answer 403. Not allowed on your own account (409)."""
    return service.set_user_suspended(session, admin, user_id, True)


@admin_router.post(
    "/users/{user_id}/unsuspend", response_model=AdminUserOut, summary="Unblock a user account"
)
def unsuspend_user(user_id: uuid.UUID, admin: AdminDep, session: SessionDep) -> User:
    return service.set_user_suspended(session, admin, user_id, False)


@admin_router.get("/search", response_model=SearchPage, summary="Find users or events")
def search(
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=100, description="Part of email or title")],
    type: Annotated[SearchType, Query(description="user (by email) or event (by title)")],
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


@admin_router.get(
    "/settings", response_model=PlatformSettingsOut, summary="Platform fees (read-only)"
)
def read_settings() -> PlatformSettingsOut:
    return service.get_settings_out()
