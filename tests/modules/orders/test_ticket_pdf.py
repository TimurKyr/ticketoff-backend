"""Unit tests for the printable ticket. No DB: render_ticket_pdf is a pure function."""

import uuid
from datetime import datetime

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytest
from reportlab.lib.units import cm

from app.modules.orders.pdf import TicketPdfData, render_ticket_pdf
from app.modules.orders.qr import issue_ticket_token

DPI = 200
MIN_QR_PX = 4 / 2.54 * DPI  # 4 cm at 200 DPI = 315 px
A4_POINTS = (595.28, 841.89)
TEXT_MARGIN = 1 * cm  # the page margin is 2 cm; text must stay well inside the paper


def make_data(**overrides: object) -> TicketPdfData:
    ticket_id = uuid.uuid4()
    fields: dict[str, object] = {
        "event_title": "Jazz Night at the Opera",
        "starts_at_local": datetime(2026, 11, 21, 19, 0),
        "tz_name": "Asia/Almaty",
        "venue_line": "Abay Opera House, Abay Ave 110, Almaty",
        "ticket_type": "Standard",
        "guest_name": "Аружан Сейткали",
        "ticket_id": ticket_id,
        "qr_token": issue_ticket_token(ticket_id),
    } | overrides
    return TicketPdfData(**fields)


def page_text(pdf: bytes) -> str:
    return pdfium.PdfDocument(pdf)[0].get_textpage().get_text_range()


def collapse(text: str) -> str:
    return " ".join(text.split())


def char_boxes(pdf: bytes) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(char, (left, bottom, right, top)) for every visible character on the page."""
    textpage = pdfium.PdfDocument(pdf)[0].get_textpage()
    count = textpage.count_chars()
    text = textpage.get_text_range(0, count)
    return [
        (char, textpage.get_charbox(index))
        for index, char in enumerate(text[:count])
        if not char.isspace()
    ]


def decode_qr(pdf: bytes) -> tuple[str, np.ndarray | None]:
    """Render in grayscale (like a B/W printout) and decode with OpenCV.

    QRCodeDetectorAruco, not the default QRCodeDetector: the default one fails on ~14% of
    valid tokens (also on QRs from other encoders; zxing reads them all), which made these
    tests flaky. The ArUco-based detector decoded 1000 of 1000 random tokens.
    """
    document = pdfium.PdfDocument(pdf)
    image = document[0].render(scale=DPI / 72, grayscale=True).to_numpy()
    if image.ndim == 3:
        image = image[..., 0]
    value, points, _ = cv2.QRCodeDetectorAruco().detectAndDecode(image)
    return value, points


def assert_inside_page(pdf: bytes) -> None:
    width, height = A4_POINTS
    for char, (left, bottom, right, top) in char_boxes(pdf):
        assert TEXT_MARGIN <= left and right <= width - TEXT_MARGIN, (char, left, right)
        assert TEXT_MARGIN <= bottom and top <= height - TEXT_MARGIN, (char, bottom, top)


@pytest.fixture(scope="module")
def data() -> TicketPdfData:
    return make_data()


@pytest.fixture(scope="module")
def pdf(data: TicketPdfData) -> bytes:
    return render_ticket_pdf(data)


# --- document ---


def test_is_one_a4_page(pdf: bytes) -> None:
    assert pdf.startswith(b"%PDF-")
    document = pdfium.PdfDocument(pdf)
    assert len(document) == 1
    assert document[0].get_size() == pytest.approx(A4_POINTS, abs=1)


def test_metadata(pdf: bytes, data: TicketPdfData) -> None:
    metadata = pdfium.PdfDocument(pdf).get_metadata_dict()
    assert metadata["Title"] == f"Ticket: {data.event_title}"
    assert metadata["Author"] == "ticketoff"


# --- text ---


def test_text_is_extractable(pdf: bytes, data: TicketPdfData) -> None:
    text = page_text(pdf)
    for expected in (
        "ticketoff",
        "E-TICKET",
        data.event_title,
        "Sat, 21 Nov 2026 · 19:00 (Asia/Almaty)",
        data.venue_line,
        data.ticket_type,
        "Аружан Сейткали",
        str(data.ticket_id),
        "One ticket = one entry.",
    ):
        assert expected in text


def test_kazakh_letters_are_extractable() -> None:
    data = make_data(
        event_title="Қазақ әуендері кеші",
        guest_name="Әлихан Нұрғалиұлы",
        venue_line="Қазақконцерт, Абылай хан даңғ. 83, Алматы",
        ticket_type="Үлкен зал",
    )
    text = page_text(render_ticket_pdf(data))
    for expected in (data.event_title, data.guest_name, data.venue_line, data.ticket_type):
        assert expected in text


def test_ticket_id_is_in_courier(pdf: bytes) -> None:
    assert b"/BaseFont /Courier" in pdf


def test_seat_line_only_when_given(pdf: bytes) -> None:
    assert "Seat" not in page_text(pdf)

    seated = page_text(render_ticket_pdf(make_data(seat_line="Parterre · Row 3 · Seat 12")))
    assert "Seat" in seated
    assert "Parterre · Row 3 · Seat 12" in seated


def test_no_sensitive_data(pdf: bytes, data: TicketPdfData) -> None:
    text = page_text(pdf)
    assert "@" not in text  # no email
    assert "KZT" not in text and "₸" not in text  # no price
    # The token is the entry credential: only in the QR, never as copyable text.
    assert data.qr_token not in collapse(text).replace(" ", "")


# --- QR ---


def test_token_has_real_length(data: TicketPdfData) -> None:
    assert len(data.qr_token) == 84


def test_qr_decodes_to_the_token(pdf: bytes, data: TicketPdfData) -> None:
    value, _ = decode_qr(pdf)
    assert value == data.qr_token


def test_qr_is_at_least_4_cm(pdf: bytes) -> None:
    value, points = decode_qr(pdf)
    assert value and points is not None
    corners = points.reshape(-1, 2)
    width, height = np.ptp(corners, axis=0)
    assert min(width, height) >= MIN_QR_PX


def test_qr_is_vector(pdf: bytes) -> None:
    # Drawn as rectangles, not embedded as a bitmap.
    assert b"/Subtype /Image" not in pdf


# --- layout ---


def test_long_title_wraps_without_losing_text() -> None:
    title = (
        "Международный фестиваль современной музыки и искусства «Дала Саундс» — "
        "гала-концерт закрытия сезона с участием симфонического оркестра"
    )
    pdf = render_ticket_pdf(make_data(event_title=title))

    assert collapse(title) in collapse(page_text(pdf))
    assert_inside_page(pdf)
    assert decode_qr(pdf)[0]


@pytest.mark.parametrize(
    "field", ["event_title", "venue_line", "ticket_type", "guest_name", "seat_line"]
)
@pytest.mark.parametrize(
    "huge",
    ["слово " * 400, "Ж" * 2000],  # many words; one endless word
    ids=["words", "one-word"],
)
def test_huge_values_stay_on_one_page(field: str, huge: str) -> None:
    data = make_data(**{field: huge})
    pdf = render_ticket_pdf(data)

    assert len(pdfium.PdfDocument(pdf)) == 1
    assert_inside_page(pdf)
    assert "…" in page_text(pdf)  # truncated, visibly
    assert decode_qr(pdf)[0] == data.qr_token
