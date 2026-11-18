"""Parts of BE-16 that need no database: the discount rule and the campaign QR picture."""

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.modules.promotions.enums import DiscountType
from app.modules.promotions.qr_png import render_qr_png
from app.modules.promotions.service import discount_for


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
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return cv2.QRCodeDetector().detectAndDecode(image)[0]


def test_png_encodes_exactly_the_token() -> None:
    token = "pc1.Zm9vYmFyLXNlY3JldC10b2tlbi0xMjM0NTY3OA"

    png = render_qr_png(token)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert decode(png) == token


def test_png_differs_per_token() -> None:
    assert render_qr_png("pc1.aaaa") != render_qr_png("pc1.bbbb")
