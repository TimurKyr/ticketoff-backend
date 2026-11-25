"""Report periods given as calendar days in Kazakhstan (BE-26, reused by R-118).

`from_date` and `to_date` are both inclusive days in Asia/Almaty. They become a half-open
range of aware datetimes [start, end): start is midnight of from_date, end is midnight of the
day after to_date. A missing day leaves that side open.
"""

from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Query

from app.common.errors import InvalidInputError

REPORT_TZ = ZoneInfo("Asia/Almaty")

ReportDate = Annotated[
    date | None, Query(description="Day in Kazakhstan time (Asia/Almaty), inclusive")
]


def day_bounds(
    from_date: date | None, to_date: date | None
) -> tuple[datetime | None, datetime | None]:
    """(start, end) for created_at >= start and created_at < end. from_date > to_date is 422."""
    if from_date is not None and to_date is not None and from_date > to_date:
        raise InvalidInputError("from_date must not be after to_date")
    start = datetime.combine(from_date, time(), REPORT_TZ) if from_date else None
    end = datetime.combine(to_date + timedelta(days=1), time(), REPORT_TZ) if to_date else None
    return start, end
