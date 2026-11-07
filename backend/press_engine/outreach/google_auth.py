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
    "https://www.googleapis.com/auth/spreadsheets",  # suggestions box sheet
]
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


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

    def granted_scopes(self) -> set[str]:
        try:
            return set(json.loads(self.token_file.read_text()).get("scopes") or [])
        except (OSError, ValueError):
            return set()

    def has_scope(self, scope: str) -> bool:
        return scope in self.granted_scopes()

    def _load(self):
        from google.oauth2.credentials import Credentials

        if self._creds is None:
            if not self.token_file.exists():
                raise GoogleAuthError("Google is not connected. Run: press-engine google-auth")
            # Use the scopes the user actually granted: asking for new ones on refresh
            # (e.g. after an update added Sheets) makes Google reject the refresh.
            self._creds = Credentials.from_authorized_user_file(str(self.token_file))
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

    def run_flow(self, port: int = 0, timeout_seconds: int | None = None) -> str:
        """Interactive consent in the browser. Returns the connected account's email if known."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.client_secrets.exists():
            raise GoogleAuthError(
                f"OAuth client file not found at {self.client_secrets}. Download it from Google Cloud Console "
                "(APIs & Services > Credentials > OAuth client ID > Desktop app)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), SCOPES)
        try:
            creds = flow.run_local_server(port=port, prompt="consent", access_type="offline", timeout_seconds=timeout_seconds)
        except AttributeError as e:  # no redirect arrived before the timeout
            raise GoogleAuthError("Google sign-in timed out. Try again.") from e
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

    # --- sign-in started from the dashboard (the backend runs on D's own Mac) ---

    flow_state: dict[str, str | None] = {"status": "idle", "error": None}

    def start_browser_flow(self, timeout_seconds: int = 300) -> bool:
        """Open Google's consent page in the default browser and wait in the background."""
        if self.flow_state.get("status") == "waiting":
            return False
        self.flow_state = {"status": "waiting", "error": None}

        def run() -> None:
            try:
                self.run_flow(timeout_seconds=timeout_seconds)
                self.flow_state = {"status": "connected", "error": None}
            except Exception as e:  # report anything to the page
                log.warning("Google sign-in failed: %s", e)
                self.flow_state = {"status": "error", "error": str(e)}

        threading.Thread(target=run, daemon=True, name="google-oauth").start()
        return True

    def save_client_secrets(self, raw: bytes) -> None:
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise GoogleAuthError("That file isn't the JSON you download from Google Cloud Console.") from e
        if "installed" not in data:
            raise GoogleAuthError("Use an OAuth client of type 'Desktop app' (the file should contain \"installed\").")
        self.client_secrets.parent.mkdir(parents=True, exist_ok=True)
        self.client_secrets.write_bytes(raw)
        self.client_secrets.chmod(0o600)
