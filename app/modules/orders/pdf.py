"""Printable A4 ticket.

Pure rendering: the service loads the ticket and prepares every value (local start time,
venue and seat lines); this module only lays them out. The QR encodes exactly the stored
qr_token, so the printed copy and the phone copy are the same entry.

Deliberately not on the ticket: email, buyer details, price, order id, payment info, and
the raw token as text. A printed ticket gets left around.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 2 * cm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

# Outer size, quiet zone included. Level Q survives smudges and B/W printing; an 84-char
# token gives version 7 (45 modules), so the code itself is about 5.1 cm (>= 4 cm required).
QR_SIZE = 6 * cm
QR_LEVEL = "Q"
QR_QUIET_ZONE = 4  # modules

REGULAR = "DejaVuSans"
BOLD = "DejaVuSans-Bold"
MONO = "Courier"  # built-in; the ticket id is ASCII only

# Title: largest size that fits in TITLE_MAX_LINES; below the smallest size it is truncated.
TITLE_SIZES = (24, 20, 17, 14)
TITLE_MAX_LINES = 4
# Every other field wraps to at most this many lines, so the page always fits.
FIELD_MAX_LINES = 2

LABEL_WIDTH = 3 * cm
ELLIPSIS = "…"

_FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True, slots=True, kw_only=True)
class TicketPdfData:
    event_title: str
    starts_at_local: datetime  # already in the venue's time zone
    tz_name: str  # e.g. "Asia/Almaty", printed next to the time
    venue_line: str  # "<name>, <address>, <city>", missing parts left out
    ticket_type: str
    guest_name: str
    seat_line: str | None = None  # "<section> · Row <row> · Seat <number>", or None
    ticket_id: uuid.UUID
    qr_token: str


@lru_cache(maxsize=1)
def _register_fonts() -> None:
    """DejaVu ships with the app (Cyrillic and Kazakh letters, no server fonts needed)."""
    pdfmetrics.registerFont(TTFont(REGULAR, str(_FONTS_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(BOLD, str(_FONTS_DIR / "DejaVuSans-Bold.ttf")))


def _format_start(starts_at: datetime, tz_name: str) -> str:
    # Built by hand: strftime's %a / %b depend on the process locale.
    day = f"{_WEEKDAYS[starts_at.weekday()]}, {starts_at.day} {_MONTHS[starts_at.month - 1]}"
    return f"{day} {starts_at.year} · {starts_at:%H:%M} ({tz_name})"


def _fits(text: str, font: str, size: float, width: float) -> bool:
    return pdfmetrics.stringWidth(text, font, size) <= width


def _break_word(line: str, font: str, size: float, width: float) -> list[str]:
    """Split a line that is still too wide (one very long word) character by character."""
    if _fits(line, font, size, width):
        return [line]
    parts: list[str] = []
    current = ""
    for char in line:
        if current and not _fits(current + char, font, size, width):
            parts.append(current)
            current = ""
        current += char
    return [*parts, current]


def _ellipsize(line: str, font: str, size: float, width: float) -> str:
    while line and not _fits(line + ELLIPSIS, font, size, width):
        line = line[:-1]
    return line.rstrip() + ELLIPSIS


def _wrap(
    text: str, font: str, size: float, width: float, max_lines: int | None = None
) -> list[str]:
    lines = [
        part
        for line in simpleSplit(text, font, size, width)
        for part in _break_word(line, font, size, width)
    ] or [""]
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _ellipsize(lines[-1], font, size, width)
    return lines


def _fit_title(title: str) -> tuple[float, list[str]]:
    for size in TITLE_SIZES:
        lines = _wrap(title, BOLD, size, CONTENT_WIDTH)
        if len(lines) <= TITLE_MAX_LINES:
            return size, lines
    size = TITLE_SIZES[-1]
    return size, _wrap(title, BOLD, size, CONTENT_WIDTH, TITLE_MAX_LINES)


def _draw_qr(pdf: Canvas, value: str, x: float, y: float) -> None:
    widget = QrCodeWidget(
        value,
        barLevel=QR_LEVEL,
        barBorder=QR_QUIET_ZONE,
        barWidth=QR_SIZE,
        barHeight=QR_SIZE,
        barFillColor=black,
    )
    drawing = Drawing(QR_SIZE, QR_SIZE)
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x, y)  # vector rectangles, sharp at any printer DPI


def render_ticket_pdf(data: TicketPdfData) -> bytes:
    """One A4 page with the event, the guest and a scannable QR of data.qr_token."""
    _register_fonts()
    buffer = BytesIO()
    pdf = Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"Ticket: {data.event_title}")
    pdf.setAuthor("ticketoff")
    pdf.setFillColor(black)
    pdf.setStrokeColor(black)
    left, right, center = MARGIN, PAGE_WIDTH - MARGIN, PAGE_WIDTH / 2

    # Header
    y = PAGE_HEIGHT - MARGIN - 14
    pdf.setFont(BOLD, 16)
    pdf.drawString(left, y, "ticketoff")
    pdf.setFont(BOLD, 10)
    pdf.drawRightString(right, y, "E-TICKET")
    y -= 10
    pdf.setLineWidth(1)
    pdf.line(left, y, right, y)

    # Event
    y -= 8
    title_size, title_lines = _fit_title(data.event_title)
    pdf.setFont(BOLD, title_size)
    for line in title_lines:
        y -= title_size * 1.2
        pdf.drawString(left, y, line)

    y -= 22
    pdf.setFont(REGULAR, 12)
    pdf.drawString(left, y, _format_start(data.starts_at_local, data.tz_name))
    pdf.setFont(REGULAR, 11)
    for line in _wrap(data.venue_line, REGULAR, 11, CONTENT_WIDTH, FIELD_MAX_LINES):
        y -= 15
        pdf.drawString(left, y, line)

    # Ticket details
    y -= 12
    details = [("Ticket type", data.ticket_type), ("Guest", data.guest_name)]
    if data.seat_line:
        details.append(("Seat", data.seat_line))
    value_width = CONTENT_WIDTH - LABEL_WIDTH
    for label, value in details:
        y -= 24
        pdf.setFont(REGULAR, 10)
        pdf.drawString(left, y, label)
        pdf.setFont(BOLD, 12)
        lines = _wrap(value, BOLD, 12, value_width, FIELD_MAX_LINES)
        for index, line in enumerate(lines):
            pdf.drawString(left + LABEL_WIDTH, y - index * 16, line)
        y -= 16 * (len(lines) - 1)

    # QR, centred
    y -= 20 + QR_SIZE
    _draw_qr(pdf, data.qr_token, center - QR_SIZE / 2, y)

    y -= 6
    pdf.setFont(REGULAR, 9)
    pdf.drawCentredString(center, y, "Ticket ID")
    y -= 15
    pdf.setFont(MONO, 11)
    pdf.drawCentredString(center, y, str(data.ticket_id))

    # Footer
    y -= 30
    pdf.setFont(REGULAR, 9)
    pdf.drawCentredString(center, y, "One ticket = one entry.")
    pdf.drawCentredString(center, y - 13, "Printed and phone copies are the same ticket.")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
