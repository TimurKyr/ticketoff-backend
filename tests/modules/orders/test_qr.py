import uuid

import jwt

from app.core.config import get_settings
from app.modules.orders.qr import TicketClaims, issue_ticket_token, verify_ticket_token

# 32+ bytes, so pyjwt does not warn about a short HMAC key.
OTHER_SECRET = "another-secret-that-is-long-enough-for-hs256"


def _encode(payload: dict[str, str], secret: str | None = None) -> str:
    return jwt.encode(payload, secret or get_settings().ticket_signing_secret, algorithm="HS256")


def _payload(**overrides: str) -> dict[str, str]:
    return {
        "typ": "ticket",
        "tid": str(uuid.uuid4()),
        "eid": str(uuid.uuid4()),
        "jti": "abc",
    } | overrides


def test_round_trip() -> None:
    ticket_id, event_id = uuid.uuid4(), uuid.uuid4()
    token = issue_ticket_token(ticket_id, event_id)
    assert verify_ticket_token(token) == TicketClaims(ticket_id=ticket_id, event_id=event_id)


def test_tokens_for_same_ticket_are_unique() -> None:
    ticket_id, event_id = uuid.uuid4(), uuid.uuid4()
    assert issue_ticket_token(ticket_id, event_id) != issue_ticket_token(ticket_id, event_id)


def test_tampered_token_returns_none() -> None:
    header, payload, signature = issue_ticket_token(uuid.uuid4(), uuid.uuid4()).split(".")
    other_payload = _encode(_payload()).split(".")[1]
    assert verify_ticket_token(f"{header}.{other_payload}.{signature}") is None


def test_other_secret_returns_none() -> None:
    assert verify_ticket_token(_encode(_payload(), secret=OTHER_SECRET)) is None


def test_access_token_secret_returns_none() -> None:
    assert verify_ticket_token(_encode(_payload(), secret=get_settings().jwt_secret)) is None


def test_wrong_typ_returns_none() -> None:
    assert verify_ticket_token(_encode(_payload(typ="campaign"))) is None


def test_missing_claim_returns_none() -> None:
    payload = _payload()
    del payload["tid"]
    assert verify_ticket_token(_encode(payload)) is None


def test_malformed_ids_return_none() -> None:
    assert verify_ticket_token(_encode(_payload(tid="not-a-uuid"))) is None


def test_garbage_returns_none() -> None:
    assert verify_ticket_token("not-a-token") is None
