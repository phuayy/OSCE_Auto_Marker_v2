"""Storage for the sealed provider API keys.

Thin on purpose: every crypto decision lives in ``ProviderCredentialService``,
so this layer only ever moves ciphertext. Nothing here can accidentally return
plaintext because nothing here has ever seen any.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from app.database.models import ProviderCredentialRecord, utc_now
from app.database.orm import OrmDatabase


class ProviderCredentialRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def list_all(self) -> list[ProviderCredentialRecord]:
        async with self.database.session() as db:
            return list((await db.scalars(select(ProviderCredentialRecord))).all())

    async def get(self, provider_id: str) -> ProviderCredentialRecord | None:
        async with self.database.session() as db:
            return await db.get(ProviderCredentialRecord, str(provider_id))

    async def upsert(
        self,
        provider_id: str,
        *,
        ciphertext: str,
        nonce: str,
        key_fingerprint: str,
        last4: str,
        updated_by: str | None = None,
    ) -> None:
        """Write a new key for a provider, replacing whatever was there.

        Rotation is an overwrite, not an append: the previous key is gone from
        the database the moment the new one lands, so a leaked backup taken
        after a rotation cannot yield the credential that was rotated away.
        """
        async with self.database.transaction() as db:
            record = await db.get(ProviderCredentialRecord, str(provider_id))
            now = utc_now()
            if record is None:
                db.add(
                    ProviderCredentialRecord(
                        provider_id=str(provider_id),
                        ciphertext=ciphertext,
                        nonce=nonce,
                        key_fingerprint=key_fingerprint,
                        last4=last4,
                        created_at=now,
                        updated_at=now,
                        updated_by=updated_by,
                    )
                )
                return
            record.ciphertext = ciphertext
            record.nonce = nonce
            record.key_fingerprint = key_fingerprint
            record.last4 = last4
            record.updated_at = now
            record.updated_by = updated_by
            # A rotated key has not been tested yet; carrying the old verdict
            # forward would show a green tick for a credential nobody probed.
            record.last_tested_at = None
            record.last_test_ok = None
            record.last_test_error = None

    async def delete(self, provider_id: str) -> bool:
        async with self.database.transaction() as db:
            result = await db.execute(
                delete(ProviderCredentialRecord).where(
                    ProviderCredentialRecord.provider_id == str(provider_id)
                )
            )
        return bool(result.rowcount)

    async def record_test(self, provider_id: str, *, ok: bool, error: str = "") -> None:
        async with self.database.transaction() as db:
            record = await db.get(ProviderCredentialRecord, str(provider_id))
            if record is None:
                return
            record.last_tested_at = utc_now()
            record.last_test_ok = bool(ok)
            record.last_test_error = str(error or "")[:2000] or None

    @staticmethod
    def to_status(record: ProviderCredentialRecord) -> dict[str, Any]:
        return {
            "providerId": record.provider_id,
            "last4": record.last4 or "",
            "updatedAt": record.updated_at.isoformat() if record.updated_at else "",
            "updatedBy": record.updated_by or "",
            "lastTestedAt": record.last_tested_at.isoformat() if record.last_tested_at else "",
            "lastTestOk": record.last_test_ok,
            "lastTestError": record.last_test_error or "",
        }
