"""PDF tickets for the order email (BE-18).

Up to MAX_ATTACHED_TICKETS tickets: one PDF each (orders.pdf.render_ticket_pdf), named
ticket-<id>.pdf. Above that, nothing is attached (a big email bounces and rendering many
PDFs slows the request) and the email links to the tickets page instead. The PDF carries
the QR; the email text never contains a QR token.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.notifications.links import tickets_url
from app.modules.notifications.providers import Attachment
from app.modules.orders.pdf import TicketPdfData, render_ticket_pdf

MAX_ATTACHED_TICKETS = 10


@dataclass(frozen=True)
class TicketFiles:
    attachments: tuple[Attachment, ...]
    # Set only when the tickets were too many to attach: the email shows this link instead.
    tickets_url: str | None


def ticket_files(tickets: Sequence[TicketPdfData]) -> TicketFiles:
    """PDFs for up to 10 tickets, or no attachments and the account link above that.

    Call it before the commit (render_ticket_pdf is pure; TicketPdfData holds plain values).
    """
    if len(tickets) > MAX_ATTACHED_TICKETS:
        return TicketFiles(attachments=(), tickets_url=tickets_url())
    return TicketFiles(
        attachments=tuple(
            Attachment(filename=f"ticket-{t.ticket_id}.pdf", content=render_ticket_pdf(t))
            for t in tickets
        ),
        tickets_url=None,
    )
