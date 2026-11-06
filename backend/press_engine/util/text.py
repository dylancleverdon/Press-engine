"""Name / text normalisation used for matching people, artists and pages."""

from __future__ import annotations

import re
import unicodedata

_SPECIAL = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
     "œ": "oe", "Œ": "OE", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D", "ı": "i"}
)
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def fold(s: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not s:
        return ""
    s = s.translate(_SPECIAL)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _NON_WORD.sub(" ", s.lower()).replace("_", " ")
    return _SPACES.sub(" ", s).strip()


def name_key(s: str | None) -> str:
    """Stable key for artist / person names ("The Comet Is Coming" == "comet is coming")."""
    k = fold(s)
    if k.startswith("the "):
        k = k[4:]
    return k


def contains_phrase(haystack: str | None, needle: str | None) -> bool:
    """Whole-word, accent/case-insensitive containment."""
    n = fold(needle)
    if not n:
        return False
    h = f" {fold(haystack)} "
    if f" {n} " in h:
        return True
    # CJK text has no spaces between words; fall back to plain containment.
    if any(ord(c) > 0x2E80 for c in n):
        return n in h
    return False


def name_tokens(name: str | None) -> list[str]:
    return [t for t in fold(name).split() if len(t) >= 2]


GENERIC_BYLINES = {
    "admin", "staff", "editor", "editors", "editorial", "editorial team", "redaktion", "redaktionen",
    "redaktoren", "redactie", "team", "guest", "guest author", "contributor", "news desk", "webmaster",
    "anonymous", "unknown", "press", "music", "musik", "redazione", "redaccion", "author",
}


def looks_like_person_name(name: str | None) -> bool:
    """Cheap filter against bylines like "Staff" or "Redaktionen" and sentence-length garbage."""
    if not name:
        return False
    f = fold(name)
    if not f or f in GENERIC_BYLINES:
        return False
    if any(ch.isdigit() for ch in name) or "@" in name or "http" in f:
        return False
    words = f.split()
    if len(words) > 5 or len(name) > 60:
        return False
    # CJK names are often written without spaces.
    if len(words) == 1 and not any(ord(c) > 0x2E80 for c in name):
        return len(words[0]) >= 3
    return True


def clean_name(name: str) -> str:
    name = re.sub(r"^(by|af|von|door|par|di|por|av|text:|tekst:|text von)\s+", "", name.strip(), flags=re.I)
    return _SPACES.sub(" ", name).strip(" ,;|-–")


def truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"
