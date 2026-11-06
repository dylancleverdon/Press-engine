"""Signed session cookies for the dashboard.

The cookie holds the user id and a signature made with a random key kept in the
data folder, so the raw access token never sits in a cookie. Logging out
everywhere = deleting the key file.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

COOKIE = "pe_session"
MAX_AGE = 60 * 60 * 24 * 365


def _key(data_dir: Path) -> bytes:
    path = data_dir / "session.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return bytes.fromhex(path.read_text().strip())


def sign(data_dir: Path, user_id: int, *, now: float | None = None) -> str:
    issued = int(now or time.time())
    payload = f"{user_id}.{issued}"
    mac = hmac.new(_key(data_dir), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def verify(data_dir: Path, value: str | None, *, now: float | None = None) -> int | None:
    if not value or value.count(".") != 2:
        return None
    user_id, issued, mac = value.split(".")
    expected = hmac.new(_key(data_dir), f"{user_id}.{issued}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        if (now or time.time()) - int(issued) > MAX_AGE:
            return None
        return int(user_id)
    except ValueError:
        return None
