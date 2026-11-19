"""app.scripts.create_admin (BE-22) on the rolled-back test session."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import User
from app.modules.identity.security import verify_password
from app.scripts.create_admin import CreateAdminError, run
from tests.factories import create_user


def _no_password() -> str:
    raise AssertionError("must not ask for a password")


def test_refuses_existing_email_without_flag(db_session: Session) -> None:
    # Registration is public and unverified: someone may have taken the admin's email first.
    user = create_user(db_session)
    with pytest.raises(CreateAdminError, match="--promote-existing"):
        run(db_session, f"  {user.email.upper()} ", lambda: "admin-password")
    db_session.refresh(user)
    assert user.system_role == SystemRole.ATTENDEE
    assert user.password_hash == "x"
    assert (
        db_session.scalar(
            select(AuditLog).where(
                AuditLog.action_type == "user.made_platform_admin",
                AuditLog.entity_id == user.id,
            )
        )
        is None
    )


def test_promotes_existing_user_with_flag_without_touching_password(db_session: Session) -> None:
    user = create_user(db_session)
    outcome = run(db_session, f"  {user.email.upper()} ", _no_password, promote_existing=True)
    assert outcome == "promoted"
    assert user.system_role == SystemRole.PLATFORM_ADMIN
    assert user.password_hash == "x"
    assert db_session.scalar(
        select(AuditLog).where(
            AuditLog.action_type == "user.made_platform_admin", AuditLog.entity_id == user.id
        )
    )


def test_is_idempotent(db_session: Session) -> None:
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    run(db_session, email, lambda: "s3cret-pass")
    assert run(db_session, email, _no_password) == "unchanged"


def test_creates_missing_user_with_given_password(db_session: Session) -> None:
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    assert run(db_session, email, lambda: "s3cret-pass") == "created"
    user = db_session.scalar(select(User).where(User.email == email))
    assert user.system_role == SystemRole.PLATFORM_ADMIN
    assert verify_password("s3cret-pass", user.password_hash)


@pytest.mark.parametrize(("email", "password"), [("not-an-email", "long-enough"), (None, "short")])
def test_rejects_bad_input(db_session: Session, email: str | None, password: str) -> None:
    email = email or f"admin-{uuid.uuid4().hex[:8]}@example.com"
    with pytest.raises(CreateAdminError):
        run(db_session, email, lambda: password)
    assert db_session.scalar(select(User).where(User.email == email)) is None
