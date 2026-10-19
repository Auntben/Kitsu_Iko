"""Upload journal and undo.

Every comment the tool posts is recorded the moment it is created, with
what's needed to reverse it: the task's status beforehand and, if the upload
set the thumbnail, the previous thumbnail. Records are keyed by comment id
(a retry that adds files to an earlier comment extends that record), grouped
into batches (one per click of Upload), and written to disk atomically after
every change, so undo survives a crash or restart.

Undo deletes the comments (Kitsu removes their revisions and attachments with
them), then puts status and thumbnail back. Tasks created by the upload are
never deleted.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

MAX_BATCHES = 50


def default_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "KitsuIko", "upload_history.json")


@dataclass
class CommentRecord:
    comment_id: str
    task_id: str
    entity_label: str
    prev_status_id: str | None
    new_status_id: str
    files: list[str] = field(default_factory=list)
    created_at: str = ""
    thumb_set: bool = False
    thumb_prev_preview_id: str | None = None
    task_created: bool = False
    undone: bool = False
    undo_note: str = ""


@dataclass
class Batch:
    id: str
    created_at: str
    context: str
    comment_ids: list[str] = field(default_factory=list)


class UploadJournal:
    """Per server+user history. ``path=None`` keeps it in memory (demo)."""

    def __init__(self, path: str | None, scope: str):
        self.path = path
        self.scope = scope
        self.batches: list[Batch] = []
        self.records: dict[str, CommentRecord] = {}
        self._all: dict = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                self._all = json.load(fh)
        except (OSError, ValueError):
            self._all = {}  # unreadable journal: start fresh rather than crash
            return
        mine = self._all.get(self.scope, {})
        self.batches = [Batch(**b) for b in mine.get("batches", [])]
        self.records = {k: CommentRecord(**r) for k, r in mine.get("records", {}).items()}

    def save(self) -> None:
        self.batches = self.batches[-MAX_BATCHES:]
        keep = {cid for b in self.batches for cid in b.comment_ids}
        self.records = {k: r for k, r in self.records.items() if k in keep}
        if not self.path:
            return
        self._all[self.scope] = {
            "batches": [asdict(b) for b in self.batches],
            "records": {k: asdict(r) for k, r in self.records.items()},
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._all, fh, indent=1)
        os.replace(tmp, self.path)

    # -- recording ---------------------------------------------------------

    def new_batch(self, context: str) -> str:
        b = Batch(id=uuid.uuid4().hex[:12], created_at=datetime.now().isoformat(timespec="seconds"),
                  context=context)
        self.batches.append(b)
        return b.id

    def record(self, batch_id: str, rec: CommentRecord) -> None:
        """Record a newly created comment, or extend an existing record
        (retry adding files to a comment posted by an earlier batch)."""
        existing = self.records.get(rec.comment_id)
        if existing is not None:
            for f in rec.files:
                if f not in existing.files:
                    existing.files.append(f)
        else:
            rec.created_at = rec.created_at or datetime.now().isoformat(timespec="seconds")
            self.records[rec.comment_id] = rec
            batch = self.batch(batch_id)
            if batch is not None and rec.comment_id not in batch.comment_ids:
                batch.comment_ids.append(rec.comment_id)
        self.save()

    def batch(self, batch_id: str) -> Batch | None:
        return next((b for b in self.batches if b.id == batch_id), None)

    def recent(self) -> list[Batch]:
        """Newest first, skipping batches where nothing got posted."""
        return [b for b in reversed(self.batches) if b.comment_ids]

    def batch_records(self, batch: Batch) -> list[CommentRecord]:
        return [self.records[c] for c in batch.comment_ids if c in self.records]

    def pending_undo(self, batch: Batch) -> list[CommentRecord]:
        return [r for r in self.batch_records(batch) if not r.undone]

    def mark_undone(self, comment_id: str, note: str) -> None:
        rec = self.records.get(comment_id)
        if rec:
            rec.undone, rec.undo_note = True, note
            self.save()


# --------------------------------------------------------------------------
# Undo logic (service passed in, so it runs against the demo or a real server)
# --------------------------------------------------------------------------


def _newest_first(comments: list[dict]) -> list[dict]:
    return sorted(comments, key=lambda c: c.get("created_at") or "", reverse=True)


@dataclass
class UndoCheck:
    comment_id: str
    exists: bool
    newer_foreign: int  # comments by anyone posted after ours (not in this undo)
    replies: int


def inspect(svc, records: list[CommentRecord]) -> list[UndoCheck]:
    """Look at the server before deleting anything, so the confirmation can
    warn about other people's work that came after ours."""
    ours = {r.comment_id for r in records}
    by_task: dict[str, list[dict]] = {}
    out = []
    for r in records:
        if r.task_id not in by_task:
            by_task[r.task_id] = _newest_first(svc.comments(r.task_id))
        comments = by_task[r.task_id]
        mine = next((c for c in comments if c["id"] == r.comment_id), None)
        if mine is None:
            out.append(UndoCheck(r.comment_id, False, 0, 0))
            continue
        newer = [c for c in comments if (c.get("created_at") or "") > (mine.get("created_at") or "")
                 and c["id"] not in ours]
        out.append(UndoCheck(r.comment_id, True, len(newer), len(mine.get("replies") or [])))
    return out


def undo_record(svc, rec: CommentRecord, other_ours: set[str], status_name) -> str:
    """Reverse one comment. Returns a short human note; raises on failure.

    ``other_ours``: comment ids still to be undone in the same operation
    (several comments on one task are undone newest first)."""
    comments = _newest_first(svc.comments(rec.task_id))
    if not any(c["id"] == rec.comment_id for c in comments):
        return "already deleted in Kitsu"
    remaining = [c for c in comments if c["id"] != rec.comment_id and c["id"] not in other_ours]
    ours_newest = comments[0]["id"] == rec.comment_id
    svc.delete_comment(rec.comment_id)
    notes = ["deleted"]

    if ours_newest and rec.prev_status_id and not other_ours & {c["id"] for c in comments}:
        # Ours was the latest word on the task: put the old status back.
        # Kitsu may already have done so when the comment was deleted.
        current = (svc.task(rec.task_id) or {}).get("task_status_id")
        if current != rec.prev_status_id:
            try:
                svc.set_task_status(rec.task_id, rec.prev_status_id)
                notes.append(f"status back to {status_name(rec.prev_status_id)}")
            except Exception as exc:
                notes.append(f"status left at {status_name(current)} ({exc})")
        else:
            notes.append(f"status back to {status_name(rec.prev_status_id)}")
    elif not ours_newest and remaining:
        notes.append("status left as is (newer comments exist)")

    if rec.thumb_set and rec.thumb_prev_preview_id:
        try:
            svc.set_main_preview(rec.thumb_prev_preview_id)
            notes.append("thumbnail restored")
        except Exception:
            notes.append("previous thumbnail could not be restored")
    if rec.task_created:
        notes.append("the task this upload created was kept")
    return ", ".join(notes)
