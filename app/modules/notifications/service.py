"""Notifications (BE-18): emails go out only AFTER the database commit.

An action queues its emails on its session with send_after_commit(). When the session's
transaction commits, the after_commit listener below hands them to the provider; if the
transaction rolls back, the queue is dropped, so nothing is sent for an action that did not
happen. The mail server is therefore never contacted while the transaction (and its row
locks) is open: a slow or failing SMTP server can't hold the event lock or undo a payment.

Every send is wrapped: a provider error (including an SMTP timeout) is logged and never
raised, so it can't break the action that already committed. Sync code only.

Delivery is synchronous: after the commit, the emails are sent one by one in the same
request, before the response goes out, each on its own SMTP connection (up to SMTP_TIMEOUT
each). So with SMTP a request waits for its emails. That is fine for one email, but a bulk
send (cancelling an event with many buyers) can make that request slow. Moving delivery to a
background worker is a follow-up.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from app.common.errors import NotFoundError
from app.core.config import get_settings
from app.modules.identity.models import User
from app.modules.notifications import links, repository
from app.modules.notifications.providers import (
    Attachment,
    ConsoleEmailProvider,
    EmailMessage,
    EmailProvider,
    SmtpEmailProvider,
    recipient_domain,
)
from app.modules.notifications.rendering import build_email
from app.modules.notifications.schemas import NotificationOut, NotificationPage
from app.modules.ops.enums import NotificationType, SupportStatus

logger = logging.getLogger("app.notifications")

_OUTBOX = "notifications_email_outbox"  # key in Session.info
_provider: EmailProvider | None = None


# --- provider ---------------------------------------------------------------------------------


def get_provider() -> EmailProvider:
    """The provider from settings (EMAIL_PROVIDER), created on first use."""
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.email_provider == "smtp":
            _provider = SmtpEmailProvider(settings)
        else:
            _provider = ConsoleEmailProvider(log_body=settings.environment == "local")
    return _provider


def set_provider(provider: EmailProvider | None) -> None:
    """Replace the provider (tests install a recording one); None goes back to settings."""
    global _provider
    _provider = provider


# --- sending ----------------------------------------------------------------------------------


def deliver(message: EmailMessage) -> bool:
    """Send one email now. Never raises: a failure is logged and reported as False.

    The log names the recipient's domain and the subject only: never the body, which can
    hold ticket links or a verification token, and never SMTP credentials.
    """
    try:
        get_provider().send(message)
    except Exception:
        logger.exception(
            "notification email failed (to domain %s, subject %r)",
            recipient_domain(message.to),
            message.subject,
        )
        return False
    return True


def send_after_commit(session: Session, message: EmailMessage) -> None:
    """Queue an email on the session; it is sent after the session's next commit.

    The message must already be rendered (plain strings and bytes): after the commit the
    session's objects are expired and the listener can't run SQL.
    """
    # Bind the email to a transaction. A session with no transaction yet fires no rollback
    # event on rollback() or close(), so a queued email would survive and go out with the
    # next, unrelated commit.
    if not session.in_transaction():
        session.begin()
    session.info.setdefault(_OUTBOX, []).append(message)


def queued(session: Session) -> list[EmailMessage]:
    """The emails waiting for this session's commit (read-only view, for tests)."""
    return list(session.info.get(_OUTBOX, ()))


@event.listens_for(Session, "after_commit")
def _send_outbox(session: Session) -> None:
    # Pop first: if a send is slow or fails, the same emails are never sent twice.
    for message in session.info.pop(_OUTBOX, None) or ():
        deliver(message)


@event.listens_for(Session, "after_transaction_end")
def _drop_outbox(session: Session, transaction: SessionTransaction) -> None:
    # Whatever is still queued when the session's outer transaction ends was not committed:
    # a rollback, or close() without a commit (close fires no rollback event, only this one).
    # On a commit, after_commit runs first and has already popped the queue, so nothing sent
    # is touched here. A begin_nested() savepoint ending keeps the queue: no service queues
    # emails inside a savepoint today.
    if transaction.parent is None:
        session.info.pop(_OUTBOX, None)


# --- notify -----------------------------------------------------------------------------------


def notify(
    session: Session,
    user: User,
    type_: NotificationType,
    context: dict,
    *,
    payload: dict | None = None,
    attachments: Sequence[Attachment] | None = None,
    in_app: bool = True,
) -> None:
    """Tell `user` about something that happens in the current transaction.

    Writes the in-app Notification row in the session (it commits or rolls back with the
    action) and queues the email, rendered now in user.language, for after the commit.
    `context` fills the template (see rendering.py for what each type needs); `payload` is
    what the in-app row keeps (IDs, not names: the web app shows current data). `in_app`
    False sends the email only (account verification has no place in the in-app list).

    Everything is rendered before the commit, so the email holds plain strings and bytes. A
    rendering error (a missing context key, a template bug) raises locally and in tests; in
    other environments it is logged and the email is skipped: a broken template must not
    undo a payment. The in-app row is written either way.
    """
    if in_app:
        repository.add_in_app(session, user.id, type_, payload)
    try:
        message = build_email(
            user.email, type_.value, user.language, context, tuple(attachments or ())
        )
    except Exception:
        if get_settings().environment == "local":
            raise
        logger.exception("notification email %s not rendered; skipped", type_.value)
        return
    send_after_commit(session, message)


SUPPORT_EXCERPT_CHARS = 300


def _excerpt(text: str, limit: int = SUPPORT_EXCERPT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def notify_support(
    session: Session,
    user: User,
    *,
    case_id: uuid.UUID,
    case_subject: str,
    message: str | None = None,
    status: SupportStatus | None = None,
) -> None:
    """Tell the case's customer about a support reply or a status change (for BE-17).

    Call it from the support service in the same transaction as the change, with the
    customer (the case's opened_by user), never the agent:

        notify_support(session, customer, case_id=case.id, case_subject=title,
                       message=reply.body)            # an agent replied
        notify_support(session, customer, case_id=case.id, case_subject=title,
                       status=case.status)            # the status changed

    `case_subject` is the case title the customer sees. SupportCase has no subject column
    today; BE-17 decides where it comes from (a new column, or the category label).

    Give exactly one of `message` or `status`. A reply sends support_reply with the first
    300 characters of the message (the full text stays in the app); a status change sends
    support_status. Both write the in-app row (payload {"case_id"}) and queue the email in
    the customer's language for after the commit, like notify(). Internal notes must not
    be passed here: the customer would get them by email.
    """
    if (message is None) == (status is None):
        raise ValueError("notify_support needs exactly one of message or status")
    context: dict = {"case_subject": case_subject, "case_url": links.support_url(case_id)}
    if message is not None:
        type_ = NotificationType.SUPPORT_REPLY
        context["message_excerpt"] = _excerpt(message)
    else:
        type_ = NotificationType.SUPPORT_STATUS
        context["status"] = SupportStatus(status).value
    notify(session, user, type_, context, payload={"case_id": str(case_id)})


# --- in-app list (stage 4) ---------------------------------------------------------------------


def list_my_notifications(
    session: Session, user: User, *, unread_only: bool, limit: int, offset: int
) -> NotificationPage:
    rows, total = repository.page_for_user(
        session, user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationPage(
        items=[NotificationOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        unread_count=repository.unread_count(session, user.id),
    )


def mark_my_notification_read(
    session: Session, user: User, notification_id: uuid.UUID
) -> NotificationOut:
    """Idempotent: the first read sets read_at, later reads keep it. Someone else's or an
    unknown id is 404 (its existence is not revealed)."""
    repository.mark_read(session, user.id, notification_id)
    notification = repository.get_for_user(session, user.id, notification_id)
    if notification is None:  # the UPDATE above matched nothing either
        raise NotFoundError("Notification not found")
    session.commit()
    return NotificationOut.model_validate(notification)
