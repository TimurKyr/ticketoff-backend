from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders.enums import TicketKind
from tests.factories import create_event, create_organizer, create_ticket_type

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"kind": TicketKind.FREE, "price_kzt": 100}, "price_matches_kind"),
        ({"kind": TicketKind.PAID, "price_kzt": 0}, "price_matches_kind"),
        ({"quantity_total": 0, "max_per_order": None}, "quantity_total_positive"),
        ({"max_per_order": 0}, "max_per_order_positive"),
        ({"quantity_total": 5, "max_per_order": 6}, "max_per_order_within_total"),
        ({"sales_start_at": NOW, "sales_end_at": NOW}, "sales_window_valid"),
    ],
)
def test_database_rejects_invalid_ticket_type(
    db_session: Session, fields: dict[str, object], constraint: str
) -> None:
    event = create_event(db_session, create_organizer(db_session))

    with pytest.raises(IntegrityError, match=f"ck_ticket_types_{constraint}"):
        create_ticket_type(db_session, event, **fields)
