"""Storage for accounts and the emailed action tokens that activate them.

Thin on purpose, like the credential repository: the rules — who may be
disabled, when a link counts as valid, what a password must look like — live
in ``UserAdminService`` and ``app/domain/users.py``. This layer moves rows,
and the one piece of logic it does own is the atomic consumption of a token,
because that has to be a conditional ``UPDATE`` rather than a read followed by
a write to be single-use under two simultaneous clicks.

Nothing here ever returns a password hash or a token hash to a caller outside
the auth layer: ``to_public`` is the only serialisation, and it is a whitelist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from app.database.models import UserActionTokenRecord, UserRecord, utc_now
from app.database.orm import OrmDatabase
from app.domain.users import ActionTokenPurpose, UserRole, UserStatus


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for a timezone-aware column; treat
    them as the UTC they were written in so comparisons never mix kinds."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    normalised = as_utc(value)
    return normalised.isoformat().replace("+00:00", "Z") if normalised else ""


class UserRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    # --- accounts -----------------------------------------------------------

    async def list_all(self) -> list[UserRecord]:
        async with self.database.session() as db:
            rows = await db.scalars(select(UserRecord).order_by(UserRecord.created_at, UserRecord.username))
            return list(rows.all())

    async def count(self) -> int:
        async with self.database.session() as db:
            return int((await db.scalar(select(func.count()).select_from(UserRecord))) or 0)

    async def get(self, user_id: str) -> UserRecord | None:
        async with self.database.session() as db:
            return await db.get(UserRecord, str(user_id))

    async def find_by_login(self, identifier: str) -> UserRecord | None:
        """The account a login form means: matched on username *or* email.

        Both columns are stored lowercased and the identifier arrives the same
        way (``normalize_login_identifier``), so this is two indexed equality
        tests, not a case-insensitive scan.
        """
        needle = str(identifier or "")
        if not needle:
            return None
        async with self.database.session() as db:
            return await db.scalar(
                select(UserRecord).where((UserRecord.username == needle) | (UserRecord.email == needle))
            )

    async def find_by_email(self, email: str) -> UserRecord | None:
        async with self.database.session() as db:
            return await db.scalar(select(UserRecord).where(UserRecord.email == str(email)))

    async def count_active_admins(self) -> int:
        async with self.database.session() as db:
            value = await db.scalar(
                select(func.count())
                .select_from(UserRecord)
                .where(UserRecord.role == UserRole.ADMIN.value, UserRecord.status == UserStatus.ACTIVE.value)
            )
            return int(value or 0)

    async def create(
        self,
        *,
        username: str,
        email: str | None,
        display_name: str,
        role: UserRole,
        status: UserStatus,
        password_hash: str | None,
        created_by: str | None = None,
    ) -> UserRecord:
        now = utc_now()
        record = UserRecord(
            id=str(uuid4()),
            username=username,
            email=email,
            display_name=display_name,
            role=role.value,
            status=status.value,
            password_hash=password_hash,
            token_version=1,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )
        async with self.database.transaction() as db:
            db.add(record)
        return record

    async def update(self, user_id: str, mutate: Callable[[UserRecord], None]) -> UserRecord | None:
        """Apply ``mutate`` to the row inside one transaction.

        Every change to an account goes through here so ``updated_at`` moves
        with it and the caller never holds a detached copy it might write back
        over someone else's edit.
        """
        async with self.database.transaction() as db:
            record = await db.get(UserRecord, str(user_id))
            if record is None:
                return None
            mutate(record)
            record.updated_at = utc_now()
            return record

    async def delete(self, user_id: str) -> bool:
        """Remove the account and every token issued for it.

        The tokens are deleted explicitly rather than left to the foreign key:
        SQLite only honours ``ON DELETE CASCADE`` with a pragma this
        deployment does not set, and a live invitation surviving its account
        would be a link that activates nothing — or, if the id were reused,
        the wrong thing.
        """
        async with self.database.transaction() as db:
            await db.execute(delete(UserActionTokenRecord).where(UserActionTokenRecord.user_id == str(user_id)))
            result = await db.execute(delete(UserRecord).where(UserRecord.id == str(user_id)))
        return bool(result.rowcount)

    async def record_login(self, user_id: str) -> None:
        async with self.database.transaction() as db:
            await db.execute(
                update(UserRecord).where(UserRecord.id == str(user_id)).values(last_login_at=utc_now())
            )

    # --- action tokens -------------------------------------------------------

    async def issue_token(
        self,
        *,
        user_id: str,
        purpose: ActionTokenPurpose,
        token_hash: str,
        expires_at: datetime,
        created_by: str | None = None,
    ) -> UserActionTokenRecord:
        """Record a freshly minted token and void every live one of the same
        purpose for this user — a re-sent invitation replaces the earlier link
        rather than leaving two that both work."""
        now = utc_now()
        record = UserActionTokenRecord(
            id=str(uuid4()),
            user_id=str(user_id),
            purpose=purpose.value,
            token_hash=token_hash,
            expires_at=expires_at,
            used_at=None,
            created_at=now,
            created_by=created_by,
        )
        async with self.database.transaction() as db:
            await db.execute(
                update(UserActionTokenRecord)
                .where(
                    UserActionTokenRecord.user_id == str(user_id),
                    UserActionTokenRecord.purpose == purpose.value,
                    UserActionTokenRecord.used_at.is_(None),
                )
                .values(used_at=now)
            )
            db.add(record)
        return record

    async def peek_token(
        self, token_hash: str, purpose: ActionTokenPurpose
    ) -> tuple[UserActionTokenRecord, UserRecord] | None:
        """The token row and its account, live or not — the service decides
        how to describe an expired or spent one to the person holding it."""
        async with self.database.session() as db:
            token = await db.scalar(
                select(UserActionTokenRecord).where(
                    UserActionTokenRecord.token_hash == str(token_hash),
                    UserActionTokenRecord.purpose == purpose.value,
                )
            )
            if token is None:
                return None
            user = await db.get(UserRecord, token.user_id)
            if user is None:
                return None
            return token, user

    async def consume_token(self, token_hash: str, purpose: ActionTokenPurpose) -> UserActionTokenRecord | None:
        """Mark the token used, atomically, and return it — or None when it
        was already used (or never existed).

        A conditional ``UPDATE`` on ``used_at IS NULL`` is what makes this
        single-use: of two requests racing on the same link, exactly one
        matches the row. Expiry is judged by the caller on the returned row —
        an expired token that gets stamped here was unusable anyway.
        """
        now = utc_now()
        async with self.database.transaction() as db:
            result = await db.execute(
                update(UserActionTokenRecord)
                .where(
                    UserActionTokenRecord.token_hash == str(token_hash),
                    UserActionTokenRecord.purpose == purpose.value,
                    UserActionTokenRecord.used_at.is_(None),
                )
                .values(used_at=now)
            )
            if not result.rowcount:
                return None
            return await db.scalar(
                select(UserActionTokenRecord).where(UserActionTokenRecord.token_hash == str(token_hash))
            )

    async def pending_token(self, user_id: str, purpose: ActionTokenPurpose) -> UserActionTokenRecord | None:
        """The newest unconsumed token of ``purpose`` for the user, expired or not."""
        async with self.database.session() as db:
            return await db.scalar(
                select(UserActionTokenRecord)
                .where(
                    UserActionTokenRecord.user_id == str(user_id),
                    UserActionTokenRecord.purpose == purpose.value,
                    UserActionTokenRecord.used_at.is_(None),
                )
                .order_by(UserActionTokenRecord.created_at.desc())
                .limit(1)
            )

    async def list_pending_tokens(self, purpose: ActionTokenPurpose) -> dict[str, UserActionTokenRecord]:
        """Every user's newest unconsumed token of ``purpose``, in one query —
        what the admin listing needs to say "invitation expires in 2 days"
        per row without a query per row."""
        async with self.database.session() as db:
            rows = await db.scalars(
                select(UserActionTokenRecord)
                .where(
                    UserActionTokenRecord.purpose == purpose.value,
                    UserActionTokenRecord.used_at.is_(None),
                )
                .order_by(UserActionTokenRecord.created_at.asc())
            )
            # Ascending order and a plain overwrite leave the newest per user.
            return {row.user_id: row for row in rows.all()}

    async def purge_spent_tokens(self, *, older_than: datetime) -> int:
        """Housekeeping: drop consumed or expired rows older than ``older_than``.

        Kept separate from the request path — nothing waits on it — and safe to
        skip: a spent row is inert, only bulk.
        """
        async with self.database.transaction() as db:
            result = await db.execute(
                delete(UserActionTokenRecord).where(
                    (UserActionTokenRecord.used_at.is_not(None)) | (UserActionTokenRecord.expires_at < older_than),
                    UserActionTokenRecord.created_at < older_than,
                )
            )
        return int(result.rowcount or 0)

    # --- serialisation -------------------------------------------------------

    @staticmethod
    def to_public(record: UserRecord, *, invite: UserActionTokenRecord | None = None) -> dict[str, Any]:
        """What the admin screen and ``/me`` are allowed to see.

        A whitelist: the hash and the token rows are never in it, and adding a
        column to the model does not add it here.
        """
        pending = None
        if invite is not None:
            expires_at = as_utc(invite.expires_at)
            pending = {
                "sentAt": _iso(invite.created_at),
                "expiresAt": _iso(invite.expires_at),
                "expired": bool(expires_at and expires_at <= utc_now()),
            }
        return {
            "id": record.id,
            "username": record.username,
            "email": record.email or "",
            "displayName": record.display_name or "",
            "role": record.role,
            "status": record.status,
            "createdAt": _iso(record.created_at),
            "updatedAt": _iso(record.updated_at),
            "createdBy": record.created_by or "",
            "lastLoginAt": _iso(record.last_login_at),
            "invite": pending,
        }
