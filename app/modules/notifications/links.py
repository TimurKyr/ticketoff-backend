"""Links in emails to pages of the web app (ticketoff-web's router).

Every web path an email uses is here, so a route change is a one-line edit. Links are built
on Settings.frontend_url.
"""

import uuid
from urllib.parse import urlencode

from app.core.config import get_settings

# One order: confirmation, failed payment, refund, cancelled event.
ORDER_PATH = "/orders/{order_id}"
# All of the buyer's tickets: the order email links here when there are too many to attach.
TICKETS_PATH = "/tickets"
# Support emails. The web app has no support page yet; switch to the case page when it does.
SUPPORT_PATH = "/account"
# Email verification: the web app reads ?token= and calls POST /auth/verify-email.
VERIFY_EMAIL_PATH = "/verify-email"


def web_url(path: str) -> str:
    return get_settings().frontend_url.rstrip("/") + path


def order_url(order_id: uuid.UUID) -> str:
    return web_url(ORDER_PATH.format(order_id=order_id))


def tickets_url() -> str:
    return web_url(TICKETS_PATH)


def support_url() -> str:
    return web_url(SUPPORT_PATH)


def verify_email_url(token: str) -> str:
    return web_url(VERIFY_EMAIL_PATH) + "?" + urlencode({"token": token})
