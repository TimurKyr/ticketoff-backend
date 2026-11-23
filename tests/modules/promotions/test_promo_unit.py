"""Parts of BE-16 that need no database: the discount rule and the campaign QR picture."""

from types import SimpleNamespace

import pytest

from app.modules.promotions.enums import DiscountType
from app.modules.promotions.qr_png import render_qr_png
from app.modules.promotions.service import discount_for
from tests.qr import decode_qr_png


def campaign(kind: DiscountType, value: int) -> SimpleNamespace:
    return SimpleNamespace(discount_type=kind, discount_value=value)


@pytest.mark.parametrize(
    ("kind", "value", "total", "expected"),
    [
        (DiscountType.PERCENT, 20, 20000, 4000),
        (DiscountType.PERCENT, 15, 999, 149),  # rounds down, never up
        (DiscountType.FIXED, 3000, 20000, 3000),
        # A payment must be more than 0 KZT, so the buyer always pays at least 1.
        (DiscountType.PERCENT, 100, 20000, 19999),
        (DiscountType.FIXED, 50000, 20000, 19999),
        (DiscountType.FIXED, 500, 1, 0),
        (DiscountType.PERCENT, 50, 0, 0),
    ],
)
def test_discount_for(kind: DiscountType, value: int, total: int, expected: int) -> None:
    assert discount_for(campaign(kind, value), total) == expected  # type: ignore[arg-type]


def decode(png: bytes) -> str:
    return decode_qr_png(png)


def test_png_encodes_exactly_the_token() -> None:
    token = "pc1.Zm9vYmFyLXNlY3JldC10b2tlbi0xMjM0NTY3OA"

    png = render_qr_png(token)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert decode(png) == token


def test_png_differs_per_token() -> None:
    assert render_qr_png("pc1.aaaa") != render_qr_png("pc1.bbbb")


# --- BE-29: the discount only from the items the campaign applies to ---


@pytest.mark.parametrize(
    ("kind", "value", "total", "eligible", "expected"),
    [
        # 20% of the matching 10 000, not of the whole 25 000.
        (DiscountType.PERCENT, 20, 25000, 10000, 2000),
        (DiscountType.PERCENT, 15, 25000, 999, 149),  # same rounding (down) as before
        # Fixed: at most the matching subtotal, even if the order is bigger.
        (DiscountType.FIXED, 3000, 25000, 10000, 3000),
        (DiscountType.FIXED, 15000, 25000, 10000, 10000),
        # The whole-order rule still holds: the buyer pays at least 1 KZT.
        (DiscountType.PERCENT, 100, 10000, 10000, 9999),
        (DiscountType.FIXED, 50000, 10000, 10000, 9999),
        (DiscountType.PERCENT, 50, 25000, 0, 0),  # nothing matching, nothing off
    ],
)
def test_discount_for_eligible_subtotal(
    kind: DiscountType, value: int, total: int, eligible: int, expected: int
) -> None:
    assert discount_for(campaign(kind, value), total, eligible) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("total", [0, 1, 999, 20000])
@pytest.mark.parametrize("kind", [DiscountType.PERCENT, DiscountType.FIXED])
def test_eligible_equal_to_total_is_todays_rule(kind: DiscountType, total: int) -> None:
    # A campaign of every type: eligible subtotal == total gives exactly the old discount.
    c = campaign(kind, 30 if kind == DiscountType.PERCENT else 3000)
    assert discount_for(c, total, total) == discount_for(c, total)  # type: ignore[arg-type]
