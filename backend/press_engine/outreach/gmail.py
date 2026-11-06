"""Thin Gmail REST client (send as D, read threads for replies)."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx
from bs4 import BeautifulSoup

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class GmailClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client):
        self.token_provider = token_provider
        self.http = http
        self._me: str | None = None

    def _req(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token_provider()}"}
        try:
            r = self.http.request(method, f"{BASE}{path}", headers=headers, timeout=30, **kw)
        except httpx.HTTPError as e:
            raise GmailError(f"Gmail unreachable: {e}") from e
        if r.status_code >= 400:
            raise GmailError(f"Gmail API {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.content else {}

    def me(self) -> str:
        if self._me is None:
            self._me = self._req("GET", "/profile")["emailAddress"].lower()
        return self._me

    def send(self, msg: EmailMessage, *, thread_id: str | None = None) -> dict:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body: dict = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self._req("POST", "/messages/send", json=body)

    def get_message(self, message_id: str, *, fmt: str = "metadata", headers: list[str] | None = None) -> dict:
        params: list[tuple[str, str]] = [("format", fmt)]
        for h in headers or []:
            params.append(("metadataHeaders", h))
        return self._req("GET", f"/messages/{message_id}", params=params)

    def list_messages(self, query: str, *, max_results: int = 100, pages: int = 3) -> list[dict]:
        out: list[dict] = []
        token = None
        for _ in range(pages):
            params = {"q": query, "maxResults": max_results}
            if token:
                params["pageToken"] = token
            data = self._req("GET", "/messages", params=params)
            out += data.get("messages", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def get_thread(self, thread_id: str, *, fmt: str = "full") -> dict:
        return self._req("GET", f"/threads/{thread_id}", params={"format": fmt})


def build_message(*, from_name: str, from_addr: str, to: str, subject: str, body: str,
                  in_reply_to: str | None = None, references: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    return msg


def header(message: dict, name: str) -> str | None:
    for h in (message.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def plain_text(payload: dict) -> str:
    """Best plain-text body of a Gmail message payload."""
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        return BeautifulSoup("\n".join(html), "lxml").get_text("\n")
    return ""


_QUOTE_HEADER = re.compile(
    r"^(On .+wrote:|Den .+skrev.+:|Am .+schrieb.+:|Op .+schreef.+:|Le .+a écrit.*:|El .+escribió.*:|"
    r"-{2,} ?Original Message ?-{2,}|From: .+)$",
    re.I | re.M,
)


def strip_quoted(text: str) -> str:
    """Drop the quoted history below a reply."""
    m = _QUOTE_HEADER.search(text)
    if m:
        text = text[: m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()
