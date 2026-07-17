from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

CORPUS_NAME_MAX_LENGTH = 120
CORPUS_MAX_TERMS = 200
CORPUS_TERM_MAX_LENGTH = 64


def normalize_corpus_terms(raw_terms: Any) -> list[str]:
    """Trim, drop empties, cap term length, and dedupe case-insensitively while
    preserving order (earlier terms win the WhisperX prompt budget)."""
    if not isinstance(raw_terms, list):
        return []
    seen: set[str] = set()
    terms: list[str] = []
    for raw in raw_terms:
        term = str(raw or "").strip()[:CORPUS_TERM_MAX_LENGTH]
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


class CorpusPayload(BaseModel):
    name: str = Field(min_length=1, max_length=CORPUS_NAME_MAX_LENGTH)
    terms: list[str] = Field(default_factory=list, max_length=CORPUS_MAX_TERMS)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Corpus name is required.")
        return cleaned[:CORPUS_NAME_MAX_LENGTH]

    @field_validator("terms", mode="before")
    @classmethod
    def clean_terms(cls, value: Any) -> list[str]:
        return normalize_corpus_terms(value)
