"""Links in emails to pages of the web app (ticketoff-web's router).

Every web path an email uses is here, so a route change is a one-line edit. Links are built
on Settings.frontend_url; the one API link (the .ics file) on Settings.public_api_url.
"""

import uuid
from urllib.parse import urlencode

from app.core.config import get_settings

# The public event page (also what a campaign QR and a calendar entry link to).
EVENT_PATH = "/events/{event_id}"
# One order: confirmation, failed payment, refund, cancelled event.
ORDER_PATH = "/orders/{order_id}"
# All of the buyer's tickets: the order email links here when there are too many to attach.
TICKETS_PATH = "/tickets"
# Support emails: the case thread (ticketoff-web FE-8).
SUPPORT_CASE_PATH = "/support/cases/{case_id}"
# Organizer pages (BE-28 payout notices): the dashboard, and one event's management page.
ORGANIZER_PATH = "/organizer"
ORGANIZER_EVENT_PATH = "/organizer/events/{event_id}"
# Email verification: the web app reads ?token= and calls POST /auth/verify-email.
VERIFY_EMAIL_PATH = "/verify-email"
# Password reset: the web app reads ?token=, asks for a new password and calls
# POST /auth/reset-password.
RESET_PASSWORD_PATH = "/reset-password"


def web_url(path: str) -> str:
    return get_settings().frontend_url.rstrip("/") + path


def event_url(event_id: uuid.UUID) -> str:
    return web_url(EVENT_PATH.format(event_id=event_id))


def calendar_ics_url(event_id: uuid.UUID) -> str:
    """The event's .ics file (GET /events/{id}/calendar.ics), on the API, not the web app."""
    return get_settings().public_api_url.rstrip("/") + f"/events/{event_id}/calendar.ics"


def order_url(order_id: uuid.UUID) -> str:
    return web_url(ORDER_PATH.format(order_id=order_id))


def tickets_url() -> str:
    return web_url(TICKETS_PATH)


def organizer_url() -> str:
    return web_url(ORGANIZER_PATH)


def organizer_event_url(event_id: uuid.UUID) -> str:
    return web_url(ORGANIZER_EVENT_PATH.format(event_id=event_id))


def support_url(case_id: uuid.UUID) -> str:
    return web_url(SUPPORT_CASE_PATH.format(case_id=case_id))


def verify_email_url(token: str) -> str:
    return web_url(VERIFY_EMAIL_PATH) + "?" + urlencode({"token": token})


def password_reset_url(token: str) -> str:
    return web_url(RESET_PASSWORD_PATH) + "?" + urlencode({"token": token})
