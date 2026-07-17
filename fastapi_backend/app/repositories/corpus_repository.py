from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from app.database.models import CorpusRecord, utc_now
from app.database.orm import OrmDatabase


logger = logging.getLogger(__name__)

# Inserted only when the corpora table is empty, so deleting a seed corpus does
# not resurrect it on the next startup.
SEED_CORPORA: list[tuple[str, list[str]]] = [
    (
        "General OSCE",
        [
            "chief complaint",
            "presenting complaint",
            "past medical history",
            "medication history",
            "drug allergy",
            "family history",
            "social history",
            "vital signs",
            "blood pressure",
            "heart rate",
            "respiratory rate",
            "oxygen saturation",
            "physical examination",
            "auscultation",
            "palpation",
            "percussion",
            "differential diagnosis",
            "provisional diagnosis",
            "investigations",
            "full blood count",
            "follow-up",
            "referral letter",
            "paracetamol",
            "ibuprofen",
            "antibiotics",
            "side effects",
        ],
    ),
    (
        "Common Cold (URTI)",
        [
            "common cold",
            "upper respiratory tract infection",
            "nasal block",
            "blocked nose",
            "nasal congestion",
            "runny nose",
            "rhinorrhoea",
            "post-nasal drip",
            "sore throat",
            "sneezing",
            "cough",
            "phlegm",
            "sputum",
            "fever",
            "chills",
            "headache",
            "body ache",
            "myalgia",
            "sinusitis",
            "rhinitis",
            "influenza",
            "paracetamol",
            "antihistamine",
            "chlorpheniramine",
            "loratadine",
            "decongestant",
            "pseudoephedrine",
            "lozenges",
            "plenty of fluids",
        ],
    ),
]


def _record_to_dict(record: CorpusRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "terms": list(record.terms or []),
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "updatedAt": record.updated_at.isoformat() if record.updated_at else None,
    }


class CorpusRepository:
    def __init__(self, database: OrmDatabase) -> None:
        self.database = database

    async def seed_defaults(self) -> int:
        async with self.database.transaction() as db:
            existing = int(await db.scalar(select(func.count()).select_from(CorpusRecord)) or 0)
            if existing:
                return 0
            for name, terms in SEED_CORPORA:
                db.add(CorpusRecord(id=str(uuid4()), name=name, terms=terms))
        logger.info("Seeded %d default transcription corpora.", len(SEED_CORPORA))
        return len(SEED_CORPORA)

    async def list_rows(self) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            records = (await db.scalars(select(CorpusRecord).order_by(CorpusRecord.name))).all()
        return [_record_to_dict(record) for record in records]

    async def get(self, corpus_id: str) -> dict[str, Any] | None:
        async with self.database.session() as db:
            record = await db.get(CorpusRecord, corpus_id)
        return _record_to_dict(record) if record else None

    async def create(self, name: str, terms: list[str]) -> dict[str, Any]:
        record = CorpusRecord(id=str(uuid4()), name=name, terms=terms)
        async with self.database.transaction() as db:
            db.add(record)
        return _record_to_dict(record)

    async def update(self, corpus_id: str, name: str, terms: list[str]) -> dict[str, Any] | None:
        async with self.database.transaction() as db:
            record = await db.get(CorpusRecord, corpus_id)
            if record is None:
                return None
            record.name = name
            record.terms = terms
            record.updated_at = utc_now()
            return _record_to_dict(record)

    async def delete(self, corpus_id: str) -> bool:
        async with self.database.transaction() as db:
            record = await db.get(CorpusRecord, corpus_id)
            if record is None:
                return False
            await db.delete(record)
        return True
