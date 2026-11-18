"""Query parameters shared by paginated list endpoints."""

from typing import Annotated

from fastapi import Query

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
