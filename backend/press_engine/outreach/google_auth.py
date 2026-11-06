"""Google OAuth for Gmail + Calendar (installed-app flow, token stored on disk)."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


class GoogleAuthError(RuntimeError):
    pass


class GoogleAuth:
    def __init__(self, client_secrets: Path, token_file: Path):
        self.client_secrets = Path(client_secrets)
        self.token_file = Path(token_file)
        self._creds = None
        self._lock = threading.Lock()

    def connected(self) -> bool:
        return self.token_file.exists()

    def _load(self):
        from google.oauth2.credentials import Credentials

        if self._creds is None:
            if not self.token_file.exists():
                raise GoogleAuthError("Google is not connected. Run: press-engine google-auth")
            self._creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        return self._creds

    def access_token(self) -> str:
        """A valid access token, refreshing (and persisting) it when needed."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        with self._lock:
            creds = self._load()
            if not creds.valid:
                if not creds.refresh_token:
                    raise GoogleAuthError("Google token has no refresh token; run press-engine google-auth again")
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    raise GoogleAuthError(
                        f"Google refused the refresh token ({e}). If your OAuth app is in 'Testing' mode, "
                        "tokens expire after 7 days; see README > Google setup."
                    ) from e
                self.token_file.write_text(creds.to_json())
            return creds.token

    def run_flow(self, port: int = 0) -> str:
        """Interactive consent in the browser. Returns the connected account's email if known."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.client_secrets.exists():
            raise GoogleAuthError(
                f"OAuth client file not found at {self.client_secrets}. Download it from Google Cloud Console "
                "(APIs & Services > Credentials > OAuth client ID > Desktop app)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), SCOPES)
        creds = flow.run_local_server(port=port, prompt="consent", access_type="offline")
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(creds.to_json())
        self._creds = creds
        try:
            return json.loads(creds.to_json()).get("account", "") or ""
        except ValueError:
            return ""

    def disconnect(self) -> None:
        if self.token_file.exists():
            self.token_file.unlink()
        self._creds = None
