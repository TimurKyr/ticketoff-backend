import uuid

import pytest
from pydantic import ValidationError

from app.modules.orders.schemas import RegistrationCreate


def item(quantity: int = 1, ticket_type_id: uuid.UUID | None = None) -> dict[str, object]:
    return {"ticket_type_id": str(ticket_type_id or uuid.uuid4()), "quantity": quantity}


def attendee(name: str = "Aruzhan", email: str | None = None) -> dict[str, object]:
    return {"name": name, "email": email}


def test_valid_without_attendees() -> None:
    data = RegistrationCreate.model_validate({"items": [item(2), item(1)]})
    assert data.attendees is None
    assert data.total_quantity == 3


def test_valid_with_matching_attendees() -> None:
    data = RegistrationCreate.model_validate(
        {"items": [item(2)], "attendees": [attendee(email="a@example.com"), attendee("Dias")]}
    )
    assert [a.name for a in data.attendees or []] == ["Aruzhan", "Dias"]


def test_empty_items_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate({"items": []})


def test_missing_items_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate({})


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_rejected(quantity: int) -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate({"items": [item(quantity)]})


def test_duplicate_ticket_type_rejected() -> None:
    ticket_type_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="only once"):
        RegistrationCreate.model_validate(
            {"items": [item(1, ticket_type_id), item(2, ticket_type_id)]}
        )


@pytest.mark.parametrize("count", [0, 1, 3])
def test_attendee_count_mismatch_rejected(count: int) -> None:
    with pytest.raises(ValidationError, match="total quantity"):
        RegistrationCreate.model_validate(
            {"items": [item(2)], "attendees": [attendee() for _ in range(count)]}
        )


def test_empty_attendee_name_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate({"items": [item(1)], "attendees": [attendee("")]})


def test_invalid_attendee_email_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate(
            {"items": [item(1)], "attendees": [attendee(email="not-an-email")]}
        )
