import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from app.common.errors import InvalidInputError
from app.common.pagination import Limit, Offset
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.ops.enums import SupportKind, SupportStatus
from app.modules.support import service
from app.modules.support.attachments import (
    MAX_BYTES,
    MAX_FILES,
    IncomingFile,
    content_disposition,
)
from app.modules.support.schemas import (
    CaseCreate,
    CaseDetailOut,
    CasePage,
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
    summary="Reply in a case, with up to 3 files (PNG, JPEG, PDF; 5 MB each)",
)
def add_message(
    case_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    body: Annotated[str, Form(max_length=5000, description="The message text")],
    files: Annotated[list[UploadFile | str] | None, File(description="Up to 3 files")] = None,
) -> MessageOut:
    """multipart/form-data (a plain form without files works too), not JSON.

    More than 3 files, a file that is not really a PNG, JPEG or PDF, or an empty one: 422.
    A file over 5 MB: 413. One bad file refuses the whole message.
    """
    # Count first, read after: a request with hundreds of parts must be refused before any
    # of them is read into memory. A browser sends an empty part when no file was chosen,
    # and that one does not count.
    uploads: list[UploadFile] = []
    for part in files or []:
        if isinstance(part, str):
            # An empty file input arrives as a plain empty field; anything else is not a file.
            if part:
                raise InvalidInputError("files must be files")
        elif part.filename or part.size:
            uploads.append(part)
    if len(uploads) > MAX_FILES:
        raise InvalidInputError(f"At most {MAX_FILES} files per message")
    incoming = [
        # One byte over the limit is enough to refuse a file.
        IncomingFile(upload.filename, upload.file.read(MAX_BYTES + 1))
        for upload in uploads
    ]
    return service.add_message(session, user, case_id, body, incoming)


@router.get(
    "/attachments/{attachment_id}",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "The file"}},
    summary="Download a file of a message (whoever can see the case)",
)
def download_attachment(
    attachment_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    file = service.get_attachment(session, user, attachment_id)
    return Response(
        content=file.data,
        media_type=file.content_type,
        headers={
            "Content-Disposition": content_disposition(file.filename),
            # The browser must not guess a type and run the file; and it is private data.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


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
