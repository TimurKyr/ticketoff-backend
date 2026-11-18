"""Make a user a platform admin (BE-22).

    python -m app.scripts.create_admin <email>

On the server:

    docker compose -f docker-compose.prod.yml exec api python -m app.scripts.create_admin <email>

An existing account (any role) is promoted; its password is not touched. A missing account
is created: the password comes from ADMIN_PASSWORD or, without it, from a hidden prompt.
The password is never printed. Idempotent: running it again on an admin changes nothing.
"""

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

import app.modules.registry  # noqa: F401  (all models, for relationship resolution)
from app.modules.audit import service as audit
from app.modules.identity.enums import SystemRole
from app.modules.identity.models import User
from app.modules.identity.security import hash_password
from app.modules.identity.service import normalize_email

MIN_PASSWORD_LENGTH = 8  # same as UserRegister


class CreateAdminError(Exception):
    pass


def _prompt_password() -> str:
    if not sys.stdin.isatty():
        raise CreateAdminError("set ADMIN_PASSWORD (no terminal to prompt for a password)")
    password = getpass.getpass("Password for the new admin: ")
    if getpass.getpass("Repeat password: ") != password:
        raise CreateAdminError("passwords do not match")
    return password


def run(session: Session, email: str, get_password: Callable[[], str]) -> str:
    """Promote or create the user. Returns "created", "promoted" or "unchanged"."""
    email = normalize_email(email)
    if "@" not in email:
        raise CreateAdminError(f"not an email: {email!r}")
    user = session.scalar(select(User).where(User.email == email))
    if user is not None and user.system_role == SystemRole.PLATFORM_ADMIN:
        return "unchanged"
    if user is None:
        password = get_password()
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CreateAdminError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        user = User(email=email, password_hash=hash_password(password))
        session.add(user)
        outcome = "created"
    else:
        outcome = "promoted"
    user.system_role = SystemRole.PLATFORM_ADMIN
    session.flush()
    audit.log(
        session,
        actor_user_id=None,
        action_type="user.made_platform_admin",
        entity_type="user",
        entity_id=user.id,
        event_id=None,
        details={"description": f"create_admin script ({outcome})"},
    )
    session.commit()
    return outcome


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make a user a platform admin.")
    parser.add_argument("email")
    args = parser.parse_args(argv)

    from app.core.db import SessionLocal

    def get_password() -> str:
        return os.environ.get("ADMIN_PASSWORD") or _prompt_password()

    with SessionLocal() as session:
        try:
            outcome = run(session, args.email, get_password)
        except CreateAdminError as exc:
            print(f"create_admin failed: {exc}", file=sys.stderr)
            return 1
    messages = {
        "created": "created a new platform admin",
        "promoted": "existing user is now a platform admin",
        "unchanged": "already a platform admin, nothing changed",
    }
    print(f"{normalize_email(args.email)}: {messages[outcome]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
