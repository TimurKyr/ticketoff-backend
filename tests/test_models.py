from sqlalchemy.orm import configure_mappers

import app.modules.registry  # noqa: F401
from app.common.base import Base


def test_models_configure_and_register_all_tables() -> None:
    configure_mappers()  # raises if a relationship points at a missing or misnamed class

    assert set(Base.metadata.tables) == {
        "attendees",
        "events",
        "order_items",
        "orders",
        "organizer_profiles",
        "payout_accounts",
        "staff_assignments",
        "ticket_types",
        "tickets",
        "users",
        "venues",
    }
