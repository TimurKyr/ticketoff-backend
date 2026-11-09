"""Signed QR tokens for tickets.

Format: tk1.<ticket_uuid>.<signature>, where the signature is
base64url(HMAC-SHA256(TICKET_SIGNING_SECRET, "tk1.<ticket_uuid>")) without "=" padding.

The token has no expiry: a ticket stays valid until it is used or cancelled, and the
ticket's DB status is the source of truth. Check-in calls verify_ticket_token() first, so
forged tokens are rejected without a DB lookup, and then looks up the stored qr_token.
The prefix keeps other QR codes (e.g. campaign codes, "pc1.") from passing as tickets.
"""

import base64
import hashlib
import hmac
import uuid

from app.core.config import get_settings

TICKET_PREFIX = "tk1."


def _signature(message: str) -> str:
    digest = hmac.new(
        get_settings().ticket_signing_secret.encode(), message.encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def issue_ticket_token(ticket_id: uuid.UUID) -> str:
    message = f"{TICKET_PREFIX}{ticket_id}"
    return f"{message}.{_signature(message)}"


def verify_ticket_token(token: str) -> uuid.UUID | None:
    """Return the ticket id of a genuine ticket token, or None for anything else."""
    if not token.startswith(TICKET_PREFIX):
        return None
    message, _, signature = token.rpartition(".")
    raw_id = message.removeprefix(TICKET_PREFIX)
    try:
        ticket_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    # Only the canonical spelling is signed; "{...}", upper case etc. are not ticket tokens.
    if raw_id != str(ticket_id):
        return None
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str (scanned input).
    if not hmac.compare_digest(signature.encode(), _signature(message).encode()):
        return None
    return ticket_id
