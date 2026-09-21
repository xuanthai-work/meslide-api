"""Create or reset local login users user1/user2/user3.

Run from /app/servers/fastapi inside the API container. Prints 16-character
passwords for accounts that were created or reset. Does not print passwords
for unchanged existing users unless --reset is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
from sqlalchemy import func, select

from api.v1.auth.users import PASSWORD_HELPER
from models.sql.user import User
from services.database import async_session_maker

USERNAMES = ("user1", "user2", "user3")
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 16


def _random_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


async def seed_users(*, reset: bool) -> list[tuple[str, str]]:
    issued: list[tuple[str, str]] = []
    async with async_session_maker() as session:
        for username in USERNAMES:
            existing = await session.scalar(
                select(User).where(func.lower(User.username) == username)
            )
            password = _random_password()
            if existing is None:
                session.add(
                    User(
                        username=username,
                        hashed_password=PASSWORD_HELPER.hash(password),
                        is_active=True,
                        is_verified=True,
                        auth_version=1,
                    )
                )
                issued.append((username, password))
                continue
            if reset:
                existing.hashed_password = PASSWORD_HELPER.hash(password)
                existing.is_active = True
                existing.auth_version = int(existing.auth_version or 1) + 1
                session.add(existing)
                issued.append((username, password))
        await session.commit()
    return issued


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Rotate passwords for user1/user2/user3 if they already exist.",
    )
    args = parser.parse_args()
    issued = asyncio.run(seed_users(reset=args.reset))
    if not issued:
        print("Users already exist. Re-run with --reset to rotate passwords.")
        return
    print("Created/reset login accounts:")
    for username, password in issued:
        print(f"  {username}\t{password}")


if __name__ == "__main__":
    main()
