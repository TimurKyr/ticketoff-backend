"""Core flow end to end over HTTP (BE-23, TZ 13.4).

Organizer creates and publishes an event, turns on paid sales, makes a promo campaign and
code; a buyer applies the code, pays (simulated) and gets a ticket; staff scan it at the
door: checked_in, then already_used; the campaign QR (bare token and link) is refused.
"""

import uuid
from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.modules.events.models import Venue
from tests.markers import postgres_only

pytestmark = postgres_only

PASSWORD = "core-flow-password"
PRICE = 10000
PERCENT = 25


def _account(api: TestClient, role: str) -> tuple[str, dict[str, str]]:
    email = f"{role}-{uuid.uuid4().hex[:10]}@example.com"
    response = api.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    response = api.post("/auth/login", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return email, {"Authorization": f"Bearer {response.json()['access_token']}"}


def _ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _decode_png(content: bytes) -> str:
    image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    return text


def test_core_flow(session_api: TestClient, db_session: Session) -> None:
    api = session_api
    _, organizer = _account(api, "organizer")
    buyer_email, buyer = _account(api, "buyer")
    scanner_email, scanner = _account(api, "scanner")

    # Organizer: profile, event, paid ticket type, publish.
    _ok(api.post("/organizer/profile", json={"display_name": "Core Flow"}, headers=organizer), 201)
    venue = Venue(name=f"Core Flow Hall {uuid.uuid4().hex[:6]}", city="Алматы")
    db_session.add(venue)
    db_session.flush()
    start = datetime.now(UTC) + timedelta(days=7)
    event = _ok(
        api.post(
            "/events",
            json={
                "title": "Core flow concert",
                "venue_id": str(venue.id),
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=2)).isoformat(),
                "capacity": 100,
            },
            headers=organizer,
        ),
        201,
    )
    event_id = event["id"]
    ticket_type = _ok(
        api.post(
            f"/events/{event_id}/ticket-types",
            json={"name": "Standard", "kind": "paid", "price_kzt": PRICE, "quantity_total": 100},
            headers=organizer,
        ),
        201,
    )
    _ok(api.post(f"/events/{event_id}/publish", headers=organizer))

    # Paid sales checklist (simulated).
    for step in ("simulate-verification", "accept-terms", "pay"):
        checklist = _ok(api.post(f"/events/{event_id}/activation/{step}", headers=organizer))
    assert checklist["active"] is True

    # Promo campaign and code.
    campaign = _ok(
        api.post(
            f"/events/{event_id}/campaigns",
            json={"name": "Students", "discount_type": "percent", "discount_value": PERCENT},
            headers=organizer,
        ),
        201,
    )
    code = _ok(
        api.post(
            f"/events/{event_id}/campaigns/{campaign['id']}/codes",
            json={"code": "CORE25"},
            headers=organizer,
        ),
        201,
    )
    qr_png = api.get(
        f"/events/{event_id}/campaigns/{campaign['id']}/codes/{code['id']}/qr.png",
        headers=organizer,
    )
    assert qr_png.status_code == 200
    campaign_link = _decode_png(qr_png.content)
    assert campaign_link.endswith(f"/events/{event_id}?promo={code['qr_token']}")

    # Buyer: the event is public, the code checks out, checkout with it, simulated payment.
    listed = _ok(api.get("/events", params={"q": "Core flow concert"}))
    assert event_id in {item["id"] for item in listed["items"]}
    promo = _ok(
        api.post(f"/events/{event_id}/promo-codes/check", json={"code": "core25"}, headers=buyer)
    )
    assert promo["discount_value"] == PERCENT
    order = _ok(
        api.post(
            f"/events/{event_id}/checkout",
            json={
                "items": [{"ticket_type_id": ticket_type["id"], "quantity": 1}],
                "attendees": [{"name": "Core Buyer", "email": buyer_email}],
                "promo_code": "CORE25",
            },
            headers=buyer,
        ),
        201,
    )
    assert order["status"] == "pending"
    assert order["total_kzt"] == PRICE
    assert order["discount_kzt"] == PRICE * PERCENT // 100
    paid = _ok(api.post(f"/orders/{order['id']}/pay", json={"simulate": "success"}, headers=buyer))
    assert paid["payment"]["amount_kzt"] == PRICE - PRICE * PERCENT // 100
    assert paid["order"]["status"] == "paid"
    [ticket] = paid["order"]["tickets"]
    assert ticket["qr_token"].startswith("tk1.")
    assert _ok(api.get(f"/tickets/{ticket['id']}", headers=buyer))["status"] == "valid"

    # Door: organizer assigns staff, staff scan.
    _ok(
        api.post(f"/events/{event_id}/staff", json={"email": scanner_email}, headers=organizer), 201
    )

    def scan(qr: str) -> str:
        body = {"event_id": event_id, "qr_token": qr}
        return _ok(api.post("/checkin/scan", json=body, headers=scanner))["result"]

    assert scan(ticket["qr_token"]) == "checked_in"
    assert scan(ticket["qr_token"]) == "already_used"
    assert scan(code["qr_token"]) == "campaign_qr_rejected"
    assert scan(campaign_link) == "campaign_qr_rejected"

    stats = _ok(api.get(f"/checkin/events/{event_id}/stats", headers=scanner))
    assert stats["checked_in"] == 1


def test_core_flow_support_case() -> None:
    pytest.skip("TODO(BE-17): buyer opens a support case about the order; add when BE-17 lands")
