"""Support cases and messages (BE-17).

Who sees and handles a case follows its kind, with no new roles:
- an attendee case (a ticket buyer asking about an event) is handled by that event's
  organizer, who is not an admin;
- an organizer case (an organizer asking the platform) is handled by a platform admin;
- a platform admin can handle every case.
Anyone who is neither the opener nor a handler gets 404, so cases are never revealed.

Status: open -> in_progress (a handler takes it) -> waiting_customer (the handler replied)
-> in_progress (the customer replied) -> resolved. A customer reply on a resolved case
reopens it, so a follow-up does not need a second case.

Notifications (BE-18): the customer (the case's opened_by) is told, in the same transaction,
about a handler's reply (support_reply) and about a status change a handler made
(support_status). One notification per action: a reply that also flips the status sends only
the reply. Handlers are never notified, and neither is a customer about their own action.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.common.errors import (
    BusinessRuleError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from app.modules.audit import service as audit
from app.modules.events import service as events_service
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import User
from app.modules.notifications import service as notifications
from app.modules.ops.enums import SupportKind, SupportStatus
from app.modules.ops.models import SupportCase, SupportMessage
from app.modules.support import repository
from app.modules.support.schemas import (
    CaseCreate,
    CaseDetailOut,
    CaseOut,
    CasePage,
    MessageOut,
)


@dataclass(frozen=True, slots=True)
class Viewer:
    """The logged-in user and what they are on this platform."""

    user: User
    organizer_id: uuid.UUID | None
    is_admin: bool


def _viewer(session: Session, user: User) -> Viewer:
    return Viewer(
        user=user,
        organizer_id=repository.organizer_id_of(session, user.id),
        is_admin=user.system_role == SystemRole.PLATFORM_ADMIN,
    )


def _is_handler(session: Session, viewer: Viewer, case: SupportCase) -> bool:
    if viewer.is_admin:
        return True
    if viewer.organizer_id is None or case.kind != SupportKind.ATTENDEE or case.event_id is None:
        return False
    event = repository.get_event(session, case.event_id)
    return event is not None and event.organizer_id == viewer.organizer_id


def _load(
    session: Session, viewer: Viewer, case_id: uuid.UUID, *, for_update: bool = False
) -> tuple[SupportCase, bool, bool]:
    """(case, is_opener, is_handler), or 404 when the user has nothing to do with it."""
    case = repository.get_case(session, case_id, for_update=for_update)
    if case is None:
        raise NotFoundError("Support case not found")
    is_opener = case.opened_by == viewer.user.id
    is_handler = _is_handler(session, viewer, case)
    if not (is_opener or is_handler):
        raise NotFoundError("Support case not found")
    return case, is_opener, is_handler


def _detail(session: Session, case: SupportCase) -> CaseDetailOut:
    messages = [MessageOut.model_validate(m) for m in repository.list_messages(session, case.id)]
    return CaseDetailOut(**CaseOut.model_validate(case).model_dump(), messages=messages)


def _audit(session: Session, user: User, case: SupportCase, action: str, details: dict) -> None:
    audit.log(
        session,
        actor_user_id=user.id,
        action_type=action,
        entity_type="support_case",
        entity_id=case.id,
        event_id=case.event_id,
        details=details,
    )


def _change_status(session: Session, user: User, case: SupportCase, new: SupportStatus) -> bool:
    """Set the status and write the audit row; False when it was already that status."""
    if case.status == new:
        return False
    old = case.status
    case.status = new
    _audit(session, user, case, "support.case_status_changed", {"from": old.value, "to": new.value})
    return True


def _notify_customer(
    session: Session,
    actor: User,
    case: SupportCase,
    *,
    message: str | None = None,
    status: SupportStatus | None = None,
) -> None:
    """Tell the case's customer about a reply (`message`) or a status change (`status`).

    Nothing when the customer is the one acting. The subject is the category label (the case
    has no title of its own), e.g. "Payment".
    """
    if actor.id == case.opened_by:
        return
    customer = repository.get_user(session, case.opened_by)
    if customer is None:
        return
    notifications.notify_support(
        session,
        customer,
        case_id=case.id,
        case_subject=case.category.value.replace("_", " ").capitalize(),
        message=message,
        status=status,
    )


def _assign(session: Session, user: User, case: SupportCase) -> None:
    case.assignee_id = user.id
    _audit(session, user, case, "support.case_assigned", {"assignee_id": str(user.id)})


# --- opening a case -------------------------------------------------------------------------


def _resolve_event_for_attendee(session: Session, user: User, data: CaseCreate) -> uuid.UUID:
    """The event an attendee case is about, found from the event, order or ticket given.

    An order or ticket must be the user's own (someone else's is a plain 404), and what
    they point to must agree with each other.
    """
    event_ids: set[uuid.UUID] = set()
    if data.order_id is not None:
        order = repository.get_order(session, data.order_id)
        if order is None or order.buyer_user_id != user.id:
            raise NotFoundError("Order not found")
        event_ids.add(order.event_id)
    if data.ticket_id is not None:
        owner = repository.get_ticket_owner(session, data.ticket_id)
        if owner is None or owner.buyer_user_id != user.id:
            raise NotFoundError("Ticket not found")
        event_ids.add(owner.event_id)
    if data.event_id is not None:
        if not event_ids:
            # Only an event was given (a question before buying): it must be a public event.
            events_service.get_public_event(session, data.event_id)
        event_ids.add(data.event_id)
    if not event_ids:
        raise InvalidInputError("An attendee case needs an event_id, order_id or ticket_id")
    if len(event_ids) > 1:
        raise InvalidInputError("The event, order and ticket given belong to different events")
    return event_ids.pop()


def _resolve_event_for_organizer(
    session: Session, viewer: Viewer, data: CaseCreate
) -> uuid.UUID | None:
    """An organizer case may mention an event, order or ticket, but only of their own events."""
    if viewer.organizer_id is None:
        raise PermissionDeniedError("Organizer account required")
    event_ids: set[uuid.UUID] = set()
    if data.order_id is not None:
        order = repository.get_order(session, data.order_id)
        if order is None:
            raise NotFoundError("Order not found")
        event_ids.add(order.event_id)
    if data.ticket_id is not None:
        owner = repository.get_ticket_owner(session, data.ticket_id)
        if owner is None:
            raise NotFoundError("Ticket not found")
        event_ids.add(owner.event_id)
    if data.event_id is not None:
        event_ids.add(data.event_id)
    for event_id in event_ids:
        event = repository.get_event(session, event_id)
        if event is None or event.organizer_id != viewer.organizer_id:
            raise NotFoundError("Event not found")  # someone else's event looks like no event
    if len(event_ids) > 1:
        raise InvalidInputError("The event, order and ticket given belong to different events")
    return event_ids.pop() if event_ids else None


def open_case(session: Session, user: User, data: CaseCreate) -> CaseDetailOut:
    viewer = _viewer(session, user)
    if data.kind == SupportKind.ATTENDEE:
        event_id = _resolve_event_for_attendee(session, user, data)
    else:
        event_id = _resolve_event_for_organizer(session, viewer, data)
    case = SupportCase(
        opened_by=user.id,
        kind=data.kind,
        category=data.category,
        status=SupportStatus.OPEN,
        event_id=event_id,
        order_id=data.order_id,
        ticket_id=data.ticket_id,
    )
    repository.add(session, case)
    repository.add(session, SupportMessage(case_id=case.id, sender_id=user.id, body=data.body))
    _audit(
        session,
        user,
        case,
        "support.case_opened",
        {"kind": case.kind.value, "category": case.category.value},
    )
    session.commit()  # the case, its first message and its audit row together
    return _detail(session, case)


# --- reading --------------------------------------------------------------------------------


def get_case(session: Session, user: User, case_id: uuid.UUID) -> CaseDetailOut:
    case, _, _ = _load(session, _viewer(session, user), case_id)
    return _detail(session, case)


def list_my_cases(
    session: Session, user: User, *, status: SupportStatus | None, limit: int, offset: int
) -> CasePage:
    rows, total = repository.list_opened_by(
        session, user.id, status=status, limit=limit, offset=offset
    )
    return CasePage(
        items=[CaseOut.model_validate(c) for c in rows], total=total, limit=limit, offset=offset
    )


def list_inbox(
    session: Session,
    user: User,
    *,
    status: SupportStatus | None,
    kind: SupportKind | None,
    limit: int,
    offset: int,
) -> CasePage:
    viewer = _viewer(session, user)
    if not viewer.is_admin and viewer.organizer_id is None:
        raise PermissionDeniedError("Only organizers and platform admins have a support inbox")
    rows, total = repository.list_inbox(
        session,
        organizer_id=None if viewer.is_admin else viewer.organizer_id,
        status=status,
        kind=kind,
        limit=limit,
        offset=offset,
    )
    return CasePage(
        items=[CaseOut.model_validate(c) for c in rows], total=total, limit=limit, offset=offset
    )


# --- acting on a case -----------------------------------------------------------------------


def add_message(session: Session, user: User, case_id: uuid.UUID, body: str) -> MessageOut:
    case, _, is_handler = _load(session, _viewer(session, user), case_id, for_update=True)
    message = SupportMessage(case_id=case.id, sender_id=user.id, body=body)
    repository.add(session, message)
    if is_handler:
        # A reply from the handling side: it takes the case if nobody has, and now waits
        # for the customer, who gets the reply (not a second email for the status flip).
        if case.assignee_id is None:
            _assign(session, user, case)
        _change_status(session, user, case, SupportStatus.WAITING_CUSTOMER)
        _notify_customer(session, user, case, message=body)
    elif case.status in (SupportStatus.WAITING_CUSTOMER, SupportStatus.RESOLVED):
        # The customer answers (or comes back after the case was resolved).
        back_to = SupportStatus.IN_PROGRESS if case.assignee_id else SupportStatus.OPEN
        _change_status(session, user, case, back_to)
    session.commit()  # the message, any status change and their audit rows together
    return MessageOut.model_validate(message)


def assign_to_me(session: Session, user: User, case_id: uuid.UUID) -> CaseDetailOut:
    viewer = _viewer(session, user)
    case, _, is_handler = _load(session, viewer, case_id, for_update=True)
    if not is_handler:
        raise PermissionDeniedError("Only a handler of this case can take it")
    if case.assignee_id not in (None, user.id) and not viewer.is_admin:
        raise BusinessRuleError("This case is already taken by someone else")
    if case.assignee_id != user.id:
        _assign(session, user, case)
    if case.status == SupportStatus.OPEN:
        _change_status(session, user, case, SupportStatus.IN_PROGRESS)
        _notify_customer(session, user, case, status=case.status)
    session.commit()
    return _detail(session, case)


def set_status(
    session: Session, user: User, case_id: uuid.UUID, new: SupportStatus
) -> CaseDetailOut:
    case, _, is_handler = _load(session, _viewer(session, user), case_id, for_update=True)
    if is_handler:
        if case.assignee_id is None:  # changing the status means taking responsibility
            _assign(session, user, case)
    elif new != SupportStatus.RESOLVED:
        raise PermissionDeniedError("You can only mark your own case as resolved")
    if _change_status(session, user, case, new):
        _notify_customer(session, user, case, status=case.status)
    session.commit()
    return _detail(session, case)
