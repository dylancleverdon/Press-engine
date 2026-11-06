"""Email finding and classification.

The core rule from the spec (§4.5): never send to an address that wasn't found
verbatim on a fetched page. ``verbatim`` here means the exact string appears
in the page's HTML/text or a mailto: link, or is Cloudflare email protection
(which is what a browser shows the reader). Obfuscated forms like
"name [at] site [dot] com" are kept separately as *unverified* and need a
human to confirm them in the dashboard.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from .text import fold, name_tokens

EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})(?![\w-])"
)
CF_ATTR_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+-]{1,64})\s*[\[\(\{<]\s*(?:at|@)\s*[\]\)\}>]\s*"
    r"((?:[A-Za-z0-9-]+\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*)+[A-Za-z]{2,24})",
    re.I,
)

BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "tif", "tiff", "mp4", "mp3", "woff", "woff2", "ttf"}
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "domain.com", "email.com", "yourdomain.com", "yoursite.com", "test.com",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "address.com", "mysite.com", "company.com",
}
PLACEHOLDER_LOCALS = {"you", "your", "yourname", "name", "email", "user", "username", "firstname", "first.last", "john.doe", "jane.doe", "xxx"}

GENERIC_LOCALS = {
    "info", "contact", "hello", "hi", "hey", "mail", "email", "office", "admin", "team", "support", "help",
    "editor", "editors", "editorial", "redaktion", "redaktionen", "redactie", "redazione", "redaccion", "redaction",
    "news", "newsdesk", "tips", "press", "pr", "media", "submissions", "submission", "submit", "promo", "promos",
    "music", "musik", "musique", "reviews", "review", "features", "events", "marketing", "ads", "advertising",
    "sales", "booking", "bookings", "management", "jobs", "post", "kontakt", "kontakte", "service", "webmaster",
    "noreply", "no-reply", "privacy", "legal", "abuse", "feedback", "letters", "general", "studio", "desk",
}


def decode_cfemail(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _plausible(email: str) -> bool:
    local, _, domain = email.rpartition("@")
    domain = domain.lower()
    tld = domain.rsplit(".", 1)[-1]
    if tld in BAD_TLDS or domain in PLACEHOLDER_DOMAINS or local.lower() in PLACEHOLDER_LOCALS:
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", local.lower()):  # tracking hashes
        return False
    return "." in domain and len(email) <= 254


def normalize_email(email: str) -> str:
    return email.strip().strip(".").lower()


@dataclass
class EmailFindings:
    verbatim: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


def find_emails(html: str | None, text: str | None = None) -> EmailFindings:
    raw = (html or "") + "\n" + (text or "")
    unescaped = htmllib.unescape(unquote(raw))
    found: dict[str, None] = {}
    for m in EMAIL_RE.finditer(unescaped):
        e = normalize_email(m.group(1))
        if _plausible(e):
            found[e] = None
    for rx in (CF_ATTR_RE, CF_HREF_RE):
        for m in rx.finditer(raw):
            e = decode_cfemail(m.group(1))
            if e and EMAIL_RE.fullmatch(e) and _plausible(e):
                found[normalize_email(e)] = None
    unverified: dict[str, None] = {}
    for m in OBFUSCATED_RE.finditer(unescaped):
        domain = re.sub(r"\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*", ".", m.group(2), flags=re.I)
        e = normalize_email(f"{m.group(1)}@{domain}")
        if EMAIL_RE.fullmatch(e) and _plausible(e) and e not in found:
            unverified[e] = None
    return EmailFindings(list(found), list(unverified))


def is_generic(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    base = re.split(r"[+._-]", local)[0]
    return local in GENERIC_LOCALS or base in GENERIC_LOCALS


def matches_name(email: str, name: str | None) -> bool:
    """Does the local part look like it belongs to this person (jane, jdoe, jane.doe...)?"""
    tokens = name_tokens(name)
    if not tokens:
        return False
    local = fold(email.split("@", 1)[0]).replace(" ", "")
    first, last = tokens[0], tokens[-1]
    if len(first) >= 3 and first in local:
        return True
    if len(tokens) > 1 and len(last) >= 3 and last in local:
        return True
    return len(tokens) > 1 and local in (first[0] + last, first + last[0], first[0] + "." + last)


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


FREEMAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "web.de", "mail.com", "aol.com", "yandex.com",
}
