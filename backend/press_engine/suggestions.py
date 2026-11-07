"""Suggestions box, mirrored to D's Google Sheet ("Suggestions" tab).

* A suggestion made in the app is saved locally right away and appended to the sheet
  on the next sync (immediately if Google is connected).
* The sheet is where suggestions get reviewed: Status, Notes and Adjustments edited
  there come back into the app on the next sync. Edits made in the app win if both
  changed since the last sync.
* Rows typed straight into the sheet (no ID yet) are imported and given an ID.
* A "Soundalikes" tab is rewritten on each sync with how every soundalike performs.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .appsettings import AppSettings
from .models import Artist, Suggestion, User
from .sheets import SheetsError, sheet_id_from
from .util.timeutil import utcnow

log = logging.getLogger(__name__)

TAB = "Suggestions"
HEADER = ["ID", "Submitted", "From", "Area", "Suggestion", "Status", "Notes", "Adjustments", "Updated"]
STATUSES = ["new", "accepted", "in progress", "shipped", "rejected", "needs info", "duplicate"]
AREAS = ["Soundalikes", "Finding press", "Creators", "Pitching", "Replies", "Mac app", "Phone app", "Chrome extension", "Other"]
SOUNDALIKE_TAB = "Soundalikes"
SOUNDALIKE_HEADER = ["Artist", "Kind", "Review", "Similarity", "Contacts found", "Pitched", "Replied", "Reply rate",
                     "Covered", "Fit", "Why", "Updated"]


def submit(session: Session, user: User | None, text: str, area: str | None = None) -> Suggestion:
    s = Suggestion(text=text.strip(), area=(area or None), submitted_by_user_id=user.id if user else None, dirty=True)
    session.add(s)
    session.flush()
    return s


def edit(session: Session, s: Suggestion, *, status: str | None = None, notes: str | None = None,
         adjustments: str | None = None, now: datetime | None = None) -> None:
    if status is not None:
        s.status = status.strip() or "new"
    if notes is not None:
        s.notes = notes.strip() or None
    if adjustments is not None:
        s.adjustments = adjustments.strip() or None
    s.updated_at = now or utcnow()
    s.dirty = True
    session.flush()


def _row(s: Suggestion) -> list[str]:
    return [str(s.id), s.created_at.strftime("%Y-%m-%d %H:%M"), s.submitted_by.name if s.submitted_by else "",
            s.area or "", s.text, s.status, s.notes or "", s.adjustments or "", s.updated_at.strftime("%Y-%m-%d %H:%M")]


def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) and row[i] is not None else ""


def sync(session: Session, svc, settings: AppSettings, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    sid = sheet_id_from(settings.suggestions_sheet_id or "")
    if not sid:
        return {"skipped": "no spreadsheet set (Connections page)"}
    client = svc.sheets()
    if client is None:
        return {"skipped": "Google isn't connected with access to Sheets (reconnect on the Connections page)"}
    stats = {"pushed": 0, "updated_in_sheet": 0, "pulled": 0, "imported": 0}
    try:
        client.ensure_tab(sid, TAB, HEADER, dropdown=(HEADER.index("Status"), STATUSES))
        rows = client.get(sid, f"{TAB}!A2:I")
        by_id: dict[str, tuple[int, list[str]]] = {}
        new_rows: list[tuple[int, list[str]]] = []
        for i, row in enumerate(rows):
            rownum = i + 2
            if not any(c.strip() for c in row):
                continue
            ident = _cell(row, 0)
            if ident.isdigit():
                by_id[ident] = (rownum, row)
            elif _cell(row, 4):
                new_rows.append((rownum, row))

        for s in session.scalars(select(Suggestion).order_by(Suggestion.id)):
            found = by_id.get(str(s.id))
            if found is None:
                client.append(sid, f"{TAB}!A:I", [_row(s)])
                stats["pushed"] += 1
            else:
                rownum, row = found
                if s.dirty:
                    client.update(sid, f"{TAB}!A{rownum}:I{rownum}", [_row(s)])
                    stats["updated_in_sheet"] += 1
                else:
                    status, notes, adj, text = _cell(row, 5) or "new", _cell(row, 6) or None, _cell(row, 7) or None, _cell(row, 4)
                    if (status, notes, adj) != (s.status, s.notes, s.adjustments) or (text and text != s.text):
                        s.status, s.notes, s.adjustments = status, notes, adj
                        s.text = text or s.text
                        s.updated_at = now
                        stats["pulled"] += 1
            s.dirty = False
            s.synced_at = now

        for rownum, row in new_rows:  # typed straight into the sheet
            s = Suggestion(text=_cell(row, 4), area=_cell(row, 3) or None, status=_cell(row, 5) or "new",
                           notes=_cell(row, 6) or None, adjustments=_cell(row, 7) or None, dirty=False, synced_at=now)
            session.add(s)
            session.flush()
            client.update(sid, f"{TAB}!A{rownum}:I{rownum}", [_row(s)[:2] + [_cell(row, 2) or "sheet"] + _row(s)[3:]])
            stats["imported"] += 1

        stats["soundalikes"] = write_soundalike_report(client, sid, session, now)
    except SheetsError as e:
        session.flush()
        return {**stats, "error": str(e)}
    session.flush()
    return stats


def write_soundalike_report(client, sid: str, session: Session, now: datetime) -> int:
    client.ensure_tab(sid, SOUNDALIKE_TAB, SOUNDALIKE_HEADER)
    artists = list(session.scalars(select(Artist).order_by(Artist.source.desc(), Artist.similarity.desc())))
    rows = []
    for a in artists:
        st = a.fit_stats or {}
        rate = st.get("reply_rate")
        rows.append([a.name, "seed" if a.is_seed else a.source, a.review if a.active else "excluded",
                     round(a.similarity, 2), st.get("contacts", 0), st.get("pitched", 0), st.get("replied", 0),
                     f"{round(rate * 100)}%" if rate is not None else "", st.get("covered", 0), a.fit_verdict,
                     st.get("why", ""), now.strftime("%Y-%m-%d %H:%M")])
    client.clear(sid, f"{SOUNDALIKE_TAB}!A2:L")
    if rows:
        client.update(sid, f"{SOUNDALIKE_TAB}!A2", rows)
    return len(rows)
