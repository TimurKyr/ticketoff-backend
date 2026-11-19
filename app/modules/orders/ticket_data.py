"""What a printed ticket shows, built from a loaded ticket.

Shared by the PDF download (orders.service) and the PDFs attached to the order email
(notifications.flows), so both print the same thing.
"""

from zoneinfo import ZoneInfo

from app.modules.events.models import Event, Seat, Venue
from app.modules.orders.models import Ticket
from app.modules.orders.pdf import TicketPdfData


def venue_line(venue: Venue) -> str:
    return ", ".join(part for part in (venue.name, venue.address, venue.city) if part)


def seat_line(seat: Seat | None) -> str | None:
    if seat is None:
        return None
    return f"{seat.row.section.name} · Row {seat.row.label} · Seat {seat.number}"


def ticket_pdf_data(ticket: Ticket, event: Event) -> TicketPdfData:
    # start_at is stored in UTC; a ticket shows the time at the venue.
    tz_name = event.venue.timezone
    return TicketPdfData(
        event_title=event.title,
        starts_at_local=event.start_at.astimezone(ZoneInfo(tz_name)),
        tz_name=tz_name,
        venue_line=venue_line(event.venue),
        ticket_type=ticket.order_item.ticket_type.name,
        guest_name=ticket.attendee.name,
        seat_line=seat_line(ticket.seat),
        ticket_id=ticket.id,
        qr_token=ticket.qr_token,
    )
