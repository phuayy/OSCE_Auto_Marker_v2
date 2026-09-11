"""Storage for operator-defined scoring providers.

Thin on purpose, like its sibling for credentials: every decision about what a
definition may contain lives in ``app/llm/custom.py``, so this layer only moves
rows. Nothing here can store an invalid definition because nothing here parses
one — callers hand it a validated :class:`CustomProviderSpec`.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from app.database.models import CustomProviderRecord, utc_now
from app.database.orm import OrmDatabase
from app.llm.custom import CustomProviderSpec


class CustomProviderRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def list_all(self) -> list[CustomProviderRecord]:
        async with self.database.session() as db:
            return list(
                (
                    await db.scalars(select(CustomProviderRecord).order_by(CustomProviderRecord.label))
                ).all()
            )

    async def get(self, provider_id: str) -> CustomProviderRecord | None:
        async with self.database.session() as db:
            return await db.get(CustomProviderRecord, str(provider_id))

    async def upsert(self, spec: CustomProviderSpec, *, updated_by: str | None = None) -> None:
        """Create or replace one definition.

        A replace, not a merge: the settings form sends the whole definition, and
        merging would make "clear this field" impossible to express — a header
        the operator deleted would survive every subsequent save.
        """
        async with self.database.transaction() as db:
            record = await db.get(CustomProviderRecord, spec.id)
            now = utc_now()
            if record is None:
                db.add(
                    CustomProviderRecord(
                        id=spec.id,
                        label=spec.label,
                        vendor=spec.vendor,
                        description=spec.description,
                        base_url=spec.base_url,
                        api_format=spec.api_format,
                        config_json=spec.to_storage(),
                        enabled=spec.enabled,
                        created_at=now,
                        updated_at=now,
                        updated_by=updated_by,
                    )
                )
                return
            record.label = spec.label
            record.vendor = spec.vendor
            record.description = spec.description
            record.base_url = spec.base_url
            record.api_format = spec.api_format
            record.config_json = spec.to_storage()
            record.enabled = spec.enabled
            record.updated_at = now
            record.updated_by = updated_by

    async def delete(self, provider_id: str) -> bool:
        async with self.database.transaction() as db:
            result = await db.execute(
                delete(CustomProviderRecord).where(CustomProviderRecord.id == str(provider_id))
            )
        return bool(result.rowcount)

    @staticmethod
    def to_spec(record: CustomProviderRecord) -> CustomProviderSpec:
        """Rebuild a definition from its row.

        The columns win over ``config_json`` for the fields both carry: they are
        the ones a database-level edit would touch, and a row that disagrees with
        itself should behave the way it reads.
        """
        payload = dict(record.config_json or {})
        payload.update(
            {
                "id": record.id,
                "label": record.label,
                "vendor": record.vendor,
                "description": record.description,
                "baseUrl": record.base_url,
                "apiFormat": record.api_format,
                "enabled": record.enabled,
                "createdAt": record.created_at.isoformat() if record.created_at else "",
                "updatedAt": record.updated_at.isoformat() if record.updated_at else "",
                "updatedBy": record.updated_by or "",
            }
        )
        return CustomProviderSpec.from_raw(payload)
