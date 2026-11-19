"""Stage 4 of BE-18: GET /notifications and POST /notifications/{id}/read."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.main import app
from app.modules.identity.models import User
from app.modules.identity.security import create_access_token
from app.modules.ops.enums import NotificationChannel, NotificationType
from app.modules.ops.models import Notification
from tests.factories import create_user
from tests.markers import postgres_only

pytestmark = postgres_only

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 11, 2, 8, 30, tzinfo=UTC)


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def add(
    session: Session,
    user: User,
    minutes: int,
    *,
    read: bool = False,
    type_: NotificationType = NotificationType.ORDER_CONFIRMED,
    id_: uuid.UUID | None = None,
) -> Notification:
    """A row created `minutes` after T0 (set explicitly: in a test, now() never moves)."""
    row = Notification(
        id=id_ or uuid.uuid4(),
        user_id=user.id,
        type=type_,
        channel=NotificationChannel.IN_APP,
        payload={"order_id": str(uuid.uuid4())},
        created_at=T0 + timedelta(minutes=minutes),
        read_at=T0 + timedelta(days=1) if read else None,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def me(db_session: Session) -> User:
    return create_user(db_session)


@pytest.fixture
def mine(db_session: Session, me: User) -> list[Notification]:
    """5 rows, 2 of them read, plus 3 rows of someone else. Newest is minute 4."""
    rows = [add(db_session, me, m, read=m in (1, 3)) for m in range(5)]
    other = create_user(db_session)
    for m in range(3):
        add(db_session, other, m + 10)  # newer than all of mine: must still not show
    return rows


def ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


# --- GET /notifications ---


def test_lists_only_my_rows_newest_first(
    api: TestClient, me: User, mine: list[Notification]
) -> None:
    response = api.get("/notifications", headers=headers(me))

    assert response.status_code == 200
    body = response.json()
    assert ids(body) == [str(r.id) for r in reversed(mine)]
    assert (body["total"], body["limit"], body["offset"], body["unread_count"]) == (5, 20, 0, 3)
    newest = body["items"][0]
    assert set(newest) == {"id", "type", "payload", "created_at", "read_at"}
    assert newest["type"] == "order_confirmed" and newest["read_at"] is None
    assert newest["payload"] == mine[4].payload
    assert datetime.fromisoformat(newest["created_at"]) == T0 + timedelta(minutes=4)
    assert body["items"][1]["read_at"] is not None


def test_same_created_at_is_ordered_by_id_desc(
    db_session: Session, api: TestClient, me: User
) -> None:
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)
    add(db_session, me, 0, id_=low)
    add(db_session, me, 0, id_=high)

    body = api.get("/notifications", headers=headers(me)).json()

    assert ids(body) == [str(high), str(low)]


def test_paging_keeps_total(api: TestClient, me: User, mine: list[Notification]) -> None:
    first = api.get("/notifications", params={"limit": 2}, headers=headers(me)).json()
    second = api.get("/notifications", params={"limit": 2, "offset": 2}, headers=headers(me)).json()
    last = api.get("/notifications", params={"limit": 2, "offset": 4}, headers=headers(me)).json()
    past = api.get("/notifications", params={"offset": 50}, headers=headers(me)).json()

    newest_first = [str(r.id) for r in reversed(mine)]
    assert ids(first) + ids(second) + ids(last) == newest_first
    assert [p["total"] for p in (first, second, last, past)] == [5, 5, 5, 5]
    assert (second["limit"], second["offset"]) == (2, 2)
    assert past["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_bad_paging_is_422(api: TestClient, me: User, params: dict) -> None:
    assert api.get("/notifications", params=params, headers=headers(me)).status_code == 422


def test_unread_filter(api: TestClient, me: User, mine: list[Notification]) -> None:
    body = api.get("/notifications", params={"unread": "true"}, headers=headers(me)).json()

    assert ids(body) == [str(mine[m].id) for m in (4, 2, 0)]
    assert all(item["read_at"] is None for item in body["items"])
    assert (body["total"], body["unread_count"]) == (3, 3)

    everything = api.get("/notifications", params={"unread": "false"}, headers=headers(me))
    assert everything.json()["total"] == 5


def test_unread_count_ignores_the_filter_and_paging(
    api: TestClient, me: User, mine: list[Notification]
) -> None:
    body = api.get(
        "/notifications", params={"unread": "true", "limit": 1, "offset": 1}, headers=headers(me)
    ).json()

    assert ids(body) == [str(mine[2].id)]
    assert (body["total"], body["unread_count"]) == (3, 3)


def test_empty_list(api: TestClient, me: User) -> None:
    body = api.get("/notifications", headers=headers(me)).json()

    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0, "unread_count": 0}


def test_list_needs_a_token(api: TestClient) -> None:
    assert api.get("/notifications").status_code == 401


# --- POST /notifications/{id}/read ---


def test_read_sets_read_at_and_updates_the_counts(
    db_session: Session, api: TestClient, me: User, mine: list[Notification]
) -> None:
    target = mine[4]

    response = api.post(f"/notifications/{target.id}/read", headers=headers(me))

    assert response.status_code == 200
    item = response.json()
    assert item["id"] == str(target.id) and item["read_at"] is not None
    db_session.refresh(target)
    assert target.read_at is not None

    body = api.get("/notifications", params={"unread": "true"}, headers=headers(me)).json()
    assert ids(body) == [str(mine[m].id) for m in (2, 0)]
    assert (body["total"], body["unread_count"]) == (2, 2)
    assert api.get("/notifications", headers=headers(me)).json()["unread_count"] == 2


def test_reading_again_keeps_the_first_read_at(
    db_session: Session, api: TestClient, me: User
) -> None:
    # A known first read_at: in a test transaction now() never moves, so a re-read that
    # overwrote the value would look the same unless the first value is different.
    row = add(db_session, me, 0)
    row.read_at = EARLIER
    db_session.flush()

    for _ in range(2):
        response = api.post(f"/notifications/{row.id}/read", headers=headers(me))
        assert response.status_code == 200
        assert datetime.fromisoformat(response.json()["read_at"]) == EARLIER

    db_session.refresh(row)
    assert row.read_at == EARLIER


def test_first_then_second_read_return_the_same_timestamp(
    api: TestClient, me: User, mine: list[Notification]
) -> None:
    url = f"/notifications/{mine[0].id}/read"

    first = api.post(url, headers=headers(me)).json()["read_at"]
    second = api.post(url, headers=headers(me)).json()["read_at"]

    assert first is not None and first == second


def test_someone_elses_notification_is_404_and_stays_unread(
    db_session: Session, api: TestClient, me: User
) -> None:
    theirs = add(db_session, create_user(db_session), 0)

    response = api.post(f"/notifications/{theirs.id}/read", headers=headers(me))

    assert response.status_code == 404
    db_session.refresh(theirs)
    assert theirs.read_at is None


def test_unknown_notification_is_404(api: TestClient, me: User) -> None:
    response = api.post(f"/notifications/{uuid.uuid4()}/read", headers=headers(me))

    assert response.status_code == 404


def test_not_a_uuid_is_422(api: TestClient, me: User) -> None:
    assert api.post("/notifications/abc/read", headers=headers(me)).status_code == 422


def test_read_needs_a_token(api: TestClient, me: User, mine: list[Notification]) -> None:
    assert api.post(f"/notifications/{mine[0].id}/read").status_code == 401


@pytest.mark.parametrize(("method", "path"), [("PATCH", "/{id}"), ("DELETE", "/{id}")])
def test_no_patch_or_delete(
    api: TestClient, me: User, mine: list[Notification], method: str, path: str
) -> None:
    url = "/notifications" + path.format(id=mine[0].id)

    assert api.request(method, url, headers=headers(me)).status_code in (404, 405)


def test_rows_written_by_a_flow_show_up(db_session: Session, api: TestClient, me: User) -> None:
    from app.modules.notifications import service as notifications

    context = {
        "case_subject": "Не пришёл билет",
        "message_excerpt": "Ответ",
        "case_url": "https://ticketoff.store/account",
    }
    notifications.notify(
        db_session, me, NotificationType.SUPPORT_REPLY, context, payload={"case_id": "1"}
    )

    [item] = api.get("/notifications", headers=headers(me)).json()["items"]
    assert (item["type"], item["payload"]) == ("support_reply", {"case_id": "1"})
