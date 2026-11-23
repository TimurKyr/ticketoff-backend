"""event slugs (BE-35)

events.slug: the readable link /events/{slug}, unique, NULL for private events. Existing
non-private events get one from their title (earliest start first gets the plain slug,
the next ones -2, -3...).

The transliteration is a frozen copy of app.modules.events.slugs at the time of writing,
so a later change there does not change what this migration did.

Revision ID: 9a95f0f62416
Revises: b8d2f6a41c97
Create Date: 2026-11-24 12:00:00.000000

"""
import re
import unicodedata
import uuid
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a95f0f62416'
down_revision: str | None = 'b8d2f6a41c97'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX = 80
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
    "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u", "ү": "u", "һ": "h", "і": "i",
}  # fmt: skip


def _slugify(title: str) -> str:
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in title.lower())
    # é -> e: the letters left after the table lose their accents.
    latin = unicodedata.normalize("NFKD", latin).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")[:_MAX].rstrip("-")
    if not slug:
        return "event"
    try:
        uuid.UUID(slug)
    except ValueError:
        return slug
    return f"event-{slug}"[:_MAX]


def _with_suffix(base: str, n: int) -> str:
    if n == 1:
        return base
    suffix = f"-{n}"
    return base[: _MAX - len(suffix)].rstrip("-") + suffix


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('events', sa.Column('slug', sa.String(length=80), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, title FROM events WHERE visibility <> 'private' "
            "ORDER BY start_at, id"
        )
    ).all()
    taken: set[str] = set()
    for event_id, title in rows:
        base = _slugify(title)
        n = 1
        while _with_suffix(base, n) in taken:
            n += 1
        slug = _with_suffix(base, n)
        taken.add(slug)
        bind.execute(
            sa.text("UPDATE events SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": event_id},
        )

    op.create_unique_constraint(op.f('uq_events_slug'), 'events', ['slug'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('uq_events_slug'), 'events', type_='unique')
    op.drop_column('events', 'slug')
