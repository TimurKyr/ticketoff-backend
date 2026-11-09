"""Shared pytest markers."""

import pytest

from app.core.db import engine

# Row locks (FOR UPDATE / NOWAIT), concurrent connections and timezone-aware datetimes
# behave as in production only on Postgres.
postgres_only = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Postgres only")
