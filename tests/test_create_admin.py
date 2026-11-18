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


def test_promotes_existing_user_without_touching_password(db_session: Session) -> None:
    user = create_user(db_session)
    assert run(db_session, f"  {user.email.upper()} ", _no_password) == "promoted"
    assert user.system_role == SystemRole.PLATFORM_ADMIN
    assert user.password_hash == "x"
    assert db_session.scalar(
        select(AuditLog).where(
            AuditLog.action_type == "user.made_platform_admin", AuditLog.entity_id == user.id
        )
    )


def test_is_idempotent(db_session: Session) -> None:
    user = create_user(db_session)
    run(db_session, user.email, _no_password)
    assert run(db_session, user.email, _no_password) == "unchanged"


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
