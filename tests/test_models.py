from sqlalchemy.orm import configure_mappers

import app.modules.registry  # noqa: F401
from app.common.base import Base


def test_models_configure_and_register_all_tables() -> None:
    configure_mappers()  # raises if a relationship points at a missing or misnamed class

    assert set(Base.metadata.tables) == {
        "attendees",
        "audit_logs",
        "check_in_records",
        "events",
        "notifications",
        "order_items",
        "orders",
        "organizer_profiles",
        "payments",
        "payout_accounts",
        "promo_codes",
        "promo_redemptions",
        "promotional_campaigns",
        "refunds",
        "rows",
        "seat_holds",
        "seats",
        "staff_assignments",
        "support_cases",
        "support_messages",
        "ticket_types",
        "tickets",
        "users",
        "venue_sections",
        "venues",
    }
