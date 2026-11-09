import uuid

import pytest
from pydantic import ValidationError

from app.modules.orders.schemas import RegistrationCreate


def item(quantity: int = 1, ticket_type_id: uuid.UUID | None = None) -> dict[str, object]:
    return {"ticket_type_id": str(ticket_type_id or uuid.uuid4()), "quantity": quantity}


def attendee(name: str = "Aruzhan", email: str | None = None) -> dict[str, object]:
    return {"name": name, "email": email}


def body(*items: dict[str, object], attendees: list[dict] | None = None) -> dict[str, object]:
    """Request body; by default one attendee per ticket, so only the tested rule can fail."""
    if attendees is None:
        total = sum(int(i["quantity"]) for i in items if int(i["quantity"]) > 0)
        attendees = [attendee(f"Guest {n}") for n in range(total)]
    return {"items": list(items), "attendees": attendees}


def test_valid_with_matching_attendees() -> None:
    data = RegistrationCreate.model_validate(
        body(item(2), attendees=[attendee(email="a@example.com"), attendee("Dias")])
    )
    assert [a.name for a in data.attendees] == ["Aruzhan", "Dias"]
    assert data.attendees[1].email is None


def test_total_quantity_spans_items() -> None:
    assert RegistrationCreate.model_validate(body(item(2), item(1))).total_quantity == 3


def test_attendees_are_required() -> None:
    with pytest.raises(ValidationError, match="attendees"):
        RegistrationCreate.model_validate({"items": [item(1)]})


def test_empty_items_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate(body())


def test_missing_items_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate({"attendees": [attendee()]})


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_rejected(quantity: int) -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate(body(item(quantity)))


def test_duplicate_ticket_type_rejected() -> None:
    ticket_type_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="only once"):
        RegistrationCreate.model_validate(body(item(1, ticket_type_id), item(2, ticket_type_id)))


@pytest.mark.parametrize("count", [0, 1, 3])
def test_attendee_count_mismatch_rejected(count: int) -> None:
    with pytest.raises(ValidationError, match="total quantity"):
        RegistrationCreate.model_validate(
            body(item(2), attendees=[attendee() for _ in range(count)])
        )


def test_empty_attendee_name_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate(body(item(1), attendees=[attendee("")]))


def test_invalid_attendee_email_rejected() -> None:
    with pytest.raises(ValidationError):
        RegistrationCreate.model_validate(body(item(1), attendees=[attendee(email="not-an-email")]))
