"""Calendar export (BE-31): the event as an iCalendar file and add-to-calendar links.

The platform writes to nobody's calendar: it serves a file and builds links, and the
user's calendar app does the rest.

The .ics is written by hand (RFC 5545) instead of with a library: one VEVENT and one
VTIMEZONE need only text escaping, 75-octet line folding and CRLF, all below.
"""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.modules.events.enums import EventStatus
from app.modules.events.models import Event, Venue
from app.modules.events.schemas import CalendarLinks
from app.modules.notifications import links

# An event without end_at still needs an end in a calendar.
DEFAULT_DURATION = timedelta(hours=2)
# Google and Outlook links carry the description in the URL: keep it short.
LINK_DESCRIPTION_MAX = 500
# SEQUENCE counts seconds of updated_at from here, so it grows with every change and stays
# far below 2**31.
_SEQUENCE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
# How far back to look for the venue time zone's last offset change (VTIMEZONE).
_TZ_LOOKBACK = timedelta(days=366 * 50)
_PRODID = "-//ticketoff//Events//RU"


def uid(event_id: uuid.UUID) -> str:
    """Stable per event, so a calendar replaces its entry after an edit or a cancel."""
    return f"event-{event_id}@ticketoff.store"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _span(event: Event) -> tuple[datetime, datetime]:
    start = _aware(event.start_at)
    end = _aware(event.end_at) if event.end_at else start + DEFAULT_DURATION
    return start, end


def location(venue: Venue) -> str:
    """'Name, address, city' without empty parts or a repeated city."""
    parts = [venue.name]
    if venue.address:
        parts.append(venue.address)
    if venue.city and venue.city not in (venue.address or ""):
        parts.append(venue.city)
    return ", ".join(parts)


def _description(event: Event, limit: int | None = None) -> str:
    text = (event.description or "").strip()
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    url = links.event_url(event.id)
    return f"{text}\n\n{url}" if text else url


def sequence(event: Event) -> int:
    seconds = (_aware(event.updated_at) - _SEQUENCE_EPOCH).total_seconds()
    return max(0, int(seconds))


# --- links ---


def _utc_compact(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def links_for(event: Event) -> CalendarLinks:
    """Google and Outlook take UTC times, so they show the right time in any zone."""
    start, end = _span(event)
    title = event.title
    where = location(event.venue)
    details = _description(event, LINK_DESCRIPTION_MAX)
    google = "https://calendar.google.com/calendar/render?" + urlencode(
        {
            "action": "TEMPLATE",
            "text": title,
            "dates": f"{_utc_compact(start)}/{_utc_compact(end)}",
            "details": details,
            "location": where,
            "ctz": event.venue.timezone,
        }
    )
    outlook = "https://outlook.live.com/calendar/0/action/compose?" + urlencode(
        {
            "rru": "addevent",
            "subject": title,
            "startdt": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "enddt": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body": details,
            "location": where,
        }
    )
    return CalendarLinks(google=google, outlook=outlook, ics=links.calendar_ics_url(event.id))


# --- the .ics file ---


def escape_text(value: str) -> str:
    """RFC 5545 TEXT: backslash, semicolon, comma and newlines escaped."""
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def fold(line: str) -> str:
    """Split a content line into lines of at most 75 octets, never inside a UTF-8 character.

    Each continuation starts with one space, which counts toward its 75.
    """
    out: list[str] = []
    current, size = "", 0
    for char in line:
        width = len(char.encode())
        if size + width > 75:
            out.append(current)
            current, size = " ", 1
        current += char
        size += width
    out.append(current)
    return "\r\n".join(out)


def _local(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y%m%dT%H%M%S")


def _offset(value: timedelta) -> str:
    sign = "-" if value < timedelta(0) else "+"
    minutes = abs(int(value.total_seconds())) // 60
    return f"{sign}{minutes // 60:02d}{minutes % 60:02d}"


def _last_change(tz: ZoneInfo, before: datetime) -> datetime | None:
    """The UTC instant of the zone's last offset change at or before `before`, or None."""
    offset = before.astimezone(tz).utcoffset()
    probe = before
    while before - probe < _TZ_LOOKBACK:
        earlier = probe - timedelta(days=1)
        if earlier.astimezone(tz).utcoffset() != offset:
            low, high = earlier, probe  # offset differs at low, equals at high
            while high - low > timedelta(seconds=1):
                mid = low + (high - low) / 2
                if mid.astimezone(tz).utcoffset() == offset:
                    high = mid
                else:
                    low = mid
            return high.replace(microsecond=0)
        probe = earlier
    return None


def _observance(tz: ZoneInfo, change: datetime | None, at: datetime) -> list[str]:
    """One STANDARD/DAYLIGHT block: the offset in force at `at`, since `change`."""
    after = at.astimezone(tz)
    to_offset = after.utcoffset()
    if change is None:
        from_offset, onset = to_offset, "19700101T000000"
    else:
        from_offset = (change - timedelta(seconds=1)).astimezone(tz).utcoffset()
        # DTSTART of an observance is local time in the offset in force before it.
        onset = (change.replace(tzinfo=None) + from_offset).strftime("%Y%m%dT%H%M%S")
    kind = "DAYLIGHT" if after.dst() else "STANDARD"
    return [
        f"BEGIN:{kind}",
        f"DTSTART:{onset}",
        f"TZOFFSETFROM:{_offset(from_offset)}",
        f"TZOFFSETTO:{_offset(to_offset)}",
        f"TZNAME:{escape_text(after.tzname() or _offset(to_offset))}",
        f"END:{kind}",
    ]


def vtimezone(tz_name: str, start: datetime, end: datetime) -> list[str]:
    """The zone's rules over the event: the observance at its start plus any change inside.

    Finite observances without RRULE are valid and enough for one event; they come from
    the same tz database that turns the times into local ones, so the two always agree.
    """
    tz = ZoneInfo(tz_name)
    lines = ["BEGIN:VTIMEZONE", f"TZID:{tz_name}"]
    lines += _observance(tz, _last_change(tz, start), start)
    change = _last_change(tz, end)
    if change is not None and change > start:
        lines += _observance(tz, change, end)
    lines.append("END:VTIMEZONE")
    return lines


def ics(event: Event, now: datetime | None = None) -> str:
    """The whole VCALENDAR, CRLF line ends. A cancelled event keeps its UID and gets
    STATUS:CANCELLED, so importing it again removes or strikes out the earlier entry."""
    venue = event.venue
    tz = ZoneInfo(venue.timezone)
    start, end = _span(event)
    status = "CANCELLED" if event.status == EventStatus.CANCELLED else "CONFIRMED"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *vtimezone(venue.timezone, start, end),
        "BEGIN:VEVENT",
        f"UID:{uid(event.id)}",
        f"DTSTAMP:{_utc_compact(now or datetime.now(UTC))}",
        f"LAST-MODIFIED:{_utc_compact(_aware(event.updated_at))}",
        f"SEQUENCE:{sequence(event)}",
        f"DTSTART;TZID={venue.timezone}:{_local(start, tz)}",
        f"DTEND;TZID={venue.timezone}:{_local(end, tz)}",
        f"SUMMARY:{escape_text(event.title)}",
        f"LOCATION:{escape_text(location(venue))}",
        f"DESCRIPTION:{escape_text(_description(event))}",
        f"URL:{links.event_url(event.id)}",
        f"STATUS:{status}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(fold(line) + "\r\n" for line in lines)
