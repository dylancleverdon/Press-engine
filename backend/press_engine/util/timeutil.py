"""Time helpers. All datetimes stored in the DB are naive UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def system_timezone_name() -> str:
    try:
        from tzlocal import get_localzone_name

        return get_localzone_name() or "UTC"
    except Exception:  # pragma: no cover - platform dependent
        return "UTC"


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    """Naive-UTC datetime -> aware local datetime."""
    return dt.replace(tzinfo=UTC).astimezone(tz)


def to_utc_naive(dt: datetime) -> datetime:
    """Aware datetime -> naive UTC."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def local_today(tz: ZoneInfo, now: datetime | None = None) -> date:
    return to_local(now or utcnow(), tz).date()


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":", 1)
    return time(int(h), int(m))


def local_window_utc(day: date, start: str, end: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the [start, end) window of ``day`` in ``tz`` as naive UTC datetimes."""
    s = datetime.combine(day, parse_hhmm(start), tzinfo=tz)
    e = datetime.combine(day, parse_hhmm(end), tzinfo=tz)
    if e <= s:
        e += timedelta(days=1)
    return to_utc_naive(s), to_utc_naive(e)


# Best-guess time zone per country (ISO 3166 alpha-2) for "recipient" sending
# mode. Countries spanning several zones use their most populous one.
COUNTRY_TZ = {
    "DK": "Europe/Copenhagen", "SE": "Europe/Stockholm", "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki", "IS": "Atlantic/Reykjavik", "DE": "Europe/Berlin",
    "AT": "Europe/Vienna", "CH": "Europe/Zurich", "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels", "LU": "Europe/Luxembourg", "FR": "Europe/Paris",
    "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "IT": "Europe/Rome",
    "GB": "Europe/London", "UK": "Europe/London", "IE": "Europe/Dublin",
    "PL": "Europe/Warsaw", "CZ": "Europe/Prague", "HU": "Europe/Budapest",
    "GR": "Europe/Athens", "EE": "Europe/Tallinn", "LV": "Europe/Riga",
    "LT": "Europe/Vilnius", "US": "America/New_York", "CA": "America/Toronto",
    "MX": "America/Mexico_City", "BR": "America/Sao_Paulo", "AR": "America/Argentina/Buenos_Aires",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "CN": "Asia/Shanghai", "TW": "Asia/Taipei",
    "HK": "Asia/Hong_Kong", "SG": "Asia/Singapore", "IN": "Asia/Kolkata",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "ZA": "Africa/Johannesburg",
}

# Fallback when only a language is known.
LANGUAGE_TZ = {
    "da": "Europe/Copenhagen", "sv": "Europe/Stockholm", "nb": "Europe/Oslo", "no": "Europe/Oslo",
    "fi": "Europe/Helsinki", "de": "Europe/Berlin", "nl": "Europe/Amsterdam", "fr": "Europe/Paris",
    "es": "Europe/Madrid", "it": "Europe/Rome", "pt": "Europe/Lisbon", "pl": "Europe/Warsaw",
    "ja": "Asia/Tokyo", "ko": "Asia/Seoul", "zh": "Asia/Shanghai",
}


def guess_recipient_tz(country: str | None, language: str | None, default: str) -> str:
    if country and country.upper() in COUNTRY_TZ:
        return COUNTRY_TZ[country.upper()]
    if language and language.lower()[:2] in LANGUAGE_TZ:
        return LANGUAGE_TZ[language.lower()[:2]]
    return default
