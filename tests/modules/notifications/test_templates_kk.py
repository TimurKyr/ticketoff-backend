"""BE-28: every notification type renders in ru, en and kk, the format filters per language,
and the fallback to ru when a template is missing in the asked language."""

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.modules.identity.enums import Language
from app.modules.notifications import rendering
from app.modules.notifications.formatting import local_datetime, money
from app.modules.notifications.providers import EmailMessage
from app.modules.notifications.rendering import TEMPLATES_DIR, build_email
from app.modules.ops.enums import NotificationType
from tests.modules.notifications.test_templates import CONTEXTS

LANGUAGES = [lang.value for lang in Language]
STARTS = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)  # 18:00 in Almaty (UTC+5)
# Leftovers a broken template would show: unrendered Jinja, Python's None, an empty value
# ("Label: " then the line ends), empty quotes. Headings ("Билеты:") and a line that ends
# with ":" before a link on the next line are fine.
BROKEN = re.compile(r"\{\{|\{%|\bNone\b|: +\n|«»|\"\"")

PAYOUT_ACCOUNT = {
    "stage": "account_verified",
    "event_title": None,
    "link_url": "https://ticketoff.store/organizer",
}


def render(type_: NotificationType, language: str, context: dict | None = None) -> EmailMessage:
    return build_email("a@example.com", type_.value, language, context or CONTEXTS[type_])


def test_languages_are_ru_en_kk() -> None:
    assert LANGUAGES == ["ru", "en", "kk"]


def test_every_sent_type_has_templates_and_a_test_context() -> None:
    # ticket_issued is in the enum since BE-10 but nothing sends it (order_confirmed carries
    # the tickets); every other type must have templates in every language.
    sent = {t for t in NotificationType if t != NotificationType.TICKET_ISSUED}
    assert set(CONTEXTS) == sent
    for language in LANGUAGES:
        for type_ in sent:
            for suffix in (".subject.txt", ".txt", ".html"):
                path = TEMPLATES_DIR / language / f"{type_.value}{suffix}"
                assert path.is_file(), path


# Every sent type in every language, plus payout_status's other stage (the account one).
CASES = [(t, lang, None) for t in CONTEXTS for lang in LANGUAGES] + [
    (NotificationType.PAYOUT_STATUS, lang, "account") for lang in LANGUAGES
]


@pytest.mark.parametrize(("type_", "language", "variant"), CASES)
def test_every_type_renders_in_every_language(
    type_: NotificationType, language: str, variant: str | None
) -> None:
    email = render(type_, language, PAYOUT_ACCOUNT if variant == "account" else None)

    assert email.subject.strip() and email.text.strip() and email.html
    for part in (email.subject, email.text):
        found = BROKEN.search(part)
        assert found is None, (found.group(0) if found else "", part)
    assert "{{" not in email.html and "{%" not in email.html and ">None<" not in email.html
    assert f'<html lang="{language}">' in email.html


def test_kk_texts_are_kazakh_not_the_russian_fallback() -> None:
    order = render(NotificationType.ORDER_CONFIRMED, "kk")

    assert order.subject.startswith("Тапсырыс төленді")
    assert "Бұл — ticketoff-тың автоматты хаты" in order.html  # base.html footer in kk


def test_simulation_is_said_in_every_language() -> None:
    words = {"ru": "симуляция", "en": "simulation", "kk": "симуляция"}
    for language, word in words.items():
        for context in (CONTEXTS[NotificationType.PAYOUT_STATUS], PAYOUT_ACCOUNT):
            email = render(NotificationType.PAYOUT_STATUS, language, context)
            assert word in email.subject.lower() and word in email.text.lower(), language


def test_assignment_email_never_names_the_specialist() -> None:
    for language in LANGUAGES:
        email = render(NotificationType.SUPPORT_ASSIGNMENT, language)
        assert "@" not in email.subject + email.text


# --- the format filters ----------------------------------------------------------------------


def test_money_is_the_same_in_every_language_and_matches_the_web() -> None:
    # The web's formatKzt and Intl ru-RU / kk-KZ: U+00A0 between thousands and before ₸.
    assert money(5000) == "5 000 ₸"
    assert money(1234567) == "1 234 567 ₸"
    assert money(0) == "0 ₸"
    for language in LANGUAGES:
        assert "22 500 ₸" in render(NotificationType.PAYMENT_FAILED, language).text


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ru", "пн, 23 ноября 2026, 18:00 (Asia/Almaty)"),
        ("en", "Mon, 23 Nov 2026, 18:00 (Asia/Almaty)"),
        ("kk", "23 қараша 2026, 18:00 (Asia/Almaty)"),  # as on the web
    ],
)
def test_dates_in_the_venue_time_zone_per_language(language: str, expected: str) -> None:
    assert local_datetime(STARTS, "Asia/Almaty", language) == expected


def test_kk_month_names_follow_the_web() -> None:
    months = [
        local_datetime(datetime(2026, m, 15, 6, tzinfo=UTC), "Asia/Almaty", "kk").split()[1]
        for m in range(1, 13)
    ]
    assert months == [
        "қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым",
        "шілде", "тамыз", "қыркүйек", "қазан", "қараша", "желтоқсан",
    ]  # fmt: skip


# --- fallback to ru ------------------------------------------------------------------------------


@pytest.fixture
def templates_without_kk_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A copy of the templates with kk/order_confirmed.* removed, used by the renderer."""
    copy = tmp_path / "templates"
    shutil.copytree(TEMPLATES_DIR, copy)
    for file in (copy / "kk").glob("order_confirmed.*"):
        file.unlink()
    monkeypatch.setattr(rendering, "TEMPLATES_DIR", copy)
    rendering._env.cache_clear()
    yield
    rendering._env.cache_clear()


def test_a_missing_kk_template_falls_back_to_ru(templates_without_kk_order) -> None:
    email = render(NotificationType.ORDER_CONFIRMED, "kk")

    assert email.subject.startswith("Заказ оплачен")  # the ru template
    assert '<html lang="ru">' in email.html  # with ru formats
    other = render(NotificationType.PAYMENT_FAILED, "kk")
    assert other.subject.startswith("Төлем өтпеді")  # the rest stays kk
