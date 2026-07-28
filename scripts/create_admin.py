"""Create (or promote) an ADMIN user so you can test the admin dashboard.

The admin role is stored on ``users.role`` (never in the JWT) and every
``/admin/*`` route is guarded by ``require_role(ADMIN)``, so all you need to
exercise the dashboard is an account with that role. This script mints one
directly against whatever DB the API is pointed at (``AsyncSessionLocal`` reads
the same settings as the app), with a hashed password, ``is_active=True`` and
``is_verified=True`` so it can log in immediately.

    # create a brand-new admin (prompts for the password if --password is omitted):
    python -m scripts.create_admin --email admin@example.com --password Admin123! --name Admin

    # promote an account you already registered through the app to ADMIN:
    python -m scripts.create_admin --email you@example.com --promote

Idempotent: re-running for an existing email promotes them to ADMIN (and resets
the password / re-verifies when those flags are given) rather than erroring.
Intended for local/dev — it writes straight to the DB with no auth.
"""

import argparse
import asyncio
import getpass

from pydantic import TypeAdapter, ValidationError
from pydantic.networks import EmailStr

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.repositories.user_repository import user_repository

# The API serializes every user through UserResponse.email: EmailStr, so an email
# that model can't validate (e.g. a reserved ".local" domain) writes fine here but
# 500s on the first GET /users/me. Reject it up front instead.
_email_adapter = TypeAdapter(EmailStr)


def _validate_email(email: str) -> str | None:
    try:
        return _email_adapter.validate_python(email)
    except ValidationError:
        return None


async def _create_or_promote(
    db, *, email: str, password: str | None, full_name: str | None
) -> tuple[User, bool]:
    """Return (user, created). Promotes + optionally re-passwords an existing user."""
    existing = await user_repository.get_by_email(db, email)
    if existing is not None:
        existing.role = UserRole.ADMIN
        existing.is_active = True
        existing.is_verified = True
        if password:
            existing.password_hash = hash_password(password)
        await db.flush()
        return existing, False

    user = User(
        email=email,
        password_hash=hash_password(password) if password else None,
        full_name=full_name,
        role=UserRole.ADMIN,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user, True


async def main(email: str, password: str | None, name: str | None, promote: bool) -> None:
    settings = get_settings()
    if not settings.is_local:
        # Guardrail: this bypasses registration/auth entirely. Refuse outside local
        # unless explicitly forced, so it can't silently mint an admin in prod.
        print(
            f"[!] APP_ENV is {settings.app_env!r}, not 'local'. Refusing to create an "
            "admin against a non-local database. Set APP_ENV=local (or run this only "
            "against your dev DB) if that's really what you want."
        )
        return

    if _validate_email(email) is None:
        # Matches the API's own EmailStr rules — reserved domains like ".local",
        # ".localhost", ".test" and ".invalid" are refused. Use a real TLD
        # (admin@example.com, admin@klearnote.ai, …).
        print(
            f"[!] {email!r} is not a valid email for the API (EmailStr rejects it — "
            "reserved domains like .local can't be used). Pick a real-TLD address."
        )
        return

    # A pure --promote for a new account needs no password; a fresh account does.
    if password is None and not promote:
        password = getpass.getpass("Password for the new admin: ").strip()
        if not password:
            print("[!] No password entered. Aborting.")
            return

    async with AsyncSessionLocal() as db:
        user, created = await _create_or_promote(
            db, email=email, password=password, full_name=name
        )
        await db.commit()

    verb = "Created" if created else "Promoted existing user to"
    print(f"{verb} ADMIN: {user.email} (id={user.id})")
    print(f"  role={user.role.value}  is_active={user.is_active}  is_verified={user.is_verified}")
    if password:
        print("  password: set — log in at POST /api/v1/auth/login")
    else:
        print("  password: unchanged (promote-only; use the account's existing password)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="admin account email")
    parser.add_argument(
        "--password",
        help="account password; if omitted (and not --promote), you'll be prompted",
    )
    parser.add_argument("--name", help="full name (optional)")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="promote an existing account without changing its password",
    )
    args = parser.parse_args()
    asyncio.run(main(args.email, args.password, args.name, args.promote))
