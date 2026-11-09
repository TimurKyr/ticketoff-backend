import base64
import hashlib
import hmac
import uuid

import pytest

from app.core.config import get_settings
from app.modules.orders.qr import TICKET_PREFIX, issue_ticket_token, verify_ticket_token

# 32+ bytes, like a real secret.
OTHER_SECRET = "another-secret-that-is-long-enough-for-hs256"


def _sign(message: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_format_matches_spec() -> None:
    """tk1.<uuid>.<base64url(HMAC-SHA256(secret, "tk1.<uuid>")) without padding>."""
    ticket_id = uuid.uuid4()
    token = issue_ticket_token(ticket_id)

    message = f"tk1.{ticket_id}"
    assert token == f"{message}.{_sign(message, get_settings().ticket_signing_secret)}"
    assert TICKET_PREFIX == "tk1."
    assert "=" not in token
    assert len(token.rpartition(".")[2]) == 43  # 32 bytes -> 43 base64url chars


def test_round_trip() -> None:
    ticket_id = uuid.uuid4()
    assert verify_ticket_token(issue_ticket_token(ticket_id)) == ticket_id


def test_tokens_of_different_tickets_differ() -> None:
    assert issue_ticket_token(uuid.uuid4()) != issue_ticket_token(uuid.uuid4())


def test_signature_from_other_ticket_returns_none() -> None:
    signature = issue_ticket_token(uuid.uuid4()).rpartition(".")[2]
    assert verify_ticket_token(f"tk1.{uuid.uuid4()}.{signature}") is None


def test_tampered_signature_returns_none() -> None:
    token = issue_ticket_token(uuid.uuid4())
    last = token[-1]
    assert verify_ticket_token(token[:-1] + ("A" if last != "A" else "B")) is None


@pytest.mark.parametrize("secret", [OTHER_SECRET, get_settings().jwt_secret])
def test_other_secret_returns_none(secret: str) -> None:
    message = f"tk1.{uuid.uuid4()}"
    assert verify_ticket_token(f"{message}.{_sign(message, secret)}") is None


def test_wrong_prefix_returns_none() -> None:
    """A campaign code signed the same way must not pass as a ticket."""
    ticket_id = uuid.uuid4()
    message = f"pc1.{ticket_id}"
    token = f"{message}.{_sign(message, get_settings().ticket_signing_secret)}"
    assert verify_ticket_token(token) is None


@pytest.mark.parametrize(
    "build",
    [
        lambda tid: f"tk1.{tid}",  # no signature
        lambda tid: f"tk1.{tid}.",  # empty signature
        lambda tid: "tk1.",
        lambda tid: "tk1.not-a-uuid.c2ln",
        lambda tid: f"tk1.{str(tid).upper()}.c2ln",  # non-canonical uuid
        lambda tid: f"tk1.{tid}.{'é' * 43}",  # non-ASCII signature
        lambda tid: "not-a-token",
        lambda tid: "",
    ],
)
def test_malformed_tokens_return_none(build) -> None:
    assert verify_ticket_token(build(uuid.uuid4())) is None


def test_non_canonical_uuid_with_valid_signature_returns_none() -> None:
    """Only the canonical spelling is a ticket token, even if someone signs another one."""
    message = f"tk1.{str(uuid.uuid4()).upper()}"
    token = f"{message}.{_sign(message, get_settings().ticket_signing_secret)}"
    assert verify_ticket_token(token) is None
