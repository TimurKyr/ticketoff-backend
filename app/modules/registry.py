"""Imports every module's models so SQLAlchemy can resolve cross-module relationships.

Alembic (migrations/env.py) and the tests import this. Add new modules here.
"""

import app.modules.audit.models  # noqa: F401
import app.modules.events.models  # noqa: F401
import app.modules.identity.models  # noqa: F401
import app.modules.ops.models  # noqa: F401
import app.modules.orders.models  # noqa: F401
import app.modules.payments.models  # noqa: F401
import app.modules.promotions.models  # noqa: F401
import app.modules.seating.models  # noqa: F401
