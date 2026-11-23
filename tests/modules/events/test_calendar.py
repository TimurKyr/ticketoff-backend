"""Calendar export (BE-31): GET /events/{id}/calendar.ics and EventOut.calendar_links."""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.modules.events import calendar
from app.modules.events.models import Event, Venue
from app.modules.identity.models import OrganizerProfile, User
from app.modules.identity.security import create_access_token, hash_password

Headers = dict[str, str]

# 19:30 in Almaty (UTC+5) on 5 Dec 2026 is 14:30 UTC.
START = datetime(2026, 12, 5, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=2, minutes=15)


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


@pytest.fixture
def make_venue(db: Session) -> Callable[..., Venue]:
    def _make(**overrides: object) -> Venue:
        fields = {
            "name": f"Арена {uuid.uuid4().hex[:6]}",
            "address": "пр. Абая, 1",
            "city": "Алматы",
            "timezone": "Asia/Almaty",
            **overrides,
        }
        venue = Venue(**fields)
        db.add(venue)
        db.commit()
        return venue

    return _make


@pytest.fixture
def org(db: Session) -> Headers:
    # Verified: publishing needs a confirmed email since #35 (the factory default too).
    user = User(
        email=f"{uuid.uuid4().hex}@example.com",
        password_hash=hash_password("x" * 12),
        email_verified=True,
    )
    db.add(user)
    db.flush()
    db.add(OrganizerProfile(user_id=user.id, display_name="Organizer"))
    db.commit()
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def _create(client: TestClient, org: Headers, venue: Venue, **overrides: object) -> dict:
    body = {
        "title": "Джаз; вечер, «Live»",
        "description": "Первая строка\nВторая строка " + "очень длинный текст " * 10,
        "venue_id": str(venue.id),
        "start_at": START.isoformat(),
        "end_at": END.isoformat(),
        **overrides,
    }
    response = client.post("/events", json=body, headers=org)
    assert response.status_code == 201, response.text
    return response.json()


def _publish(client: TestClient, org: Headers, event_id: str) -> dict:
    body = {"name": "Standard", "quantity_total": 10}
    assert client.post(f"/events/{event_id}/ticket-types", json=body, headers=org).is_success
    response = client.post(f"/events/{event_id}/publish", headers=org)
    assert response.status_code == 200, response.text
    return response.json()


def _ics(client: TestClient, event_id: str) -> str:
    response = client.get(f"/events/{event_id}/calendar.ics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.headers["content-disposition"].startswith("attachment;")
    return response.content.decode()


def _unfold(text: str) -> list[str]:
    """RFC 5545 3.1: CRLF followed by one space continues the previous line."""
    return text.replace("\r\n ", "").split("\r\n")


def _props(text: str, component: str = "VEVENT") -> dict[str, str]:
    """name(;params) -> value, for the lines inside the first BEGIN:<component>."""
    lines = _unfold(text)
    start = lines.index(f"BEGIN:{component}")
    end = lines.index(f"END:{component}")
    return dict(line.split(":", 1) for line in lines[start + 1 : end])


# --- the file ------------------------------------------------------------------------------


def test_ics_is_a_valid_icalendar_file(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    venue = make_venue()
    event = _create(client, org, venue)
    _publish(client, org, event["id"])

    text = _ics(client, event["id"])

    # CRLF everywhere, nothing longer than 75 octets, folded lines start with one space.
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    raw = text.split("\r\n")[:-1]
    assert all(len(line.encode()) <= 75 for line in raw)
    assert any(line.startswith(" ") for line in raw)  # the long description was folded
    lines = _unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR"
    for name in ("VCALENDAR", "VTIMEZONE", "STANDARD", "VEVENT"):
        assert lines.count(f"BEGIN:{name}") == lines.count(f"END:{name}") == 1
    assert "VERSION:2.0" in lines and any(line.startswith("PRODID:") for line in lines)

    props = _props(text)
    assert props["UID"] == f"event-{event['id']}@ticketoff.store"
    assert props["STATUS"] == "CONFIRMED"
    assert int(props["SEQUENCE"]) >= 0
    assert props["DTSTAMP"].endswith("Z") and props["LAST-MODIFIED"].endswith("Z")
    assert props["SUMMARY"] == "Джаз\\; вечер\\, «Live»"  # TEXT escaping
    assert props["LOCATION"] == f"{venue.name}\\, пр. Абая\\, 1\\, Алматы"
    assert props["DESCRIPTION"].startswith("Первая строка\\nВторая строка")
    assert props["DESCRIPTION"].endswith(f"\\n\\nhttps://ticketoff.store/events/{event['id']}")
    assert props["URL"] == f"https://ticketoff.store/events/{event['id']}"


def test_times_carry_the_venue_time_zone(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    almaty = _create(client, org, make_venue())
    _publish(client, org, almaty["id"])
    props = _props(_ics(client, almaty["id"]))
    assert props["DTSTART;TZID=Asia/Almaty"] == "20261205T193000"
    assert props["DTEND;TZID=Asia/Almaty"] == "20261205T214500"
    tz = _props(_ics(client, almaty["id"]), "VTIMEZONE")
    assert tz["TZID"] == "Asia/Almaty"
    # Kazakhstan moved to one zone, UTC+5, on 1 March 2024.
    standard = _props(_ics(client, almaty["id"]), "STANDARD")
    assert (standard["TZOFFSETFROM"], standard["TZOFFSETTO"]) == ("+0600", "+0500")
    assert standard["DTSTART"] == "20240301T000000"

    # Another venue zone: the same instant in its own local time.
    tokyo = _create(client, org, make_venue(timezone="Asia/Tokyo", city="Tokyo"))
    _publish(client, org, tokyo["id"])
    props = _props(_ics(client, tokyo["id"]))
    assert props["DTSTART;TZID=Asia/Tokyo"] == "20261205T233000"
    assert _props(_ics(client, tokyo["id"]), "STANDARD")["TZOFFSETTO"] == "+0900"


def test_event_without_end_gets_the_default_duration(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    event = _create(client, org, make_venue(), end_at=None)
    _publish(client, org, event["id"])
    props = _props(_ics(client, event["id"]))
    assert props["DTEND;TZID=Asia/Almaty"] == "20261205T213000"


def test_cancelled_event_keeps_its_uid_and_says_cancelled(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    event = _create(client, org, make_venue())
    _publish(client, org, event["id"])
    before = _props(_ics(client, event["id"]))

    assert client.post(f"/events/{event['id']}/cancel", headers=org).status_code == 200

    after = _props(_ics(client, event["id"]))
    assert after["STATUS"] == "CANCELLED"
    assert after["UID"] == before["UID"]
    assert int(after["SEQUENCE"]) >= int(before["SEQUENCE"])
    assert client.get(f"/events/{event['id']}").status_code == 404  # the page is gone


def test_edit_raises_the_sequence(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue], db: Session
) -> None:
    event = _create(client, org, make_venue())
    _publish(client, org, event["id"])
    # SEQUENCE counts seconds: move the last change a minute back, as if made earlier.
    db.execute(
        update(Event)
        .where(Event.id == uuid.UUID(event["id"]))
        .values(updated_at=Event.updated_at - timedelta(minutes=1))
    )
    db.commit()
    before = int(_props(_ics(client, event["id"]))["SEQUENCE"])

    moved = {"start_at": (START + timedelta(days=1)).isoformat(), "end_at": None}
    assert client.patch(f"/events/{event['id']}", json=moved, headers=org).is_success

    after = _props(_ics(client, event["id"]))
    assert int(after["SEQUENCE"]) > before
    assert after["DTSTART;TZID=Asia/Almaty"] == "20261206T193000"


def test_hidden_events_are_404(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    venue = make_venue()
    draft = _create(client, org, venue)
    private = _create(client, org, venue, visibility="private")
    _publish(client, org, private["id"])
    cancelled_draft = _create(client, org, venue)  # cancelled before it was ever public
    assert client.post(f"/events/{cancelled_draft['id']}/cancel", headers=org).is_success
    unlisted = _create(client, org, venue, visibility="unlisted")
    _publish(client, org, unlisted["id"])

    for event_id in (draft["id"], private["id"], cancelled_draft["id"], str(uuid.uuid4())):
        # No login, and also its own organizer: the file is public or nothing.
        assert client.get(f"/events/{event_id}/calendar.ics").status_code == 404
        assert client.get(f"/events/{event_id}/calendar.ics", headers=org).status_code == 404
    assert client.get(f"/events/{unlisted['id']}/calendar.ics").status_code == 200


# --- links in EventOut ---------------------------------------------------------------------


def test_event_out_has_calendar_links(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    venue = make_venue()
    event = _create(client, org, venue)
    assert event["calendar_links"]["ics"].endswith(f"/events/{event['id']}/calendar.ics")
    _publish(client, org, event["id"])

    links = client.get(f"/events/{event['id']}").json()["calendar_links"]
    assert links["ics"] == f"https://api.ticketoff.store/events/{event['id']}/calendar.ics"

    google = urlsplit(links["google"])
    assert (google.netloc, google.path) == ("calendar.google.com", "/calendar/render")
    query = parse_qs(google.query)
    assert query["action"] == ["TEMPLATE"]
    assert query["text"] == ["Джаз; вечер, «Live»"]
    assert query["dates"] == ["20261205T143000Z/20261205T164500Z"]  # UTC
    assert query["ctz"] == ["Asia/Almaty"]
    assert query["location"] == [f"{venue.name}, пр. Абая, 1, Алматы"]
    assert query["details"][0].endswith(f"https://ticketoff.store/events/{event['id']}")

    outlook = urlsplit(links["outlook"])
    assert outlook.netloc == "outlook.live.com"
    query = parse_qs(outlook.query)
    assert query["startdt"] == ["2026-12-05T14:30:00Z"]
    assert query["enddt"] == ["2026-12-05T16:45:00Z"]
    assert query["subject"] == ["Джаз; вечер, «Live»"]

    # Lists carry them too.
    listed = client.get("/events", params={"q": "Джаз"}).json()["items"]
    assert all("calendar_links" in item for item in listed)


def test_long_description_is_cut_in_links_not_in_the_file(
    client: TestClient, org: Headers, make_venue: Callable[..., Venue]
) -> None:
    long = "а" * 3000
    event = _create(client, org, make_venue(), description=long)
    _publish(client, org, event["id"])
    details = parse_qs(urlsplit(event["calendar_links"]["google"]).query)["details"][0]
    assert len(details) < calendar.LINK_DESCRIPTION_MAX + 100
    assert long in _props(_ics(client, event["id"]))["DESCRIPTION"]


# --- helpers -------------------------------------------------------------------------------


def test_fold_never_splits_a_utf8_character() -> None:
    line = "DESCRIPTION:" + "ж" * 100  # 2 octets each
    folded = calendar.fold(line).split("\r\n")
    assert all(len(part.encode()) <= 75 for part in folded)
    assert all(part.startswith(" ") for part in folded[1:])
    assert "".join(p[1:] if i else p for i, p in enumerate(folded)) == line
    assert calendar.fold("SHORT:x") == "SHORT:x"


def test_escape_text() -> None:
    assert calendar.escape_text("a\\b;c,d\ne\r\nf") == "a\\\\b\\;c\\,d\\ne\\nf"


def test_location_skips_empty_parts_and_a_repeated_city() -> None:
    assert calendar.location(Venue(name="Hall", address=None, city=None)) == "Hall"
    venue = Venue(name="Hall", address="Достык 1, Алматы", city="Алматы")
    assert calendar.location(venue) == "Hall, Достык 1, Алматы"
