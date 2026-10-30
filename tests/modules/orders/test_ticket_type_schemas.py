from datetime import datetime

import pytest
from pydantic import ValidationError

from app.modules.orders.schemas import TicketTypeCreate, TicketTypeUpdate

BASE = {"name": "General", "kind": "paid", "price_kzt": 5000}


def create(**fields: object) -> TicketTypeCreate:
    return TicketTypeCreate.model_validate(BASE | fields)


# --- max_per_order default ---


@pytest.mark.parametrize(
    ("quantity_total", "expected"),
    [(5, 5), (10, 10), (50, 10)],
)
def test_max_per_order_defaults_to_min_of_10_and_quantity(
    quantity_total: int, expected: int
) -> None:
    assert create(quantity_total=quantity_total).max_per_order == expected


def test_explicit_max_per_order_is_kept() -> None:
    assert create(quantity_total=50, max_per_order=3).max_per_order == 3


def test_explicit_max_per_order_above_quantity_is_rejected() -> None:
    with pytest.raises(ValidationError, match="max_per_order cannot exceed quantity_total"):
        create(quantity_total=5, max_per_order=6)


def test_explicit_zero_max_per_order_is_rejected() -> None:
    with pytest.raises(ValidationError):
        create(quantity_total=5, max_per_order=0)


def test_explicit_null_max_per_order_means_no_limit() -> None:
    assert create(quantity_total=5, max_per_order=None).max_per_order is None


# --- timezone-aware sales window ---


def _assert_naive_rejected(exc: pytest.ExceptionInfo[ValidationError]) -> None:
    assert {e["type"] for e in exc.value.errors()} == {"timezone_aware"}


@pytest.mark.parametrize("field", ["sales_start_at", "sales_end_at"])
def test_create_rejects_naive_datetime(field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        create(quantity_total=5, **{field: "2026-11-01T12:00:00"})
    _assert_naive_rejected(exc)


@pytest.mark.parametrize("field", ["sales_start_at", "sales_end_at"])
def test_update_rejects_naive_datetime(field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        TicketTypeUpdate.model_validate({field: "2026-11-01T12:00:00"})
    _assert_naive_rejected(exc)


def test_update_rejects_naive_python_datetime() -> None:
    with pytest.raises(ValidationError) as exc:
        TicketTypeUpdate(sales_start_at=datetime(2026, 11, 1, 12, 0))
    _assert_naive_rejected(exc)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-11-01T12:00:00Z", "2026-11-02T12:00:00"),
        ("2026-11-01T12:00:00", "2026-11-02T12:00:00Z"),
    ],
)
def test_create_rejects_mixed_aware_and_naive(start: str, end: str) -> None:
    with pytest.raises(ValidationError) as exc:
        create(quantity_total=5, sales_start_at=start, sales_end_at=end)
    _assert_naive_rejected(exc)


def test_aware_values_with_different_offsets_compare_by_instant() -> None:
    # 12:00+05:00 is 07:00Z, so it is before 08:00Z despite the larger wall-clock hour.
    data = create(
        quantity_total=5,
        sales_start_at="2026-11-01T12:00:00+05:00",
        sales_end_at="2026-11-01T08:00:00Z",
    )

    assert data.sales_start_at < data.sales_end_at


def test_aware_values_with_different_offsets_reject_inverted_window() -> None:
    # 08:00Z is after 12:00+05:00 (07:00Z), so this window is inverted.
    with pytest.raises(ValidationError, match="sales_start_at must be before sales_end_at"):
        create(
            quantity_total=5,
            sales_start_at="2026-11-01T08:00:00Z",
            sales_end_at="2026-11-01T12:00:00+05:00",
        )
