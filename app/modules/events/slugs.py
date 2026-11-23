"""Readable event links (BE-35): /events/dzhaz-vecher-almaty instead of the UUID.

A slug is made from the title once, when the event is created (or made non-private), and
never follows later title edits, so a shared link keeps working. Private events have none:
their link stays the UUID, which cannot be guessed.
"""

import re
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.events.models import Event

MAX_LENGTH = 80
# A title with nothing transliterable ("🎉", "!!!") still gets a link.
FALLBACK = "event"

# Russian (BGN/PCGN-like, as ticketon.kz) plus the Kazakh letters.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
    "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
}  # fmt: skip

_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Lowercase latin, digits and single hyphens, at most MAX_LENGTH characters."""
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in title.lower())
    # é -> e: the letters left after the table lose their accents.
    latin = unicodedata.normalize("NFKD", latin).encode("ascii", "ignore").decode()
    slug = _NOT_SLUG.sub("-", latin).strip("-")
    slug = slug[:MAX_LENGTH].rstrip("-")
    if not slug:
        return FALLBACK
    if is_uuid(slug):  # GET /events/{key} would read it as an id
        slug = f"{FALLBACK}-{slug}"[:MAX_LENGTH]
    return slug


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def with_suffix(base: str, n: int) -> str:
    """base, base-2, base-3... cut so the whole slug stays within MAX_LENGTH."""
    if n == 1:
        return base
    suffix = f"-{n}"
    return base[: MAX_LENGTH - len(suffix)].rstrip("-") + suffix


def unique_slug(session: Session, title: str) -> str:
    """slugify(title), with -2, -3... when that slug is taken."""
    base = slugify(title)
    stem = base[: MAX_LENGTH - 4].rstrip("-")  # every suffixed form starts with it
    taken = set(session.scalars(select(Event.slug).where(Event.slug.startswith(stem))))
    n = 1
    while with_suffix(base, n) in taken:
        n += 1
    return with_suffix(base, n)
