"""Signed QR tokens for tickets.

A token is a JWT signed with TICKET_SIGNING_SECRET. It has no exp: a ticket stays valid
until it is used or cancelled, and the ticket's DB status is the source of truth.
Check-in should call verify_ticket_token(), load the ticket by ticket_id and compare
its stored qr_token with the scanned one.
"""

import secrets
import uuid
from dataclasses import dataclass

import jwt

from app.core.config import get_settings

TICKET_TOKEN_TYPE = "ticket"
_ALGORITHM = "HS256"


@dataclass(frozen=True)
class TicketClaims:
    ticket_id: uuid.UUID
    event_id: uuid.UUID


def issue_ticket_token(ticket_id: uuid.UUID, event_id: uuid.UUID) -> str:
    # The random jti makes every token unique, even for the same ticket.
    payload = {
        "typ": TICKET_TOKEN_TYPE,
        "tid": str(ticket_id),
        "eid": str(event_id),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, get_settings().ticket_signing_secret, algorithm=_ALGORITHM)


def verify_ticket_token(token: str) -> TicketClaims | None:
    """Return the claims of a valid ticket token, or None if it is bad or not a ticket token.

    The typ check stops other QR codes (e.g. campaign codes) from passing as tickets.
    """
    try:
        payload = jwt.decode(
            token,
            get_settings().ticket_signing_secret,
            algorithms=[_ALGORITHM],
            options={"require": ["typ", "tid", "eid", "jti"]},
        )
        if payload["typ"] != TICKET_TOKEN_TYPE:
            return None
        return TicketClaims(
            ticket_id=uuid.UUID(str(payload["tid"])),
            event_id=uuid.UUID(str(payload["eid"])),
        )
    except (jwt.InvalidTokenError, ValueError):
        return None
