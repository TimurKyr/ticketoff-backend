"""Stage 5 of BE-18: email verification (token, registration email, verify, resend)."""

import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.modules.identity.models import User
from app.modules.identity.security import (
    EMAIL_VERIFICATION_TTL,
    create_access_token,
    create_email_verification_token,
    decode_access_token,
    decode_email_verification_token,
)
from app.modules.notifications import service as notifications
from app.modules.notifications.providers import ConsoleEmailProvider, EmailMessage
from app.modules.ops.models import Notification
from app.modules.orders.qr import issue_ticket_token
from app.modules.promotions.service import CAMPAIGN_PREFIX
from tests.emails import RecordingEmailProvider
from tests.factories import create_user
from tests.markers import postgres_only

pytestmark = postgres_only

INVALID = "Invalid or expired verification link"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def new_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def register(api: TestClient, email: str, language: str | None = None):
    body = {"email": email, "password": PASSWORD}
    if language:
        body["language"] = language
    return api.post("/auth/register", json=body)


def token_from(email: EmailMessage) -> str:
    """The token in the email's link (the only place it exists)."""
    [link] = [w for w in email.text.split() if "/verify-email?" in w]
    url = urlparse(link)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://ticketoff.store/verify-email"
    [token] = parse_qs(url.query)["token"]
    return token


def verify(api: TestClient, token: str):
    return api.post("/auth/verify-email", json={"token": token})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signed(claims: dict) -> str:
    """A JWT signed with our real secret: only the claims are wrong."""
    s = get_settings()
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


def in_a_day() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


# --- registration sends the link ---


@pytest.mark.parametrize(
    ("language", "subject", "valid_for"),
    [
        (None, "Подтвердите email в ticketoff", "Ссылка действует 24 ч."),
        ("ru", "Подтвердите email в ticketoff", "Ссылка действует 24 ч."),
        ("en", "Confirm your email for ticketoff", "The link is valid for 24 h."),
    ],
)
def test_registration_queues_one_verification_email(
    db_session: Session,
    api: TestClient,
    emails: RecordingEmailProvider,
    language: str | None,
    subject: str,
    valid_for: str,
) -> None:
    address = new_email()

    response = register(api, address, language)

    assert response.status_code == 201 and response.json()["email_verified"] is False
    [email] = emails.sent
    assert email.to == address and email.subject == subject
    assert valid_for in email.text
    assert email.contains_secret is True and email.attachments == ()
    token = token_from(email)
    assert f'href="https://ticketoff.store/verify-email?token={token}"' in email.html
    # Email only: no in-app row for verification.
    user_id = uuid.UUID(response.json()["id"])
    assert (
        db_session.scalars(select(Notification).where(Notification.user_id == user_id)).all() == []
    )


def test_the_emailed_link_verifies_the_account(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_email()
    register(api, address)

    response = verify(api, token_from(emails.sent[0]))

    assert response.status_code == 200
    assert response.json()["email"] == address and response.json()["email_verified"] is True
    login = api.post("/auth/login", data={"username": address, "password": PASSWORD})
    me = api.get("/auth/me", headers=bearer(login.json()["access_token"]))
    assert me.json()["email_verified"] is True


def test_duplicate_registration_sends_nothing(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_email()
    register(api, address)
    emails.sent.clear()

    assert register(api, address.upper()).status_code == 409
    assert emails.sent == []


def test_registration_succeeds_when_the_mail_provider_fails(
    db_session: Session, api: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    class Down:
        def send(self, message: EmailMessage) -> None:
            raise ConnectionRefusedError("smtp down")

    notifications.set_provider(Down())
    address = new_email()

    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        response = register(api, address)

    assert response.status_code == 201
    assert db_session.scalar(select(User).where(User.email == address)) is not None
    assert "notification email failed" in caplog.text
    assert "verify-email" not in caplog.text  # the failure log never has the link


def test_the_token_is_never_logged(
    api: TestClient, emails: RecordingEmailProvider, caplog: pytest.LogCaptureFixture
) -> None:
    # The console provider (local default) logs emails; for this one it leaves the body out.
    notifications.set_provider(ConsoleEmailProvider())
    with caplog.at_level(logging.DEBUG):
        register(api, new_email())

    assert "email (console, not sent)" in caplog.text
    assert "verify-email" not in caplog.text and "token=" not in caplog.text
    assert "body not logged" in caplog.text


# --- POST /auth/verify-email ---


def test_verifying_twice_gives_the_same_answer(db_session: Session, api: TestClient) -> None:
    user = create_user(db_session)
    token = create_email_verification_token(user.id, user.email)

    first, second = verify(api, token), verify(api, token)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and first.json()["email_verified"] is True
    db_session.refresh(user)
    assert user.email_verified is True


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def _b64(claims: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()


def bad_tokens(user: User) -> dict[str, str]:
    good = create_email_verification_token(user.id, user.email)
    head, body, signature = good.split(".")
    stranger = uuid.uuid4()
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    return {
        "expired": create_email_verification_token(
            user.id, user.email, now=datetime.now(UTC) - EMAIL_VERIFICATION_TTL - timedelta(1)
        ),
        "tampered signature": f"{head}.{body}.{flipped}",
        # Someone else's id in the claims, the original signature kept.
        "tampered claims": f"{head}.{_b64({**_claims(good), 'sub': str(stranger)})}.{signature}",
        "other secret": jwt.encode(
            {
                "sub": str(user.id),
                "email": user.email,
                "typ": "email_verification",
                "exp": in_a_day(),
            },
            "not-our-secret-not-our-secret-not-our-secret",
            algorithm="HS256",
        ),  # fmt: skip
        "wrong typ": signed(
            {"sub": str(user.id), "email": user.email, "typ": "password_reset", "exp": in_a_day()}
        ),
        "no typ": signed({"sub": str(user.id), "email": user.email, "exp": in_a_day()}),
        "no exp": signed({"sub": str(user.id), "email": user.email, "typ": "email_verification"}),
        "no email": signed({"sub": str(user.id), "typ": "email_verification", "exp": in_a_day()}),
        "bad sub": signed(
            {
                "sub": "not-a-uuid",
                "email": user.email,
                "typ": "email_verification",
                "exp": in_a_day(),
            }
        ),  # fmt: skip
        "unknown user": create_email_verification_token(uuid.uuid4(), user.email),
        "access token": create_access_token(user.id),
        "ticket token": issue_ticket_token(uuid.uuid4()),
        "promo qr token": f"{CAMPAIGN_PREFIX}{'x' * 32}",
        "garbage": "not-a-token",
    }


@pytest.mark.parametrize(
    "kind",
    [
        "expired",
        "tampered signature",
        "tampered claims",
        "other secret",
        "wrong typ",
        "no typ",
        "no exp",
        "no email",
        "bad sub",
        "unknown user",
        "access token",
        "ticket token",
        "promo qr token",
        "garbage",
    ],
)
def test_every_bad_token_gets_the_same_error(
    db_session: Session, api: TestClient, kind: str
) -> None:
    user = create_user(db_session)

    response = verify(api, bad_tokens(user)[kind])

    assert response.status_code == 422
    assert response.json() == {"detail": INVALID}
    db_session.refresh(user)
    assert user.email_verified is False


def test_a_link_for_an_old_email_is_void(db_session: Session, api: TestClient) -> None:
    user = create_user(db_session)
    token = create_email_verification_token(user.id, user.email)
    user.email = new_email()  # the address changed after the link was sent
    db_session.flush()

    response = verify(api, token)

    assert (response.status_code, response.json()) == (422, {"detail": INVALID})
    db_session.refresh(user)
    assert user.email_verified is False


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": "x" * 2049}])
def test_missing_or_oversized_token_is_422(api: TestClient, body: dict) -> None:
    assert api.post("/auth/verify-email", json=body).status_code == 422


# --- the two token kinds never cross ---


def test_a_verification_token_is_not_a_login(db_session: Session, api: TestClient) -> None:
    user = create_user(db_session)
    token = create_email_verification_token(user.id, user.email)

    assert decode_access_token(token) is None
    assert api.get("/auth/me", headers=bearer(token)).status_code == 401
    assert api.get("/notifications", headers=bearer(token)).status_code == 401
    assert api.post("/auth/verify-email/resend", headers=bearer(token)).status_code == 401


def test_an_access_token_is_not_a_verification_link(db_session: Session) -> None:
    user = create_user(db_session)
    access = create_access_token(user.id)

    assert decode_email_verification_token(access) is None
    assert decode_access_token(access) == user.id  # and it still logs in


def test_a_jwt_without_typ_is_not_a_login(db_session: Session, api: TestClient) -> None:
    # Before BE-18 access tokens had no typ: any JWT with our secret and a sub logged in.
    user = create_user(db_session)
    untyped = signed({"sub": str(user.id), "exp": in_a_day()})

    assert decode_access_token(untyped) is None
    assert api.get("/auth/me", headers=bearer(untyped)).status_code == 401


# --- POST /auth/verify-email/resend ---


def test_resend_sends_a_fresh_working_link(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)

    response = api.post("/auth/verify-email/resend", headers=bearer(create_access_token(user.id)))

    assert response.status_code == 204
    [email] = emails.sent
    assert email.to == user.email
    assert verify(api, token_from(email)).json()["email_verified"] is True


def test_resend_when_already_verified_is_409(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)
    user.email_verified = True
    db_session.flush()

    response = api.post("/auth/verify-email/resend", headers=bearer(create_access_token(user.id)))

    assert (response.status_code, response.json()) == (409, {"detail": "Email is already verified"})
    assert emails.sent == []


def test_resend_needs_a_login(api: TestClient) -> None:
    assert api.post("/auth/verify-email/resend").status_code == 401


def test_unverified_users_can_still_log_in(api: TestClient) -> None:
    address = new_email()
    register(api, address)

    login = api.post("/auth/login", data={"username": address, "password": PASSWORD})

    assert login.status_code == 200
    me = api.get("/auth/me", headers=bearer(login.json()["access_token"]))
    assert me.status_code == 200 and me.json()["email_verified"] is False


# --- resend cooldown (60 s per user; registration counts as a send) ---


def resend(api: TestClient, user: User):
    return api.post("/auth/verify-email/resend", headers=bearer(create_access_token(user.id)))


def test_resending_twice_quickly_is_429_and_queues_nothing(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)

    assert resend(api, user).status_code == 204
    second = resend(api, user)

    assert second.status_code == 429
    assert second.json() == {"detail": "A verification email was sent recently; try again later"}
    assert 1 <= int(second.headers["Retry-After"]) <= 60
    assert len(emails.sent) == 1  # the second request queued nothing
    assert notifications.queued(db_session) == []


def test_retry_after_counts_down_from_the_last_send(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)
    user.email_verification_sent_at = datetime.now(UTC) - timedelta(seconds=45)
    db_session.flush()

    response = resend(api, user)

    assert response.status_code == 429
    assert 14 <= int(response.headers["Retry-After"]) <= 15
    assert emails.sent == []


def test_resend_works_again_after_the_cooldown(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    user = create_user(db_session)
    assert resend(api, user).status_code == 204
    # Move the last send back past the cooldown instead of sleeping 60 s.
    db_session.refresh(user)
    first_sent = user.email_verification_sent_at
    user.email_verification_sent_at = first_sent - timedelta(seconds=61)
    db_session.flush()

    response = resend(api, user)

    assert response.status_code == 204 and len(emails.sent) == 2
    db_session.refresh(user)
    assert user.email_verification_sent_at > first_sent - timedelta(seconds=61)
    assert verify(api, token_from(emails.sent[1])).status_code == 200


def test_registration_counts_as_a_send(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_email()
    register(api, address)
    user = db_session.scalar(select(User).where(User.email == address))
    assert user.email_verification_sent_at is not None

    response = resend(api, user)

    assert response.status_code == 429 and "Retry-After" in response.headers
    assert len(emails.sent) == 1  # only the registration email


def test_already_verified_wins_over_the_cooldown(db_session: Session, api: TestClient) -> None:
    user = create_user(db_session)
    user.email_verified = True
    user.email_verification_sent_at = datetime.now(UTC)
    db_session.flush()

    assert resend(api, user).status_code == 409


def test_rate_limited_error_maps_to_429_with_retry_after() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as Client

    from app.common.errors import RateLimitedError
    from app.core.error_handlers import add_error_handlers

    small = FastAPI()
    add_error_handlers(small)

    @small.get("/limited")
    def limited() -> None:
        raise RateLimitedError("slow down", retry_after=0)  # never below 1 s

    response = Client(small).get("/limited")
    assert (response.status_code, response.headers["Retry-After"]) == (429, "1")
    assert response.json() == {"detail": "slow down"}
