"""Deterministic synthetic transcript + corpus for the correction golden test."""
from __future__ import annotations

import random

_TERMS = [
    "paracetamol", "salbutamol", "prednisolone", "prednisone", "nasal block", "sore throat",
    "amoxicillin", "ibuprofen", "omeprazole", "metformin", "atorvastatin", "lisinopril",
    "amlodipine", "ventolin", "chest pain", "shortness of breath", "diabetes mellitus",
    "hypertension", "gastroenteritis", "naproxen", "cetirizine", "loratadine", "dexamethasone",
    "hydrocortisone", "clarithromycin", "doxycycline", "ciprofloxacin", "warfarin", "heparin",
    "insulin glargine", "blood pressure", "heart rate", "oxygen saturation", "penicillin allergy",
    "migraine", "asthma", "bronchitis", "pneumonia", "tonsillitis", "sinusitis",
]
_NOISE = [
    "para set a mole", "sal butter mole", "nasal blog", "pred nis alone", "sure that", "the", "patient",
    "I", "will", "make sure that everything is fine", "and", "take", "twice", "a", "day", "okay",
    "Doctor", "café", "naïve", "résumé", "have", "you", "any", "pain", "ibu profen", "o me prazole",
    "met form in", "amoxi sillin", "hyper tension", "Chest pain", "sore throat", "nasal block",
]


def build_fixture(segment_count: int, term_count: int, words: int = 35, seed: int = 7):
    rng = random.Random(seed)
    terms = (_TERMS * (term_count // len(_TERMS) + 1))[:term_count]
    if term_count > len(_TERMS):
        terms = _TERMS + [f"synthetic{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}ol" for i in range(term_count - len(_TERMS))]
    pool = _NOISE + _TERMS
    segments = []
    for index in range(segment_count):
        text = " ".join(rng.choice(pool) for _ in range(words // 2))
        segments.append({"id": index, "text": text})
    return segments, terms
