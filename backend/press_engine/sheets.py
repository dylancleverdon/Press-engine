"""Thin Google Sheets REST client (values + tab setup)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

import httpx

API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(RuntimeError):
    pass


def sheet_id_from(value: str) -> str:
    """Accept a full Google Sheets URL or a bare ID."""
    value = value.strip()
    if "/d/" in value:
        value = value.split("/d/", 1)[1].split("/", 1)[0]
    return value


class SheetsClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client):
        self.token_provider = token_provider
        self.http = http

    def _req(self, method: str, url: str, **kw) -> dict:
        try:
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token_provider()}"}, timeout=30, **kw)
        except httpx.HTTPError as e:
            raise SheetsError(f"Google Sheets unreachable: {e}") from e
        if r.status_code == 404:
            raise SheetsError("Spreadsheet not found. Check the sheet link on the Connections page.")
        if r.status_code == 403:
            raise SheetsError("No access to the spreadsheet. Reconnect Google on the Connections page.")
        if r.status_code >= 400:
            raise SheetsError(f"Google Sheets {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def tabs(self, sid: str) -> dict[str, int]:
        data = self._req("GET", f"{API}/{sid}", params={"fields": "sheets.properties(sheetId,title)"})
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in data.get("sheets", [])}

    def batch_update(self, sid: str, requests: list[dict]) -> dict:
        return self._req("POST", f"{API}/{sid}:batchUpdate", json={"requests": requests})

    def get(self, sid: str, rng: str) -> list[list[str]]:
        data = self._req("GET", f"{API}/{sid}/values/{quote(rng)}")
        return data.get("values", [])

    def update(self, sid: str, rng: str, rows: list[list]) -> None:
        self._req("PUT", f"{API}/{sid}/values/{quote(rng)}", params={"valueInputOption": "RAW"}, json={"values": rows})

    def append(self, sid: str, rng: str, rows: list[list]) -> None:
        self._req("POST", f"{API}/{sid}/values/{quote(rng)}:append",
                  params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json={"values": rows})

    def clear(self, sid: str, rng: str) -> None:
        self._req("POST", f"{API}/{sid}/values/{quote(rng)}:clear", json={})

    def ensure_tab(self, sid: str, title: str, header: list[str], *, dropdown: tuple[int, list[str]] | None = None) -> bool:
        """Create the tab with a bold, frozen header (and optionally a status dropdown).
        Returns True if it was created."""
        existing = self.tabs(sid)
        if title in existing:
            return False
        reply = self.batch_update(sid, [{"addSheet": {"properties": {"title": title, "gridProperties": {"frozenRowCount": 1}}}}])
        tab_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
        self.update(sid, f"{title}!A1", [header])
        requests = [{
            "repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }]
        if dropdown:
            col, values = dropdown
            requests.append({
                "setDataValidation": {
                    "range": {"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
                    "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                             "showCustomUi": True, "strict": False},
                }
            })
        self.batch_update(sid, requests)
        return True


DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


class DriveClient:
    """Uploads suggestion screenshots. With the drive.file permission it can only see
    and change files Press Engine created itself."""

    def __init__(self, token_provider: Callable[[], str], http: httpx.Client):
        self.token_provider = token_provider
        self.http = http

    def _req(self, method: str, url: str, **kw) -> dict:
        try:
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token_provider()}"}, timeout=60, **kw)
        except httpx.HTTPError as e:
            raise SheetsError(f"Google Drive unreachable: {e}") from e
        if r.status_code >= 400:
            raise SheetsError(f"Google Drive {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def folder(self, name: str) -> str:
        q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        found = self._req("GET", DRIVE_API, params={"q": q, "fields": "files(id)", "spaces": "drive"}).get("files", [])
        if found:
            return found[0]["id"]
        return self._req("POST", DRIVE_API, params={"fields": "id"},
                         json={"name": name, "mimeType": "application/vnd.google-apps.folder"})["id"]

    def upload(self, folder_id: str, name: str, data: bytes, mime: str) -> str:
        """Upload a file and return its link."""
        import json as _json
        import uuid

        boundary = f"press-engine-{uuid.uuid4().hex}"
        meta = _json.dumps({"name": name, "parents": [folder_id]}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta
                + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode() + data + f"\r\n--{boundary}--".encode())
        reply = self._upload(body, boundary)
        return reply.get("webViewLink") or f"https://drive.google.com/file/d/{reply['id']}/view"

    def _upload(self, body: bytes, boundary: str) -> dict:
        try:
            r = self.http.post(DRIVE_UPLOAD, params={"uploadType": "multipart", "fields": "id,webViewLink"}, content=body,
                               headers={"Authorization": f"Bearer {self.token_provider()}",
                                        "Content-Type": f"multipart/related; boundary={boundary}"}, timeout=120)
        except httpx.HTTPError as e:
            raise SheetsError(f"Google Drive unreachable: {e}") from e
        if r.status_code >= 400:
            raise SheetsError(f"Google Drive {r.status_code}: {r.text[:300]}")
        return r.json()
