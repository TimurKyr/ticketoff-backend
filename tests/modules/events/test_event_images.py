"""Event cover images (R-12): upload, replace, remove, who may see them, EventOut.image_url."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.events import images
from app.modules.events.models import Venue
from app.modules.identity.enums import SystemRole
from app.modules.support.storage import LocalDiskStorage
from tests.test_events import (  # noqa: F401 (fixtures)
    Headers,
    UserFactory,
    _create,
    _publish,
    db,
    make_user,
    org,
    other_org,
    venue,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64


def _put(client: TestClient, headers: Headers, event_id: str, data: bytes, name: str = "a.png"):
    return client.put(
        f"/events/{event_id}/image",
        files={"image": (name, data, "image/png")},
        headers=headers,
    )


def _path(url: str) -> str:
    """The API path of an absolute image_url."""
    assert url.startswith("http")
    return "/" + url.split("/", 3)[3]


def _image_id(url: str) -> str:
    return url.rsplit("/", 1)[1]


@pytest.mark.parametrize(
    ("data", "content_type"), [(PNG, images.PNG), (JPEG, images.JPEG), (WEBP, images.WEBP)]
)
def test_upload_sets_image_url_and_serves_the_file(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
    event_image_storage: LocalDiskStorage,
    data: bytes,
    content_type: str,
) -> None:
    event = _create(client, org, venue)
    assert event["image_url"] is None

    response = _put(client, org, event["id"], data, name="photo.exe")  # the name is ignored
    assert response.status_code == 200, response.text
    url = response.json()["image_url"]
    assert url.endswith(f"/events/{event['id']}/image/{_image_id(url)}")
    assert (event_image_storage.root / _image_id(url)).read_bytes() == data

    # The owner sees the draft's image (and in their own event view).
    got = client.get(_path(url), headers=org)
    assert got.status_code == 200
    assert got.content == data
    assert got.headers["content-type"] == content_type
    assert got.headers["cache-control"].startswith("private")
    mine = client.get(f"/organizer/events/{event['id']}", headers=org).json()
    assert mine["image_url"] == url


def test_published_image_is_public_and_draft_image_is_not(
    client: TestClient,
    org: Headers,  # noqa: F811
    other_org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    event = _create(client, org, venue)
    url = _put(client, org, event["id"], PNG).json()["image_url"]

    # Draft: guests, a bad token and other organizers get 404.
    assert client.get(_path(url)).status_code == 404
    assert client.get(_path(url), headers={"Authorization": "Bearer nope"}).status_code == 404
    assert client.get(_path(url), headers=other_org).status_code == 404

    _publish(client, org, event["id"])
    got = client.get(_path(url))
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/events/{event['id']}").json()["image_url"] == url
    # The public list carries it too.
    listed = client.get("/events", params={"q": event["title"], "limit": 100}).json()["items"]
    assert url in {item["image_url"] for item in listed}


def test_admin_sees_a_draft_image(
    client: TestClient,
    db: Session,  # noqa: F811
    make_user: UserFactory,  # noqa: F811
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    admin, admin_headers = make_user()
    admin.system_role = SystemRole.PLATFORM_ADMIN
    db.commit()
    event = _create(client, org, venue)
    url = _put(client, org, event["id"], PNG).json()["image_url"]
    assert client.get(_path(url), headers=admin_headers).status_code == 200


def test_private_event_image_is_not_public(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    event = _create(client, org, venue, visibility="private")
    url = _put(client, org, event["id"], PNG).json()["image_url"]
    _publish(client, org, event["id"])
    assert client.get(_path(url)).status_code == 404
    assert client.get(_path(url), headers=org).status_code == 200


def test_replace_deletes_the_old_file_and_changes_the_url(
    client: TestClient,
    db: Session,  # noqa: F811
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
    event_image_storage: LocalDiskStorage,
) -> None:
    event = _create(client, org, venue)
    _publish(client, org, event["id"])
    old = _put(client, org, event["id"], PNG).json()["image_url"]
    new = _put(client, org, event["id"], JPEG).json()["image_url"]

    assert new != old
    assert not (event_image_storage.root / _image_id(old)).exists()
    assert (event_image_storage.root / _image_id(new)).read_bytes() == JPEG
    assert client.get(_path(old)).status_code == 404  # an old id is gone
    assert client.get(_path(new)).headers["content-type"] == images.JPEG

    actions = db.scalars(
        select(AuditLog.action_type).where(AuditLog.entity_id == uuid.UUID(event["id"]))
    ).all()
    assert actions.count("event.image_updated") == 2


def test_delete_removes_the_image(
    client: TestClient,
    db: Session,  # noqa: F811
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
    event_image_storage: LocalDiskStorage,
) -> None:
    event = _create(client, org, venue)
    url = _put(client, org, event["id"], PNG).json()["image_url"]

    response = client.delete(f"/events/{event['id']}/image", headers=org)
    assert response.status_code == 200, response.text
    assert response.json()["image_url"] is None
    assert not (event_image_storage.root / _image_id(url)).exists()
    assert client.get(_path(url), headers=org).status_code == 404
    # Again: nothing to remove, still 200.
    assert client.delete(f"/events/{event['id']}/image", headers=org).status_code == 200

    removed = db.scalars(
        select(AuditLog).where(
            AuditLog.entity_id == uuid.UUID(event["id"]),
            AuditLog.action_type == "event.image_removed",
        )
    ).all()
    assert len(removed) == 1
    assert removed[0].details == {"image_id": _image_id(url)}


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a" + b"\x00" * 64,  # a real image type we do not accept
        b"MZ\x90\x00" + b"\x00" * 64,  # an .exe named .png
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64,  # RIFF, but not WebP
        b"",
    ],
)
def test_wrong_type_is_refused_by_content(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
    event_image_storage: LocalDiskStorage,
    data: bytes,
) -> None:
    event = _create(client, org, venue)
    response = _put(client, org, event["id"], data, name="cover.png")
    assert response.status_code == 422, response.text
    assert not event_image_storage.root.exists() or not any(event_image_storage.root.iterdir())


def test_too_large_is_refused(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
    event_image_storage: LocalDiskStorage,
) -> None:
    event = _create(client, org, venue)
    data = PNG + b"\x00" * (images.MAX_BYTES - len(PNG) + 1)
    response = _put(client, org, event["id"], data)
    assert response.status_code == 413, response.text
    assert not event_image_storage.root.exists() or not any(event_image_storage.root.iterdir())
    # Exactly the limit is fine.
    assert _put(client, org, event["id"], data[:-1]).status_code == 200


def test_other_organizer_gets_404(
    client: TestClient,
    org: Headers,  # noqa: F811
    other_org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    event = _create(client, org, venue)
    assert _put(client, other_org, event["id"], PNG).status_code == 404
    assert client.delete(f"/events/{event['id']}/image", headers=other_org).status_code == 404
    assert _put(client, {}, event["id"], PNG).status_code == 401


def test_cancelled_event_image_cannot_change(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    event = _create(client, org, venue)
    _put(client, org, event["id"], PNG)
    assert client.post(f"/events/{event['id']}/cancel", headers=org).status_code == 200
    assert _put(client, org, event["id"], JPEG).status_code == 409
    assert client.delete(f"/events/{event['id']}/image", headers=org).status_code == 409


def test_unknown_image_id_is_404(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    event = _create(client, org, venue)
    _publish(client, org, event["id"])
    assert client.get(f"/events/{event['id']}/image/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/events/{uuid.uuid4()}/image/{uuid.uuid4()}").status_code == 404


def test_duplicate_gets_its_own_copy_of_the_image(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
    event_image_storage: LocalDiskStorage,
) -> None:
    event = _create(client, org, venue)
    url = _put(client, org, event["id"], WEBP).json()["image_url"]
    copy = client.post(f"/events/{event['id']}/duplicate", headers=org).json()

    assert copy["image_url"] is not None
    assert _image_id(copy["image_url"]) != _image_id(url)
    assert (event_image_storage.root / _image_id(copy["image_url"])).read_bytes() == WEBP
    # Removing the original's image leaves the copy's.
    client.delete(f"/events/{event['id']}/image", headers=org)
    got = client.get(_path(copy["image_url"]), headers=org)
    assert got.status_code == 200
    assert got.headers["content-type"] == images.WEBP


def test_duplicate_without_image_has_none(
    client: TestClient,
    org: Headers,  # noqa: F811
    venue: Venue,  # noqa: F811
) -> None:
    event = _create(client, org, venue)
    copy = client.post(f"/events/{event['id']}/duplicate", headers=org).json()
    assert copy["image_url"] is None
