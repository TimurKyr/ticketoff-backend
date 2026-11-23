"""BE-25: password reset (forgot-password email, reset-password with a one-time link)."""

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
    PASSWORD_RESET_TTL,
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
)
from app.modules.ops.models import Notification
from tests.emails import RecordingEmailProvider
from tests.markers import postgres_only

pytestmark = postgres_only

INVALID = "Invalid or expired password reset link"
OLD = "correct-horse-battery"
NEW = "brand-new-password-42"
RESET_SUBJECTS = {"Сброс пароля в ticketoff", "Reset your ticketoff password"}


@pytest.fixture
def api(client: TestClient, db_session: Session) -> TestClient:
    app.dependency_overrides[get_session] = lambda: db_session
    return client


def new_account(api: TestClient, language: str = "en") -> str:
    address = f"user-{uuid.uuid4().hex[:12]}@example.com"
    r = api.post("/auth/register", json={"email": address, "password": OLD, "language": language})
    assert r.status_code == 201
    return address


def forgot(api: TestClient, address: str):
    return api.post("/auth/forgot-password", json={"email": address})


def reset(api: TestClient, token: str, password: str = NEW):
    return api.post("/auth/reset-password", json={"token": token, "new_password": password})


def login(api: TestClient, address: str, password: str):
    return api.post("/auth/login", data={"username": address, "password": password})


def reset_emails(emails: RecordingEmailProvider, address: str) -> list:
    return [e for e in emails.to(address) if e.subject in RESET_SUBJECTS]


def token_from(email) -> str:
    [link] = [w for w in email.text.split() if "/reset-password?" in w]
    url = urlparse(link)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://ticketoff.store/reset-password"
    [token] = parse_qs(url.query)["token"]
    return token


def user_of(db_session: Session, address: str) -> User:
    return db_session.scalars(select(User).where(User.email == address)).one()


def signed(claims: dict) -> str:
    s = get_settings()
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


# --- asking for the link ---


@pytest.mark.parametrize(
    ("language", "subject", "valid_for"),
    [
        ("ru", "Сброс пароля в ticketoff", "Ссылка действует 1 ч"),
        ("en", "Reset your ticketoff password", "valid for 1 h"),
    ],
)
def test_forgot_password_sends_one_email_in_the_users_language(
    db_session: Session,
    api: TestClient,
    emails: RecordingEmailProvider,
    language: str,
    subject: str,
    valid_for: str,
) -> None:
    address = new_account(api, language)

    response = forgot(api, address)

    assert response.status_code == 204
    [email] = reset_emails(emails, address)
    assert email.subject == subject and valid_for in email.text
    assert email.contains_secret is True and email.attachments == ()
    assert f'href="https://ticketoff.store/reset-password?token={token_from(email)}"' in email.html
    # Email only: no in-app row.
    user = user_of(db_session, address)
    assert (
        db_session.scalars(select(Notification).where(Notification.user_id == user.id)).all() == []
    )
    assert user.password_reset_sent_at is not None


def test_the_address_is_matched_ignoring_case_and_spaces(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)

    assert forgot(api, f"  {address.upper()} ".strip()).status_code == 204

    assert len(reset_emails(emails, address)) == 1


def test_unknown_address_gets_the_same_answer_and_no_email(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    stranger = f"nobody-{uuid.uuid4().hex[:8]}@example.com"

    response = forgot(api, stranger)

    assert response.status_code == 204 and response.content == b""
    assert emails.to(stranger) == []


def test_bad_email_format_is_422(api: TestClient) -> None:
    assert forgot(api, "not-an-email").status_code == 422


def test_second_request_within_the_cooldown_sends_nothing(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)

    again = forgot(api, address)

    assert again.status_code == 204  # still the same answer: no hint
    assert len(reset_emails(emails, address)) == 1


def test_after_the_cooldown_a_new_email_is_sent(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)
    user_of(db_session, address).password_reset_sent_at = datetime.now(UTC) - timedelta(minutes=2)
    db_session.flush()

    forgot(api, address)

    assert len(reset_emails(emails, address)) == 2


def test_suspended_account_gets_no_email(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    user_of(db_session, address).is_suspended = True
    db_session.flush()

    assert forgot(api, address).status_code == 204

    assert reset_emails(emails, address) == []


def test_the_link_is_never_logged(
    api: TestClient, emails: RecordingEmailProvider, caplog: pytest.LogCaptureFixture
) -> None:
    address = new_account(api)

    with caplog.at_level(logging.DEBUG):
        forgot(api, address)

    assert "reset-password" not in caplog.text and "token=" not in caplog.text


# --- using the link ---


def test_the_emailed_link_sets_a_new_password(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)

    response = reset(api, token_from(reset_emails(emails, address)[0]))

    assert response.status_code == 204
    assert login(api, address, NEW).status_code == 200
    assert login(api, address, OLD).status_code == 401


def test_a_link_works_only_once(api: TestClient, emails: RecordingEmailProvider) -> None:
    address = new_account(api)
    forgot(api, address)
    token = token_from(reset_emails(emails, address)[0])
    assert reset(api, token).status_code == 204

    second = reset(api, token, "another-password-77")

    assert second.status_code == 422 and second.json()["detail"] == INVALID
    assert login(api, address, NEW).status_code == 200


def test_a_newer_link_does_not_outlive_a_password_change(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)
    user_of(db_session, address).password_reset_sent_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.flush()
    forgot(api, address)
    first, second = (token_from(e) for e in reset_emails(emails, address))

    assert reset(api, first).status_code == 204

    assert reset(api, second, "yet-another-pass-9").status_code == 422


def test_reset_does_not_verify_the_email(api: TestClient, emails: RecordingEmailProvider) -> None:
    address = new_account(api)
    forgot(api, address)
    reset(api, token_from(reset_emails(emails, address)[0]))

    token = login(api, address, NEW).json()["access_token"]
    me = api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.json()["email_verified"] is False


def test_weak_password_is_422_and_keeps_the_link_usable(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)
    token = token_from(reset_emails(emails, address)[0])

    assert reset(api, token, "short").status_code == 422
    assert reset(api, token, "x" * 129).status_code == 422

    assert reset(api, token).status_code == 204


def test_expired_link_is_refused(db_session: Session, api: TestClient) -> None:
    address = new_account(api)
    user = user_of(db_session, address)
    expired = signed(
        {
            "sub": str(user.id),
            "pwd": "0" * 16,
            "typ": "password_reset",
            "exp": datetime.now(UTC) - timedelta(seconds=1),
        }
    )

    response = reset(api, expired)

    assert response.status_code == 422 and response.json()["detail"] == INVALID


def test_the_link_lasts_one_hour() -> None:
    assert PASSWORD_RESET_TTL == timedelta(hours=1)
    token = create_password_reset_token(uuid.uuid4(), "hash")
    exp = jwt.decode(token, options={"verify_signature": False})["exp"]
    left = datetime.fromtimestamp(exp, UTC) - datetime.now(UTC)
    assert timedelta(minutes=59) < left <= timedelta(hours=1)


def test_other_kinds_of_token_are_refused(db_session: Session, api: TestClient) -> None:
    address = new_account(api)
    user = user_of(db_session, address)
    wrong = [
        create_access_token(user.id),
        create_email_verification_token(user.id, address),
        "garbage",
        # right type, but no password fingerprint
        signed(
            {
                "sub": str(user.id),
                "typ": "password_reset",
                "exp": datetime.now(UTC) + PASSWORD_RESET_TTL,
            }
        ),
    ]

    for token in wrong:
        response = reset(api, token)
        assert response.status_code == 422 and response.json()["detail"] == INVALID

    assert login(api, address, OLD).status_code == 200


def test_a_tampered_link_is_refused(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)
    token = token_from(reset_emails(emails, address)[0])

    assert reset(api, token[:-3] + "abc").status_code == 422
    assert login(api, address, OLD).status_code == 200


def test_a_reset_link_is_not_a_login_or_a_verification_link(
    api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)
    token = token_from(reset_emails(emails, address)[0])

    assert api.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert api.post("/auth/verify-email", json={"token": token}).status_code == 422


def test_unknown_user_in_a_valid_token_is_refused(api: TestClient) -> None:
    token = create_password_reset_token(uuid.uuid4(), "whatever")

    response = reset(api, token)

    assert response.status_code == 422 and response.json()["detail"] == INVALID


def test_suspended_account_cannot_reset(
    db_session: Session, api: TestClient, emails: RecordingEmailProvider
) -> None:
    address = new_account(api)
    forgot(api, address)
    token = token_from(reset_emails(emails, address)[0])
    user_of(db_session, address).is_suspended = True
    db_session.flush()

    response = reset(api, token)

    assert response.status_code == 422 and response.json()["detail"] == INVALID
