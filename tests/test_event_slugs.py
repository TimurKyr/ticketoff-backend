"""Event slugs (BE-35): transliteration and the migration's copy of it.

The API side (GET /events/{slug}) is tested in tests/test_events.py.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest

from app.modules.events import slugs

# --- transliteration ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Джаз-вечер Алматы", "dzhaz-vecher-almaty"),
        ("Щедрый Ёжик и Юля", "shchedryy-ezhik-i-yulya"),
        ("Объявление: съезд", "obyavlenie-sezd"),
        ("Әнші Ғалым, Қазақ ұлттық әні", "anshi-galym-qazaq-ulttyq-ani"),
        ("Өнер үйі Һ і ң", "oner-uyi-h-i-n"),
        ("  Rock & Roll!!! 2026  ", "rock-roll-2026"),
        ("Café — «Ночь»", "cafe-noch"),
        ("---", slugs.FALLBACK),
        ("🎉🎉", slugs.FALLBACK),
        ("", slugs.FALLBACK),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert slugs.slugify(title) == slug


def test_slugify_caps_length_without_a_trailing_hyphen() -> None:
    slug = slugs.slugify("а" * 79 + " б")
    assert len(slug) <= slugs.MAX_LENGTH
    assert not slug.endswith("-")


def test_slug_never_looks_like_an_id() -> None:
    raw = str(uuid.uuid4())
    slug = slugs.slugify(raw)
    assert not slugs.is_uuid(slug)
    assert slug.startswith(f"{slugs.FALLBACK}-")


def test_suffix_keeps_the_length_limit() -> None:
    base = "a" * slugs.MAX_LENGTH
    assert slugs.with_suffix(base, 12) == "a" * (slugs.MAX_LENGTH - 3) + "-12"


def test_migration_backfill_uses_the_same_rules() -> None:
    """The migration has a frozen copy of slugify; it must agree with the app today."""
    path = next(Path("migrations/versions").glob("*_9a95f0f62416_event_slugs.py"))
    spec = importlib.util.spec_from_file_location("event_slugs_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for title in ["Джаз-вечер Алматы", "Әнші Ғалым", "!!!", str(uuid.uuid4()), "я" * 90]:
        assert module._slugify(title) == slugs.slugify(title)
    for n in (1, 2, 15):
        assert module._with_suffix("x" * 80, n) == slugs.with_suffix("x" * 80, n)
