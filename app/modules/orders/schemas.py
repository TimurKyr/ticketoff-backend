import uuid
from datetime import datetime
from typing import Self

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from app.modules.orders.enums import TicketKind

DEFAULT_MAX_PER_ORDER = 10


def ticket_type_rule_error(
    *,
    kind: TicketKind,
    price_kzt: int,
    quantity_total: int,
    max_per_order: int | None,
    sales_start_at: datetime | None,
    sales_end_at: datetime | None,
) -> str | None:
    """Cross-field rules for a ticket type. Returns the first violation, or None.

    Mirrors the CHECK constraints on ticket_types; used for create (schema) and
    for update (service, on the merged values).
    """
    if kind == TicketKind.FREE and price_kzt != 0:
        return "Free ticket types must have price_kzt = 0"
    if kind == TicketKind.PAID and price_kzt <= 0:
        return "Paid ticket types must have price_kzt > 0"
    if max_per_order is not None and max_per_order > quantity_total:
        return "max_per_order cannot exceed quantity_total"
    if sales_start_at and sales_end_at and sales_start_at >= sales_end_at:
        return "sales_start_at must be before sales_end_at"
    return None


class TicketTypeCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    kind: TicketKind = TicketKind.FREE
    price_kzt: int = Field(default=0, ge=0)
    quantity_total: int = Field(gt=0)
    # Omitted -> min(DEFAULT_MAX_PER_ORDER, quantity_total); explicit null -> no limit.
    max_per_order: int | None = Field(default=None, ge=1)
    # Aware only: comparing naive and aware datetimes raises TypeError.
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    is_hidden: bool = False

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        if "max_per_order" not in self.model_fields_set:
            self.max_per_order = min(DEFAULT_MAX_PER_ORDER, self.quantity_total)
        error = ticket_type_rule_error(
            kind=self.kind,
            price_kzt=self.price_kzt,
            quantity_total=self.quantity_total,
            max_per_order=self.max_per_order,
            sales_start_at=self.sales_start_at,
            sales_end_at=self.sales_end_at,
        )
        if error:
            raise ValueError(error)
        return self


class TicketTypeUpdate(BaseModel):
    """Partial update: only fields that are sent are applied.

    Cross-field rules are checked by the service against the merged values.
    """

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    kind: TicketKind | None = None
    price_kzt: int | None = Field(default=None, ge=0)
    quantity_total: int | None = Field(default=None, gt=0)
    max_per_order: int | None = Field(default=None, ge=1)
    sales_start_at: AwareDatetime | None = None
    sales_end_at: AwareDatetime | None = None
    is_hidden: bool | None = None

    @field_validator("name", "kind", "price_kzt", "quantity_total", "is_hidden", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        # Only runs for values that were actually sent, so omitting the field is fine.
        if value is None:
            raise ValueError("may not be null")
        return value


class TicketTypeOut(BaseModel):
    """Organizer view: includes hidden types and inventory numbers."""

    id: uuid.UUID
    event_id: uuid.UUID
    name: str
    description: str | None
    kind: TicketKind
    price_kzt: int
    quantity_total: int
    max_per_order: int | None
    sales_start_at: datetime | None
    sales_end_at: datetime | None
    is_hidden: bool
    sold: int
    available: int


class PublicTicketTypeOut(BaseModel):
    """Buyer view of a ticket type that is currently on sale."""

    id: uuid.UUID
    name: str
    description: str | None
    kind: TicketKind
    price_kzt: int
    max_per_order: int | None
    sales_start_at: datetime | None
    sales_end_at: datetime | None
    available: int
