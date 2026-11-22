"""Stage 2 of BE-18: every email template renders in ru and en with the data it is given."""

import uuid
from datetime import UTC, datetime

import pytest
from jinja2 import UndefinedError

from app.modules.notifications.formatting import local_datetime, money
from app.modules.notifications.rendering import TEMPLATES_DIR, build_email
from app.modules.ops.enums import NotificationType, SupportStatus

STARTS = datetime(2026, 11, 21, 14, 0, tzinfo=UTC)  # 19:00 in Almaty (UTC+5)
ENDS = datetime(2026, 11, 21, 17, 0, tzinfo=UTC)
MOVED = datetime(2026, 11, 28, 15, 30, tzinfo=UTC)
RETRY = datetime(2026, 11, 20, 9, 15, tzinfo=UTC)
TZ = "Asia/Almaty"
ORDER_URL = "https://ticketoff.store/orders/0b9d7c1e-0000-4000-8000-000000000001"


def plain(text: str) -> str:
    """Amounts use no-break spaces ("22\u00a0500\u00a0₸"); compare with ordinary ones."""
    return text.replace("\u00a0", " ")


def order_context(**overrides: object) -> dict:
    return {
        "event_title": "Джаз в парке",
        "starts_at": STARTS,
        "tz_name": TZ,
        "venue_line": "Дворец Республики, пр. Достык 56, Алматы",
        "tickets": [
            {"ticket_type": "VIP", "guest_name": "Аружан Ахметова"},
            {"ticket_type": "Standard", "guest_name": "Ерлан Ахметов"},
        ],
        "is_paid": True,
        "total_kzt": 25000,
        "discount_kzt": 2500,
        "paid_kzt": 22500,
        "order_url": ORDER_URL,
        "tickets_url": None,
        **overrides,
    }


CONTEXTS: dict[NotificationType, dict] = {
    NotificationType.ORDER_CONFIRMED: order_context(),
    NotificationType.PAYMENT_FAILED: {
        "event_title": "Джаз в парке",
        "starts_at": STARTS,
        "tz_name": TZ,
        "amount_kzt": 22500,
        "retry_until": RETRY,
        "order_url": ORDER_URL,
    },
    NotificationType.REFUND_PROCESSED: {
        "event_title": "Джаз в парке",
        "starts_at": STARTS,
        "tz_name": TZ,
        "amount_kzt": 12500,
        "reason": "Customer request",
        "order_url": ORDER_URL,
    },
    NotificationType.EVENT_CANCELLED: {
        "event_title": "Джаз в парке",
        "starts_at": STARTS,
        "tz_name": TZ,
        "refund_kzt": 22500,
        "order_urls": [ORDER_URL],
    },
    NotificationType.EVENT_UPDATED: {
        "event_title": "Джаз в парке",
        "tz_name": TZ,
        "changes": [
            {"field": "starts_at", "old": STARTS, "new": MOVED},
            {"field": "ends_at", "old": ENDS, "new": None},
            {"field": "venue", "old": "Дворец Республики", "new": "Almaty Arena"},
        ],
    },
    NotificationType.ACCOUNT_VERIFICATION: {
        "verify_url": "https://ticketoff.store/verify?token=TOKEN",
        "expires_hours": 24,
    },
    NotificationType.SUPPORT_REPLY: {
        "case_subject": "Не пришёл билет",
        "message_excerpt": "Мы отправили билет повторно.",
        "case_url": "https://ticketoff.store/support/cases/7d0c8a4e-1b2f-4c3d-9e8f-0a1b2c3d4e5f",
    },
    NotificationType.SUPPORT_STATUS: {
        "case_subject": "Не пришёл билет",
        "status": SupportStatus.WAITING_CUSTOMER.value,
        "case_url": "https://ticketoff.store/support/cases/7d0c8a4e-1b2f-4c3d-9e8f-0a1b2c3d4e5f",
    },
}

# What each rendered email must say: (subject, phrases in the text), per language.
EXPECTED: dict[tuple[NotificationType, str], tuple[str, tuple[str, ...]]] = {
    (NotificationType.ORDER_CONFIRMED, "ru"): (
        "Заказ оплачен: Джаз в парке",
        (
            "сб, 21 ноября 2026, 19:00 (Asia/Almaty)",
            "Дворец Республики, пр. Достык 56, Алматы",
            "- VIP: Аружан Ахметова",
            "- Standard: Ерлан Ахметов",
            "Сумма: 25 000 ₸",
            "Скидка: 2 500 ₸",
            "Оплачено: 22 500 ₸",
            "Билеты во вложении",
            f"Заказ: {ORDER_URL}",
        ),
    ),
    (NotificationType.ORDER_CONFIRMED, "en"): (
        "Order paid: Джаз в парке",
        ("Sat, 21 Nov 2026, 19:00 (Asia/Almaty)", "Total: 25 000 ₸", "Paid: 22 500 ₸"),
    ),
    (NotificationType.PAYMENT_FAILED, "ru"): (
        "Оплата не прошла: Джаз в парке",
        (
            "Сумма: 22 500 ₸",
            "Повторить оплату можно до пт, 20 ноября 2026, 14:15 (Asia/Almaty)",
            f"Заказ: {ORDER_URL}",
        ),
    ),
    (NotificationType.PAYMENT_FAILED, "en"): (
        "Payment failed: Джаз в парке",
        ("Amount: 22 500 ₸", "You can retry until Fri, 20 Nov 2026, 14:15 (Asia/Almaty)"),
    ),
    (NotificationType.REFUND_PROCESSED, "ru"): (
        "Возврат оформлен: Джаз в парке",
        ("Сумма возврата: 12 500 ₸", "Причина: Customer request", f"Заказ: {ORDER_URL}"),
    ),
    (NotificationType.REFUND_PROCESSED, "en"): (
        "Refund issued: Джаз в парке",
        ("Refund: 12 500 ₸", "Reason: Customer request"),
    ),
    (NotificationType.EVENT_CANCELLED, "ru"): (
        "Событие отменено: Джаз в парке",
        ("Мы полностью вернули оплату: 22 500 ₸.", f"Заказ: {ORDER_URL}"),
    ),
    (NotificationType.EVENT_CANCELLED, "en"): (
        "Event cancelled: Джаз в парке",
        ("We have refunded your payment in full: 22 500 ₸.", f"Order: {ORDER_URL}"),
    ),
    (NotificationType.EVENT_UPDATED, "ru"): (
        "Изменения в событии: Джаз в парке",
        (
            "Начало: сб, 21 ноября 2026, 19:00 (Asia/Almaty) → сб, 28 ноября 2026, 20:30",
            "Окончание: сб, 21 ноября 2026, 22:00 (Asia/Almaty) → не указано",
            "Место: Дворец Республики → Almaty Arena",
        ),
    ),
    (NotificationType.EVENT_UPDATED, "en"): (
        "Event changed: Джаз в парке",
        (
            "Start: Sat, 21 Nov 2026, 19:00 (Asia/Almaty) → Sat, 28 Nov 2026, 20:30",
            "End: Sat, 21 Nov 2026, 22:00 (Asia/Almaty) → not set",
            "Venue: Дворец Республики → Almaty Arena",
        ),
    ),
    (NotificationType.ACCOUNT_VERIFICATION, "ru"): (
        "Подтвердите email в ticketoff",
        ("https://ticketoff.store/verify?token=TOKEN", "Ссылка действует 24 ч."),
    ),
    (NotificationType.ACCOUNT_VERIFICATION, "en"): (
        "Confirm your email for ticketoff",
        ("https://ticketoff.store/verify?token=TOKEN", "valid for 24 h"),
    ),
    (NotificationType.SUPPORT_REPLY, "ru"): (
        "Ответ поддержки: Не пришёл билет",
        (
            "Мы отправили билет повторно.",
            "Открыть обращение: https://ticketoff.store/support/cases/7d0c8a4e-1b2f-4c3d-9e8f-0a1b2c3d4e5f",
        ),
    ),
    (NotificationType.SUPPORT_REPLY, "en"): (
        "Support replied: Не пришёл билет",
        ("Мы отправили билет повторно.", "Open the request: https://"),
    ),
    (NotificationType.SUPPORT_STATUS, "ru"): (
        "Статус обращения: Не пришёл билет",
        ("изменился: ждёт вашего ответа.",),
    ),
    (NotificationType.SUPPORT_STATUS, "en"): (
        "Request status: Не пришёл билет",
        ("changed: waiting for your reply.",),
    ),
}


@pytest.mark.parametrize(("type_", "language"), list(EXPECTED))
def test_every_template_renders_in_both_languages(type_: NotificationType, language: str) -> None:
    email = build_email("a@example.com", type_.value, language, CONTEXTS[type_])
    subject, phrases = EXPECTED[type_, language]

    assert email.subject == subject
    for phrase in phrases:
        assert phrase in plain(email.text), phrase
    assert email.html is not None and email.html.startswith("<!doctype html>")
    assert f'<html lang="{language}">' in email.html
    assert "{{" not in email.text + email.html and "{%" not in email.text + email.html


def test_every_type_we_send_has_all_files_in_both_languages() -> None:
    sent = set(CONTEXTS)
    for language in ("ru", "en"):
        for type_ in sent:
            for suffix in (".subject.txt", ".txt", ".html"):
                assert (TEMPLATES_DIR / language / f"{type_.value}{suffix}").is_file()


def test_free_registration_has_no_amounts() -> None:
    context = order_context(is_paid=False, total_kzt=0, discount_kzt=0, paid_kzt=0)
    email = build_email("a@example.com", "order_confirmed", "ru", context)

    assert email.subject == "Регистрация подтверждена: Джаз в парке"
    assert "₸" not in email.text and "₸" not in email.html


def test_paid_order_without_discount_has_no_discount_line() -> None:
    context = order_context(total_kzt=25000, discount_kzt=0, paid_kzt=25000)
    email = build_email("a@example.com", "order_confirmed", "en", context)

    assert "Discount" not in email.text and "Paid: 25 000 ₸" in plain(email.text)


def test_order_email_links_to_the_tickets_page_when_not_attached() -> None:
    url = "https://ticketoff.store/tickets"
    email = build_email("a@example.com", "order_confirmed", "ru", order_context(tickets_url=url))

    assert f"Все билеты: {url}" in email.text
    assert f'href="{url}"' in email.html and "во вложении" not in email.text


def test_cancelled_free_registration_mentions_no_refund() -> None:
    context = {**CONTEXTS[NotificationType.EVENT_CANCELLED], "refund_kzt": 0}
    email = build_email("a@example.com", "event_cancelled", "en", context)

    assert "refunded" not in email.text and "₸" not in email.text
    assert "Your orders for this event are cancelled" in email.text


def test_refund_without_reason_has_no_reason_line() -> None:
    context = {**CONTEXTS[NotificationType.REFUND_PROCESSED], "reason": None}
    email = build_email("a@example.com", "refund_processed", "ru", context)

    assert "Причина" not in email.text and "Причина" not in email.html


def test_html_typed_by_an_organizer_is_escaped() -> None:
    title = '<script>alert(1)</script> & "Jazz"'
    guest = "<img src=x onerror=alert(1)>"
    context = order_context(
        event_title=title,
        venue_line="<b>Hall</b>",
        tickets=[{"ticket_type": "<i>VIP</i>", "guest_name": guest}],
    )
    email = build_email("a@example.com", "order_confirmed", "en", context)

    for raw in ("<script>", "<img", "<b>Hall</b>", "<i>VIP</i>"):
        assert raw not in email.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;Jazz&#34;" in email.html
    assert "&lt;img src=x onerror=alert(1)&gt;" in email.html
    # The text part is plain text: shown as typed, never interpreted.
    assert title in email.subject and title in email.text


def test_support_message_with_html_is_escaped() -> None:
    context = {**CONTEXTS[NotificationType.SUPPORT_REPLY], "message_excerpt": "<a href=evil>x</a>"}
    email = build_email("a@example.com", "support_reply", "ru", context)

    assert "<a href=evil>" not in email.html and "&lt;a href=evil&gt;" in email.html


@pytest.mark.parametrize("type_", list(CONTEXTS))
def test_a_missing_context_key_fails_loudly(type_: NotificationType) -> None:
    context = dict(CONTEXTS[type_])
    context.pop(next(iter(context)))

    with pytest.raises(UndefinedError):
        build_email("a@example.com", type_.value, "ru", context)


def test_naive_datetime_is_refused() -> None:
    context = {**CONTEXTS[NotificationType.PAYMENT_FAILED], "retry_until": datetime(2026, 11, 20)}

    with pytest.raises(ValueError, match="timezone-aware"):
        build_email("a@example.com", "payment_failed", "ru", context)


def test_formatting_helpers() -> None:
    assert local_datetime(STARTS, TZ, "ru") == "сб, 21 ноября 2026, 19:00 (Asia/Almaty)"
    assert local_datetime(STARTS, "Asia/Aqtobe", "en") == "Sat, 21 Nov 2026, 19:00 (Asia/Aqtobe)"
    assert money(0) == "0\u00a0₸" and money(1234567) == "1\u00a0234\u00a0567\u00a0₸"


def test_unknown_status_label_fails_loudly() -> None:
    context = {**CONTEXTS[NotificationType.SUPPORT_STATUS], "status": "archived"}

    with pytest.raises(UndefinedError):
        build_email("a@example.com", "support_status", "en", context)


def test_no_ticket_id_or_token_needed_by_the_order_email() -> None:
    # The order email takes no QR token at all: the PDF carries it.
    context = order_context()
    context["tickets"] = [{**t, "qr_token": f"QR-{uuid.uuid4().hex}"} for t in context["tickets"]]
    email = build_email("a@example.com", "order_confirmed", "en", context)

    assert "QR-" not in email.text and "QR-" not in email.html


def test_text_layout_keeps_one_line_per_item() -> None:
    # trim_blocks eats the newline after a block tag that ends a line; the templates use
    # `+%}` there. This catches a change that glues lines together again.
    updated = build_email(
        "a@example.com", "event_updated", "en", CONTEXTS[NotificationType.EVENT_UPDATED]
    )
    lines = updated.text.splitlines()
    assert [line.split(":")[0] for line in lines if "→" in line] == ["Start", "End", "Venue"]

    order = build_email("a@example.com", "order_confirmed", "ru", order_context())
    assert order.text.startswith("Ваш заказ оплачен.\n\nСобытие:")
