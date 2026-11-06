"""An in-memory stand-in for the Gmail REST API, mounted on the test Router."""

from __future__ import annotations

import base64
import email
import itertools
import re

import httpx

from press_engine.outreach.gmail import BASE

ME = "d@nightswim.band"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


class GmailFake:
    def __init__(self, router):
        self.sent: list[dict] = []
        self.messages: dict[str, dict] = {}
        self._ids = itertools.count(1)
        router.add("GET", BASE + "/profile", lambda r: httpx.Response(200, json={"emailAddress": ME}))
        router.add("POST", BASE + "/messages/send", self._send)
        router.add("GET", BASE + "/messages?", self._list)
        router.add("GET", BASE + "/messages/", self._get)
        router.add("GET", BASE + "/threads/", self._thread)

    # --- helpers for tests ---
    def add_inbound(self, *, thread_id: str, from_addr: str, subject: str, body: str, internal_ms: int,
                    headers: dict | None = None) -> str:
        mid = f"in{next(self._ids)}"
        hdrs = {"From": f"Someone <{from_addr}>", "Subject": subject, "Message-ID": f"<{mid}@mail.test>", **(headers or {})}
        self.messages[mid] = {
            "id": mid, "threadId": thread_id, "internalDate": str(internal_ms), "snippet": body[:100],
            "payload": {"mimeType": "text/plain", "headers": [{"name": k, "value": v} for k, v in hdrs.items()],
                        "body": {"data": _b64(body)}},
            "_from": from_addr,
        }
        return mid

    # --- handlers ---
    def _send(self, req: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(req.content)
        raw = base64.urlsafe_b64decode(payload["raw"])
        parsed = email.message_from_bytes(raw)
        mid = f"out{next(self._ids)}"
        thread = payload.get("threadId") or f"t-{mid}"
        self.sent.append({"id": mid, "threadId": thread, "parsed": parsed, "payload": payload})
        self.messages[mid] = {
            "id": mid, "threadId": thread, "internalDate": "0", "snippet": "",
            "payload": {"mimeType": "text/plain", "headers": [{"name": k, "value": v} for k, v in parsed.items()],
                        "body": {"data": _b64(parsed.get_payload())}},
            "_from": ME,
        }
        return httpx.Response(200, json={"id": mid, "threadId": thread})

    def _list(self, req: httpx.Request) -> httpx.Response:
        q = req.url.params.get("q", "")
        msgs = [m for m in self.messages.values() if m["_from"] != ME]
        if "mailer-daemon" in q:
            msgs = [m for m in msgs if re.match(r"(mailer-daemon|postmaster)@", m["_from"])]
        else:
            m_from = re.search(r"from:\(([^)]*)\)", q)
            if m_from and "-from:me" not in q:
                wanted = {a.strip().lower() for a in m_from.group(1).split(" OR ")}
                msgs = [m for m in msgs if m["_from"].lower() in wanted]
        return httpx.Response(200, json={"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in msgs]})

    def _get(self, req: httpx.Request) -> httpx.Response:
        mid = req.url.path.rsplit("/", 1)[-1]
        m = self.messages.get(mid)
        if not m:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={k: v for k, v in m.items() if not k.startswith("_")})

    def _thread(self, req: httpx.Request) -> httpx.Response:
        tid = req.url.path.rsplit("/", 1)[-1]
        msgs = [{k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages.values() if m["threadId"] == tid]
        msgs.sort(key=lambda m: int(m["internalDate"]))
        return httpx.Response(200, json={"id": tid, "messages": msgs})
