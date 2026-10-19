"""Plain data for the upload/download queues plus the upload planner.

No Qt here: the planner decides how rows become Kitsu comments and what
to warn about, and is unit-tested on its own.
"""

from __future__ import annotations

import os
import string
from collections import defaultdict
from dataclasses import dataclass, field

from .matcher import Candidate, parse_version
from .service import VIEWABLE_PREVIEW_EXTS

REVISION = "revision"
ATTACHMENT = "attachment"

# Upload row states
PENDING, QUEUED, RUNNING, DONE, ERROR = "pending", "queued", "running", "done", "error"


@dataclass
class UploadDefaults:
    status_id: str | None = None
    mode: str = REVISION
    comment: str = ""
    combine: bool = True
    revision_from_filename: bool = False
    set_thumbnail: bool = False
    create_missing_tasks: bool = False


@dataclass
class UploadItem:
    row_id: int
    path: str
    size: int = 0
    entity_id: str | None = None
    manual: bool = False  # user picked the entity: never auto-remapped
    match_reason: str = ""
    match_score: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    include: bool = True
    # None means "use the default from the top bar" (bulk with exceptions)
    mode: str | None = None
    status_id: str | None = None
    comment: str | None = None
    state: str = PENDING
    progress: float = 0.0
    message: str = ""
    comment_id: str | None = None
    preview_id: str | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ext(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def version(self) -> int | None:
        return parse_version(self.name)

    def eff_mode(self, d: UploadDefaults) -> str:
        return self.mode or d.mode

    def eff_status(self, d: UploadDefaults) -> str | None:
        return self.status_id or d.status_id

    def comment_template(self, d: UploadDefaults) -> str:
        return d.comment if self.comment is None else self.comment

    def eff_comment(self, d: UploadDefaults, entity_name: str = "") -> str:
        return render_comment(self.comment_template(d), [self], entity_name)


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_comment(template: str, items: list[UploadItem], entity_name: str = "") -> str:
    """Fill ``{filename}``, ``{version}``, ``{entity}`` placeholders for the
    files of one comment; unknown placeholders are left untouched."""
    if "{" not in template:
        return template
    versions = [i.version for i in items if i.version is not None]
    values = _SafeDict(
        filename=", ".join(i.name for i in items),
        version=f"v{max(versions):03d}" if versions else "",
        entity=entity_name,
    )
    try:
        return string.Formatter().vformat(template, (), values)
    except (ValueError, IndexError):
        return template


@dataclass
class UploadGroup:
    """One Kitsu comment: a task, a status, a text and its files."""

    entity_id: str
    task_id: str | None  # None => task will be created first
    status_id: str
    text: str
    items: list[UploadItem]
    revision: int | None = None
    # On a resumed comment: a preview already posted to it, so remaining
    # revision files join that revision instead of starting a new one.
    preview_id: str | None = None

    @property
    def comment_id(self) -> str | None:
        return next((i.comment_id for i in self.items if i.comment_id), None)

    @property
    def pending_items(self) -> list[UploadItem]:
        return [i for i in self.items if i.state != DONE]


@dataclass
class UploadPlan:
    groups: list[UploadGroup] = field(default_factory=list)
    blocked: dict[int, str] = field(default_factory=dict)  # row_id -> reason
    warnings: list[str] = field(default_factory=list)
    unassigned: int = 0
    excluded: int = 0
    tasks_to_create: int = 0

    @property
    def file_count(self) -> int:
        return sum(len(g.pending_items) for g in self.groups)


def plan_upload(
    items: list[UploadItem],
    d: UploadDefaults,
    tasks_by_entity: dict[str, dict],
    entity_names: dict[str, str],
) -> UploadPlan:
    plan = UploadPlan()
    # Includes rows already DONE: they hold the preview ids of resumed comments.
    preview_by_comment = {i.comment_id: i.preview_id for i in items if i.comment_id and i.preview_id}
    buckets: dict[tuple, list[UploadItem]] = defaultdict(list)
    order: list[tuple] = []
    non_viewable: list[str] = []
    for it in items:
        if it.state == DONE:
            continue
        if not it.include:
            plan.excluded += 1
            continue
        if not it.entity_id:
            plan.unassigned += 1
            continue
        status = it.eff_status(d)
        if not status:
            plan.blocked[it.row_id] = "No status selected"
            continue
        task = tasks_by_entity.get(it.entity_id)
        if task is None and not d.create_missing_tasks:
            plan.blocked[it.row_id] = "Entity has no task of this type"
            continue
        if it.eff_mode(d) == REVISION and it.ext not in VIEWABLE_PREVIEW_EXTS:
            non_viewable.append(it.name)
        if it.comment_id:
            # Leftovers of a partially-posted comment stay with that comment.
            key = ("resume", it.comment_id)
        elif d.combine:
            # Group on the template, not the rendered text, so "{version}"
            # doesn't split one shot's files into several comments.
            key = (it.entity_id, status, it.comment_template(d))
        else:
            key = ("row", it.row_id)
        if key not in buckets:
            order.append(key)
        buckets[key].append(it)

    per_task: dict[str, int] = defaultdict(int)
    for key in order:
        group_items = buckets[key]
        first = group_items[0]
        entity_id = first.entity_id
        task = tasks_by_entity.get(entity_id)
        revision = None
        if d.revision_from_filename:
            versions = {i.version for i in group_items if i.eff_mode(d) == REVISION and i.version is not None}
            if versions:
                revision = max(versions)
                if len(versions) > 1:
                    plan.warnings.append(
                        f"{entity_names.get(entity_id, entity_id)}: files carry different "
                        f"versions {sorted(versions)}; revision v{revision:03d} will be used."
                    )
        plan.groups.append(UploadGroup(
            entity_id=entity_id,
            task_id=task["id"] if task else None,
            status_id=first.eff_status(d),
            text=render_comment(first.comment_template(d), group_items,
                                entity_names.get(entity_id, "")),
            items=group_items,
            revision=revision,
            preview_id=preview_by_comment.get(first.comment_id) if first.comment_id else None,
        ))
        per_task[entity_id] += 1

    plan.tasks_to_create = len({g.entity_id for g in plan.groups if g.task_id is None})
    for entity_id, n in per_task.items():
        if n > 1:
            plan.warnings.append(
                f"{entity_names.get(entity_id, entity_id)} will get {n} separate comments "
                "(different status/comment overrides); the task ends on the last one's status."
            )
    if non_viewable:
        shown = ", ".join(non_viewable[:4]) + (" ..." if len(non_viewable) > 4 else "")
        plan.warnings.append(
            f"{len(non_viewable)} revision file(s) can't be played in Kitsu's review player "
            f"and will only be downloadable: {shown}. Consider sending them as attachments."
        )
    if plan.unassigned:
        plan.warnings.append(f"{plan.unassigned} file(s) in the unassigned pool will be skipped.")
    return plan


def mode_counts(plan: UploadPlan, d: UploadDefaults) -> tuple[int, int, int]:
    """(revisions, attachments, attachments that look like review media).
    The last one flags the likely accident: a video/image sent as attachment."""
    pend = [i for g in plan.groups for i in g.pending_items]
    att = [i for i in pend if i.eff_mode(d) == ATTACHMENT]
    media = [i for i in att if i.ext in VIEWABLE_PREVIEW_EXTS and i.ext != "pdf"]
    return len(pend) - len(att), len(att), len(media)


# -- download ------------------------------------------------------------


@dataclass
class DownloadChoice:
    """One selectable version for an entity: a revision (its preview files)
    or one comment's attachments."""

    label: str
    kind: str  # REVISION | ATTACHMENT
    files: list[dict]
    revision: int | None = None
    created_at: str = ""
    author: str = ""
    status_name: str = ""


@dataclass
class DownloadItem:
    row_id: int
    entity: dict
    task: dict
    choices: list[DownloadChoice]
    choice_index: int = 0
    include: bool = True
    state: str = PENDING
    progress: float = 0.0
    message: str = ""

    @property
    def choice(self) -> DownloadChoice | None:
        if 0 <= self.choice_index < len(self.choices):
            return self.choices[self.choice_index]
        return None


def _author(comment: dict) -> str:
    p = comment.get("person") or {}
    return (p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}").strip()


def build_choices(comments: list[dict], kind: str) -> list[DownloadChoice]:
    """Newest first. Revisions are grouped by revision number (a revision can
    hold several preview files); attachments are grouped per comment."""
    choices: list[DownloadChoice] = []
    comments = sorted(comments, key=lambda c: c.get("created_at") or "", reverse=True)
    if kind == REVISION:
        by_rev: dict[int, DownloadChoice] = {}
        for c in comments:
            for p in c.get("previews") or []:
                rev = p.get("revision") or 0
                ch = by_rev.get(rev)
                if ch is None:
                    ch = by_rev[rev] = DownloadChoice(
                        label="", kind=REVISION, files=[], revision=rev,
                        created_at=(c.get("created_at") or "")[:16].replace("T", " "),
                        author=_author(c),
                        status_name=(c.get("task_status") or {}).get("short_name", ""),
                    )
                ch.files.append(p)
        for rev in sorted(by_rev, reverse=True):
            ch = by_rev[rev]
            extra = f" ({len(ch.files)} files)" if len(ch.files) > 1 else ""
            ch.label = f"v{rev:03d}{extra} - {ch.created_at} - {ch.author}"
            choices.append(ch)
    else:
        for c in comments:
            files = c.get("attachment_files") or []
            if not files:
                continue
            when = (c.get("created_at") or "")[:16].replace("T", " ")
            names = ", ".join(f.get("name", "?") for f in files[:2])
            more = f" +{len(files) - 2}" if len(files) > 2 else ""
            choices.append(DownloadChoice(
                label=f"{when} - {names}{more}", kind=ATTACHMENT, files=list(files),
                created_at=when, author=_author(c),
                status_name=(c.get("task_status") or {}).get("short_name", ""),
            ))
    return choices


_ILLEGAL = '<>:"/\\|?*'


def safe_part(text: str) -> str:
    text = "".join("_" if ch in _ILLEGAL or ord(ch) < 32 else ch for ch in str(text))
    return text.strip().rstrip(".") or "_"


def render_path(template: str, values: dict) -> str:
    """Render a ``/``-separated path template; each segment is sanitised
    and empty segments (e.g. no episode) are dropped."""
    safe = _SafeDict({k: v for k, v in values.items()})
    parts = []
    for seg in template.replace("\\", "/").split("/"):
        try:
            rendered = string.Formatter().vformat(seg, (), safe)
        except (ValueError, IndexError, KeyError):
            rendered = seg
        rendered = rendered.strip()
        if rendered:
            parts.append(safe_part(rendered))
    return os.path.join(*parts) if parts else "_"
