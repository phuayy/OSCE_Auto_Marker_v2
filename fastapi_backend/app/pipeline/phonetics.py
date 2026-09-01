"""Double Metaphone phonetic encoding, vendored.

Lawrence Philips' Double Metaphone (2000) reduces a written token to the
consonant skeleton of how it is *said*, with a second code for the plausible
alternate pronunciation of foreign-origin spellings. It is the right key for
correcting an ASR transcript: a speech model does not misspell a drug name by a
character or two, it emits a different sequence of ordinary words that sounds
like the drug name — "paracetamol" comes back as "para set a mole", which is at
edit distance 4 from the truth but phonetically identical (both encode PRSTML).

Vendored rather than pulled in as a dependency: the transcript corrector is
deliberately dependency-free and deterministic (stdlib ``difflib`` only, no API
calls), and this file keeps it that way. The rule set is the standard English
one from the published algorithm.

Two deliberate deviations from the canonical implementation:

* No truncation. The reference implementation stops at four codes, which is
  right for surname indexing and useless here: every long drug name would
  collapse to the same prefix (PRST for both "paracetamol" and "prasterone").
  ``max_length`` is available for callers who want the classic behaviour.
* Input is normalised to letters only and encoded as a single token, so the
  handful of canonical rules keyed on an embedded space ("VAN ", "SAN ") are
  unreachable by construction.
"""
from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["double_metaphone", "phonetic_codes", "phonetic_key"]

_NON_ALPHA = re.compile(r"[^A-Za-z]+")
_VOWELS = frozenset("AEIOUY")


class _DoubleMetaphone:
    """One encoding pass over one token.

    ``primary`` and ``secondary`` are built in step: every rule appends to both,
    so the two codes stay aligned and a rule that has no alternate simply
    contributes the same fragment twice.
    """

    def __init__(self, word: str) -> None:
        self.word = word
        self.length = len(word)
        self.last = self.length - 1
        self.primary: list[str] = []
        self.secondary: list[str] = []
        # The Slavo-Germanic test decides several branches (J, G, S, Z): those
        # spellings take the harder, non-Romance pronunciation.
        self.slavo_germanic = bool(
            {"W", "K"} & set(word) or "CZ" in word or "WITZ" in word
        )

    # -- helpers ---------------------------------------------------------
    def at(self, start: int, size: int = 1) -> str:
        """Slice safely; out-of-range reads are the empty string, never a wrap."""
        if start < 0:
            return ""
        return self.word[start : start + size]

    def add(self, primary: str, secondary: str | None = None) -> None:
        self.primary.append(primary)
        self.secondary.append(primary if secondary is None else secondary)

    def is_vowel(self, index: int) -> bool:
        return self.at(index) in _VOWELS

    # -- driver ----------------------------------------------------------
    def encode(self, max_length: int | None) -> tuple[str, str]:
        current = self._skip_silent_start()
        while current < self.length:
            if max_length is not None and len(self.key(self.primary)) >= max_length:
                break
            handler = self._HANDLERS.get(self.at(current))
            current += 1 if handler is None else handler(self, current)
        primary = self.key(self.primary)
        secondary = self.key(self.secondary)
        if max_length is not None:
            primary, secondary = primary[:max_length], secondary[:max_length]
        return primary, secondary

    @staticmethod
    def key(fragments: list[str]) -> str:
        return "".join(fragments)

    def _skip_silent_start(self) -> int:
        if self.at(0, 2) in ("GN", "KN", "PN", "WR", "PS"):
            return 1
        if self.at(0) == "X":  # "Xavier" is said with an S
            self.add("S")
            return 1
        return 0

    # -- per-letter rules ------------------------------------------------
    def _vowel(self, current: int) -> int:
        # Only an initial vowel survives; interior vowels carry no code.
        if current == 0:
            self.add("A")
        return 1

    def _b(self, current: int) -> int:
        self.add("P")
        return 2 if self.at(current + 1) == "B" else 1

    def _c(self, current: int) -> int:
        if (
            current > 1
            and not self.is_vowel(current - 2)
            and self.at(current - 1, 3) == "ACH"
            and self.at(current + 2) != "I"
            and (self.at(current + 2) != "E" or self.at(current - 2, 6) in ("BACHER", "MACHER"))
        ):
            self.add("K")
            return 2
        if current == 0 and self.at(0, 6) == "CAESAR":
            self.add("S")
            return 2
        if self.at(current, 4) == "CHIA":  # Italian "chianti"
            self.add("K")
            return 2
        if self.at(current, 2) == "CH":
            return self._ch(current)
        if self.at(current, 2) == "CZ" and self.at(current - 2, 4) != "WICZ":
            self.add("S", "X")
            return 2
        if self.at(current + 1, 3) == "CIA":
            self.add("X")
            return 3
        if self.at(current, 2) == "CC" and not (current == 1 and self.at(0) == "M"):
            return self._cc(current)
        if self.at(current, 2) in ("CK", "CG", "CQ"):
            self.add("K")
            return 2
        if self.at(current, 2) in ("CI", "CE", "CY"):
            self.add("S", "X") if self.at(current, 3) in ("CIO", "CIE", "CIA") else self.add("S")
            return 2
        self.add("K")
        if self.at(current + 1) in ("C", "K", "Q") and self.at(current + 1, 2) not in ("CE", "CI"):
            return 2
        return 1

    def _ch(self, current: int) -> int:
        if current > 0 and self.at(current, 4) == "CHAE":  # "Michael"
            self.add("K", "X")
            return 2
        if (
            current == 0
            and (
                self.at(current + 1, 5) in ("HARAC", "HARIS")
                or self.at(current + 1, 3) in ("HOR", "HYM", "HIA", "HEM")
            )
            and self.at(0, 5) != "CHORE"
        ):
            # Greek roots: "character", "chemistry", "chiasm"
            self.add("K")
            return 2
        if (
            self.at(0, 3) == "SCH"
            or self.at(current - 2, 6) in ("ORCHES", "ARCHIT", "ORCHID")
            or self.at(current + 2) in ("T", "S")
            or (
                (self.is_vowel(current - 1) or current == 0)
                and self.at(current + 2) in ("L", "R", "N", "M", "B", "H", "F", "V", "W")
            )
        ):
            self.add("K")
            return 2
        if current > 0:
            self.add("K") if self.at(0, 2) == "MC" else self.add("X", "K")
        else:
            self.add("X")
        return 2

    def _cc(self, current: int) -> int:
        if self.at(current + 2) in ("I", "E", "H") and self.at(current + 2, 2) != "HU":
            if (current == 1 and self.at(current - 1) == "A") or self.at(current - 1, 5) in (
                "UCCEE",
                "UCCES",
            ):
                self.add("KS")  # "accident", "success"
            else:
                self.add("X")  # "bocce"
            return 3
        self.add("K")
        return 2

    def _d(self, current: int) -> int:
        if self.at(current, 2) == "DG":
            if self.at(current + 2) in ("I", "E", "Y"):  # "edge"
                self.add("J")
                return 3
            self.add("TK")
            return 2
        if self.at(current, 2) in ("DT", "DD"):
            self.add("T")
            return 2
        self.add("T")
        return 1

    def _f(self, current: int) -> int:
        self.add("F")
        return 2 if self.at(current + 1) == "F" else 1

    def _g(self, current: int) -> int:
        if self.at(current + 1) == "H":
            return self._gh(current)
        if self.at(current + 1) == "N":
            if current == 1 and self.is_vowel(0) and not self.slavo_germanic:
                self.add("KN", "N")
            elif self.at(current + 2, 2) != "EY" and self.at(current + 1) != "Y" and not self.slavo_germanic:
                self.add("N", "KN")
            else:
                self.add("KN")
            return 2
        if self.at(current + 1, 2) == "LI" and not self.slavo_germanic:
            self.add("KL", "L")
            return 2
        if current == 0 and (
            self.at(current + 1) == "Y"
            or self.at(current + 1, 2)
            in ("ES", "EP", "EB", "EL", "EY", "IB", "IL", "IN", "IE", "EI", "ER")
        ):
            self.add("K", "J")
            return 2
        if (
            (self.at(current + 1, 2) == "ER" or self.at(current + 1) == "Y")
            and self.at(0, 6) not in ("DANGER", "RANGER", "MANGER")
            and self.at(current - 1) not in ("E", "I")
            and self.at(current - 1, 3) not in ("RGY", "OGY")
        ):
            self.add("K", "J")
            return 2
        if self.at(current + 1) in ("E", "I", "Y") or self.at(current - 1, 4) in ("AGGI", "OGGI"):
            if self.at(0, 3) == "SCH" or self.at(current + 1, 2) == "ET":
                self.add("K")
            else:
                self.add("J", "K")
            return 2
        self.add("K")
        return 2 if self.at(current + 1) == "G" else 1

    def _gh(self, current: int) -> int:
        if current > 0 and not self.is_vowel(current - 1):
            self.add("K")
            return 2
        if current == 0:
            self.add("J") if self.at(current + 2) == "I" else self.add("K")
            return 2
        if (
            (current > 1 and self.at(current - 2) in ("B", "H", "D"))
            or (current > 2 and self.at(current - 3) in ("B", "H", "D"))
            or (current > 3 and self.at(current - 4) in ("B", "H"))
        ):
            return 2  # silent: "hough", "bough"
        if current > 2 and self.at(current - 1) == "U" and self.at(current - 3) in ("C", "G", "L", "R", "T"):
            self.add("F")  # "laugh", "rough"
        elif current > 0 and self.at(current - 1) != "I":
            self.add("K")
        return 2

    def _h(self, current: int) -> int:
        # Only pronounced between a vowel and a vowel, or word-initially.
        if (current == 0 or self.is_vowel(current - 1)) and self.is_vowel(current + 1):
            self.add("H")
            return 2
        return 1

    def _j(self, current: int) -> int:
        if self.at(current, 4) == "JOSE":
            self.add("H") if current == 0 else self.add("J", "H")
            return 1
        if current == 0:
            self.add("J", "A")
        elif (
            self.is_vowel(current - 1)
            and not self.slavo_germanic
            and self.at(current + 1) in ("A", "O")
        ):
            self.add("J", "H")
        elif current == self.last:
            self.add("J", "")
        elif self.at(current + 1) not in ("L", "T", "K", "S", "N", "M", "B", "Z") and self.at(
            current - 1
        ) not in ("S", "K", "L"):
            self.add("J")
        return 2 if self.at(current + 1) == "J" else 1

    def _k(self, current: int) -> int:
        self.add("K")
        return 2 if self.at(current + 1) == "K" else 1

    def _l(self, current: int) -> int:
        if self.at(current + 1) == "L":
            # Spanish "-illo", "-illa": the second pronunciation drops the L.
            if (
                current == self.length - 3
                and self.at(current - 1, 4) in ("ILLO", "ILLA", "ALLE")
            ) or (
                (self.at(self.last - 1, 2) in ("AS", "OS") or self.at(self.last) in ("A", "O"))
                and self.at(current - 1, 4) == "ALLE"
            ):
                self.add("L", "")
            else:
                self.add("L")
            return 2
        self.add("L")
        return 1

    def _m(self, current: int) -> int:
        self.add("M")
        if (
            self.at(current - 1, 3) == "UMB"
            and (current + 1 == self.last or self.at(current + 2, 2) == "ER")
        ) or self.at(current + 1) == "M":
            return 2  # silent B of "thumb", "dumber"
        return 1

    def _n(self, current: int) -> int:
        self.add("N")
        return 2 if self.at(current + 1) == "N" else 1

    def _p(self, current: int) -> int:
        if self.at(current + 1) == "H":  # "phenytoin"
            self.add("F")
            return 2
        self.add("P")
        return 2 if self.at(current + 1) in ("P", "B") else 1

    def _q(self, current: int) -> int:
        self.add("K")
        return 2 if self.at(current + 1) == "Q" else 1

    def _r(self, current: int) -> int:
        # French final "-ier" is silent in the primary reading.
        if (
            current == self.last
            and not self.slavo_germanic
            and self.at(current - 2, 2) == "IE"
            and self.at(current - 4, 2) not in ("ME", "MA")
        ):
            self.add("", "R")
        else:
            self.add("R")
        return 2 if self.at(current + 1) == "R" else 1

    def _s(self, current: int) -> int:
        if self.at(current - 1, 3) in ("ISL", "YSL"):
            return 1  # silent: "island"
        if current == 0 and self.at(0, 5) == "SUGAR":
            self.add("X", "S")
            return 1
        if self.at(current, 2) == "SH":
            self.add("S") if self.at(current + 1, 4) in ("HEIM", "HOEK", "HOLM", "HOLZ") else self.add("X")
            return 2
        if self.at(current, 3) in ("SIO", "SIA") or self.at(current, 4) == "SIAN":
            self.add("S") if self.slavo_germanic else self.add("S", "X")
            return 3
        if (current == 0 and self.at(current + 1) in ("M", "N", "L", "W")) or self.at(current + 1) == "Z":
            self.add("S", "X")
            return 2 if self.at(current + 1) == "Z" else 1
        if self.at(current, 2) == "SC":
            return self._sc(current)
        if current == self.last and self.at(current - 2, 2) in ("AI", "OI"):
            self.add("", "S")  # French "-ais", "-ois"
        else:
            self.add("S")
        return 2 if self.at(current + 1) in ("S", "Z") else 1

    def _sc(self, current: int) -> int:
        if self.at(current + 2) == "H":
            if self.at(current + 3, 2) in ("OO", "ER", "EN", "UY", "ED", "EM"):
                self.add("X", "SK") if self.at(current + 3, 2) in ("ER", "EN") else self.add("SK")
            elif current == 0 and not self.is_vowel(3) and self.at(3) != "W":
                self.add("X", "S")
            else:
                self.add("X")
            return 3
        if self.at(current + 2) in ("I", "E", "Y"):  # "science"
            self.add("S")
            return 3
        self.add("SK")
        return 3

    def _t(self, current: int) -> int:
        if self.at(current, 4) == "TION":
            self.add("X")
            return 3
        if self.at(current, 3) in ("TIA", "TCH"):
            self.add("X")
            return 3
        if self.at(current, 2) == "TH" or self.at(current, 3) == "TTH":
            if self.at(current + 2, 2) in ("OM", "AM") or self.at(0, 3) == "SCH":
                self.add("T")
            else:
                self.add("0", "T")
            return 2
        self.add("T")
        return 2 if self.at(current + 1) in ("T", "D") else 1

    def _v(self, current: int) -> int:
        self.add("F")
        return 2 if self.at(current + 1) == "V" else 1

    def _w(self, current: int) -> int:
        if self.at(current, 2) == "WR":
            self.add("R")
            return 2
        if current == 0 and (self.is_vowel(current + 1) or self.at(current, 2) == "WH"):
            self.add("A", "F") if self.is_vowel(current + 1) else self.add("A")
            return 1
        if (
            (current == self.last and self.is_vowel(current - 1))
            or self.at(current - 1, 5) in ("EWSKI", "EWSKY", "OWSKI", "OWSKY")
            or self.at(0, 3) == "SCH"
        ):
            self.add("", "F")
            return 1
        if self.at(current, 4) in ("WICZ", "WITZ"):
            self.add("TS", "FX")
            return 4
        return 1

    def _x(self, current: int) -> int:
        # Silent in French endings: "beaux", "faux".
        if not (
            current == self.last
            and (self.at(current - 3, 3) in ("IAU", "EAU") or self.at(current - 2, 2) in ("AU", "OU"))
        ):
            self.add("KS")
        return 2 if self.at(current + 1) in ("C", "X") else 1

    def _z(self, current: int) -> int:
        if self.at(current + 1) == "H":
            self.add("J")
            return 2
        if self.at(current + 1, 2) in ("ZO", "ZI", "ZA") or (
            self.slavo_germanic and current > 0 and self.at(current - 1) != "T"
        ):
            self.add("S", "TS")
        else:
            self.add("S")
        return 2 if self.at(current + 1) == "Z" else 1

    # dict.fromkeys, not a comprehension: a comprehension in a class body
    # cannot see the methods defined beside it.
    _HANDLERS = dict.fromkeys("AEIOUY", _vowel) | {
        "B": _b,
        "C": _c,
        "D": _d,
        "F": _f,
        "G": _g,
        "H": _h,
        "J": _j,
        "K": _k,
        "L": _l,
        "M": _m,
        "N": _n,
        "P": _p,
        "Q": _q,
        "R": _r,
        "S": _s,
        "T": _t,
        "V": _v,
        "W": _w,
        "X": _x,
        "Z": _z,
    }


@lru_cache(maxsize=8192)
def double_metaphone(word: str, max_length: int | None = None) -> tuple[str, str]:
    """Encode one token as ``(primary, alternate)``.

    Both codes are empty for input with no letters. ``max_length`` restores the
    reference implementation's truncation; the default keeps the full key,
    which is what discriminates long clinical vocabulary.
    """
    cleaned = _NON_ALPHA.sub("", str(word or "")).upper()
    if not cleaned:
        return "", ""
    return _DoubleMetaphone(cleaned).encode(max_length)


def phonetic_codes(text: str) -> tuple[str, str]:
    """Encode a phrase as if spoken without its word boundaries.

    This is the property the corrector needs: a transcriber that splits one
    spoken word across several written ones ("para set a mole") must produce
    the same key as the word itself, so the phrase is encoded as one token.
    """
    return double_metaphone(_NON_ALPHA.sub("", str(text or "")))


def phonetic_key(text: str) -> str:
    """Primary code only, for callers that do not weigh the alternate."""
    return phonetic_codes(text)[0]
