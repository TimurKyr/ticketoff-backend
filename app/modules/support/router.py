import uuid

from fastapi import APIRouter, status

from app.common.pagination import Limit, Offset
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.ops.enums import SupportKind, SupportStatus
from app.modules.support import service
from app.modules.support.schemas import (
    CaseCreate,
    CaseDetailOut,
    CasePage,
    MessageCreate,
    MessageOut,
    StatusUpdate,
)

router = APIRouter(prefix="/support", tags=["support"])


@router.post(
    "/cases",
    response_model=CaseDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a support case or report an event (the first message is the text)",
)
def open_case(payload: CaseCreate, user: CurrentUser, session: SessionDep) -> CaseDetailOut:
    return service.open_case(session, user, payload)


@router.get("/cases", response_model=CasePage, summary="Cases I opened, newest first")
def list_my_cases(
    user: CurrentUser,
    session: SessionDep,
    status: SupportStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> CasePage:
    return service.list_my_cases(session, user, status=status, limit=limit, offset=offset)


@router.get(
    "/inbox",
    response_model=CasePage,
    summary="Cases I handle, oldest first: my events' attendee cases, or all cases "
    "(and reports, kind=report) for an admin",
)
def list_inbox(
    user: CurrentUser,
    session: SessionDep,
    status: SupportStatus | None = None,
    kind: SupportKind | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> CasePage:
    return service.list_inbox(session, user, status=status, kind=kind, limit=limit, offset=offset)


@router.get("/cases/{case_id}", response_model=CaseDetailOut, summary="A case with its messages")
def get_case(case_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> CaseDetailOut:
    return service.get_case(session, user, case_id)


@router.post(
    "/cases/{case_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Reply in a case",
)
def add_message(
    case_id: uuid.UUID, payload: MessageCreate, user: CurrentUser, session: SessionDep
) -> MessageOut:
    return service.add_message(session, user, case_id, payload.body)


@router.post(
    "/cases/{case_id}/assign",
    response_model=CaseDetailOut,
    summary="Take a case (handlers only)",
)
def assign_case(case_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> CaseDetailOut:
    return service.assign_to_me(session, user, case_id)


@router.patch(
    "/cases/{case_id}/status",
    response_model=CaseDetailOut,
    summary="Change the status (handlers: in_progress, waiting_customer, resolved; "
    "the customer: resolved only)",
)
def update_status(
    case_id: uuid.UUID, payload: StatusUpdate, user: CurrentUser, session: SessionDep
) -> CaseDetailOut:
    return service.set_status(session, user, case_id, payload.status)
