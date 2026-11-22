"""BE-18 rendering basics (language, escaping, fallback, strictness), on support_reply: the
template with the smallest context. Every template is covered in test_templates.py."""

import pytest
from jinja2 import UndefinedError

from app.modules.notifications.rendering import TEMPLATES_DIR, build_email


def reply(**overrides: str) -> dict:
    return {
        "case_subject": "Джаз",
        "message_excerpt": "Билет отправлен.",
        "case_url": "https://ticketoff.store/support/cases/7d0c8a4e-1b2f-4c3d-9e8f-0a1b2c3d4e5f",
        **overrides,
    }


def test_renders_in_russian_and_english() -> None:
    ru = build_email("a@example.com", "support_reply", "ru", reply())
    en = build_email("a@example.com", "support_reply", "en", reply(case_subject="Jazz"))

    assert ru.subject == "Ответ поддержки: Джаз"
    assert "обращение «Джаз»" in ru.text and "«Джаз»" in ru.html
    assert en.subject == "Support replied: Jazz"
    assert 'request "Jazz"' in en.text


def test_html_is_escaped_text_is_not() -> None:
    email = build_email("a@example.com", "support_reply", "en", reply(case_subject="<b>x</b>"))

    assert "&lt;b&gt;x&lt;/b&gt;" in email.html and "<b>x</b>" not in email.html
    assert "<b>x</b>" in email.text


def test_unknown_language_falls_back_to_russian() -> None:
    email = build_email("a@example.com", "support_reply", "kk", reply())

    assert email.subject == "Ответ поддержки: Джаз"
    assert '<html lang="ru">' in email.html


def test_missing_variable_is_an_error_not_an_empty_email() -> None:
    context = reply()
    del context["case_url"]

    with pytest.raises(UndefinedError):
        build_email("a@example.com", "support_reply", "en", context)


def test_no_leftover_sample_template() -> None:
    assert not list(TEMPLATES_DIR.rglob("sample.*"))
