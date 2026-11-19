"""Jinja filters for emails: dates in the venue's time zone and KZT amounts, per language.

Callers put real values in the context (aware datetimes, ints); the template formats them in
the recipient's language, so a caller never needs to know the language.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

_RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
_RU_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EN_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def local_datetime(value: datetime, tz_name: str, language: str) -> str:
    """'сб, 21 ноября 2026, 19:00 (Asia/Almaty)' / 'Sat, 21 Nov 2026, 19:00 (Asia/Almaty)'.

    Built by hand: strftime's names depend on the process locale.
    """
    if value.tzinfo is None:
        raise ValueError("email datetimes must be timezone-aware")
    local = value.astimezone(ZoneInfo(tz_name))
    if language == "en":
        day = f"{_EN_WEEKDAYS[local.weekday()]}, {local.day} {_EN_MONTHS[local.month - 1]}"
    else:
        day = f"{_RU_WEEKDAYS[local.weekday()]}, {local.day} {_RU_MONTHS[local.month - 1]}"
    return f"{day} {local.year}, {local:%H:%M} ({tz_name})"


def money(amount_kzt: int) -> str:
    """12500 -> '12 500 ₸' (no-break space between thousands)."""
    return f"{amount_kzt:,}".replace(",", " ") + " ₸"
