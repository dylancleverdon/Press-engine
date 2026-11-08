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
from pathlib import Path
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .appsettings import AppSettings
from .models import Artist, Suggestion, User
from .sheets import SheetsError, sheet_id_from
from .util.timeutil import utcnow

log = logging.getLogger(__name__)

TAB = "Suggestions"
HEADER = ["ID", "Submitted", "From", "Area", "Suggestion", "Status", "Notes", "Adjustments", "Updated", "Details", "Images"]
LAST_COL = "K"
DRIVE_FOLDER = "Press Engine suggestions"
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/heic": ".heic"}
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 15 * 1024 * 1024
STATUSES = ["new", "accepted", "in progress", "shipped", "rejected", "needs info", "duplicate"]
AREAS = ["Discovery report", "Soundalikes", "Finding press", "Creators", "Pitching", "Replies", "Mac app", "Phone app", "Chrome extension", "Other"]
SOUNDALIKE_TAB = "Soundalikes"
SOUNDALIKE_HEADER = ["Artist", "Kind", "Review", "Similarity", "Contacts found", "Pitched", "Replied", "Reply rate",
                     "Covered", "Fit", "Why", "Updated"]


def submit(session: Session, user: User | None, text: str, area: str | None = None, *, details: str | None = None,
           images: list[tuple[str, bytes, str]] | None = None, image_dir: Path | None = None) -> Suggestion:
    """images: (original file name, bytes, content type), stored under image_dir."""
    s = Suggestion(text=text.strip(), area=(area or None), submitted_by_user_id=user.id if user else None, dirty=True,
                   details=details)
    session.add(s)
    session.flush()
    if images and image_dir is not None:
        add_images(s, images, image_dir)
    return s


def image_dir_for(cfg) -> Path:
    return Path(cfg.data_dir) / "suggestion_images"


def add_images(s: Suggestion, images: list[tuple[str, bytes, str]], image_dir: Path) -> int:
    """Keep the screenshots that are real images of a sane size. Returns how many were kept."""
    image_dir.mkdir(parents=True, exist_ok=True)
    kept = list(s.images or [])
    for name, data, ctype in images:
        ext = IMAGE_TYPES.get((ctype or "").split(";")[0].strip().lower())
        if not ext or not data or len(data) > MAX_IMAGE_BYTES or len(kept) >= MAX_IMAGES:
            continue
        stored = f"{s.id}_{len(kept) + 1}{ext}"
        (image_dir / stored).write_bytes(data)
        kept.append({"file": stored, "name": (name or stored)[:120], "type": ctype, "drive_url": None})
    added = len(kept) - len(s.images or [])
    s.images = kept
    return added


def upload_images(session: Session, drive, image_dir: Path) -> int:
    """Put screenshots not yet in Drive into the "Press Engine suggestions" folder."""
    todo = [s for s in session.scalars(select(Suggestion).where(Suggestion.images.is_not(None)))
            if any(not im.get("drive_url") for im in s.images or [])]
    if not todo:
        return 0
    folder = drive.folder(DRIVE_FOLDER)
    n = 0
    for s in todo:
        images = [dict(im) for im in s.images]
        for im in images:
            path = image_dir / im["file"]
            if im.get("drive_url") or not path.exists():
                continue
            im["drive_url"] = drive.upload(folder, f"Suggestion {s.id} - {im['name']}", path.read_bytes(),
                                           im.get("type") or "image/png")
            n += 1
        s.images = images  # reassign so the JSON change is saved
        s.dirty = True
    session.flush()
    return n


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


def _images_cell(s: Suggestion) -> str:
    return "\n".join(im.get("drive_url") or f"{im['name']} (in the app)" for im in s.images or [])


def _row(s: Suggestion) -> list[str]:
    return [str(s.id), s.created_at.strftime("%Y-%m-%d %H:%M"), s.submitted_by.name if s.submitted_by else "",
            s.area or "", s.text, s.status, s.notes or "", s.adjustments or "", s.updated_at.strftime("%Y-%m-%d %H:%M"),
            (s.details or "")[:45000], _images_cell(s)]


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
        drive = svc.drive()
        if drive is not None:
            stats["images_uploaded"] = upload_images(session, drive, image_dir_for(svc.cfg))
        client.ensure_tab(sid, TAB, HEADER, dropdown=(HEADER.index("Status"), STATUSES))
        rows = client.get(sid, f"{TAB}!A1:{LAST_COL}")
        if not rows or rows[0][:len(HEADER)] != HEADER:  # older sheets lack the newer columns
            client.update(sid, f"{TAB}!A1:{LAST_COL}1", [HEADER])
        rows = rows[1:]
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
                client.append(sid, f"{TAB}!A:{LAST_COL}", [_row(s)])
                stats["pushed"] += 1
            else:
                rownum, row = found
                if s.dirty:
                    client.update(sid, f"{TAB}!A{rownum}:{LAST_COL}{rownum}", [_row(s)])
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
            client.update(sid, f"{TAB}!A{rownum}:{LAST_COL}{rownum}", [_row(s)[:2] + [_cell(row, 2) or "sheet"] + _row(s)[3:]])
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
