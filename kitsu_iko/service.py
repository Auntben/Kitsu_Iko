"""All Kitsu network access goes through ``KitsuService``.

The UI never imports gazu directly, so ``DemoService`` (in-memory fake) can
stand in for a real server: ``python -m kitsu_iko --demo``.

Every method may be called from worker threads. Each thread gets its own
``KitsuClient`` (requests sessions aren't thread-safe) but all of them share
one token dict, so a token refresh on any thread is seen by all.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

import gazu
from gazu import client as gazu_client

ProgressCb = Callable[[int, int], None] | None

# Extensions the Kitsu review player can display. Other files are still
# accepted as revisions but show up as a download link only.
VIEWABLE_PREVIEW_EXTS = {
    "png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "bmp",
    "mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "mpg", "mpeg",
    "pdf", "glb", "gltf", "obj", "wav", "mp3",
}

ROLE_LABELS = {
    "admin": "Studio manager",
    "manager": "Production manager",
    "supervisor": "Supervisor",
    "user": "Artist",
    "client": "Client",
    "vendor": "Vendor",
}


def active(entities: list[dict]) -> list[dict]:
    """Drop shots/assets deleted in Kitsu. Deleting one in the web UI first
    marks it ``canceled`` (greyed out, restorable) instead of removing it."""
    return [e for e in entities if not e.get("canceled")]


def normalize_host(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    if not host.endswith("/api"):
        host += "/api"
    return host


@dataclass
class PublishFile:
    path: str
    kind: str  # "revision" | "attachment"
    row_id: int


@dataclass
class PublishResult:
    comment_id: str | None = None
    done_row_ids: list[int] = field(default_factory=list)
    preview_ids: dict[int, str] = field(default_factory=dict)


class PublishError(Exception):
    """A publish that failed part-way. ``result`` says what did get posted
    so a retry can add the remaining files to the same comment."""

    def __init__(self, message: str, result: PublishResult):
        super().__init__(message)
        self.result = result


def describe_error(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    low = msg.lower()
    if "certificate" in low or "ssl" in low:
        return f"SSL error ({msg}). If your server uses a self-signed certificate, untick 'Verify SSL' at login."
    if "max retries" in low or "connection" in low:
        return (f"Cannot reach the server: {msg}. Check the URL; if your Kitsu is served "
                "over plain http, type http:// explicitly.")
    if isinstance(exc, gazu.exception.NotAllowedException):
        return "Permission denied by Kitsu (your role may not allow this action)."
    if isinstance(exc, gazu.exception.TooBigFileException):
        return "File too large for the server's upload limit."
    return msg


class KitsuService:
    is_demo = False

    def __init__(self) -> None:
        self.host = ""
        self.ssl_verify = True
        self.user: dict = {}
        self._tokens: dict = {"access_token": None, "refresh_token": None}
        self._local = threading.local()
        self.server_version = ""

    # -- session ----------------------------------------------------------

    def _client(self) -> gazu_client.KitsuClient:
        c = getattr(self._local, "client", None)
        if c is None or c.host != self.host:
            c = gazu_client.KitsuClient(
                self.host,
                ssl_verify=self.ssl_verify,
                use_refresh_token=True,
                tokens=self._tokens,
            )
            self._local.client = c
        return c

    def login(
        self, host: str, email: str, password: str,
        ssl_verify: bool = True, totp: str | None = None,
    ) -> dict:
        self.host = normalize_host(host)
        self.ssl_verify = ssl_verify
        self._local = threading.local()
        if not ssl_verify:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        c = self._client()
        gazu.log_in(email, password, totp=totp or None, client=c)
        # log_in replaces client.tokens with a new dict: re-share it.
        self._tokens = c.tokens
        self._local = threading.local()
        self.user = gazu_client.get_current_user(client=self._client())
        try:
            self.server_version = gazu_client.get_api_version(client=self._client())
        except Exception:
            self.server_version = "?"
        return self.user

    def logout(self) -> None:
        try:
            gazu.log_out(client=self._client())
        except Exception:
            pass
        self.user = {}

    @property
    def role(self) -> str:
        return self.user.get("role", "user")

    @property
    def can_create_tasks(self) -> bool:
        return self.role in ("admin", "manager", "supervisor")

    # -- browsing ---------------------------------------------------------

    def projects(self) -> list[dict]:
        try:
            return gazu.project.all_open_projects(client=self._client())
        except gazu.exception.NotAllowedException:
            return gazu.user.all_open_projects(client=self._client())

    def episodes(self, project: dict) -> list[dict]:
        if project.get("production_type") != "tvshow":
            return []
        return gazu.shot.all_episodes_for_project(project, client=self._client())

    def shots(self, project: dict, episode: dict | None) -> list[dict]:
        c = self._client()
        if episode:
            shots = gazu.shot.all_shots_for_episode(episode, client=c)
            sequences = gazu.shot.all_sequences_for_episode(episode, client=c)
        else:
            shots = gazu.shot.all_shots_for_project(project, client=c)
            sequences = gazu.shot.all_sequences_for_project(project, client=c)
        seq_by_id = {s["id"]: s for s in sequences}
        # With "All episodes" the same SQ/SH names can exist in several
        # episodes: resolve each shot's episode through its sequence.
        ep_by_id = {e["id"]: e["name"] for e in self.episodes(project)} if not episode else {}
        out = []
        for s in active(shots):
            seq = seq_by_id.get(s.get("parent_id"), {})
            ep_name = ((episode or {}).get("name") or s.get("episode_name")
                       or ep_by_id.get(seq.get("parent_id"), ""))
            out.append({
                "id": s["id"],
                "name": s["name"],
                "project_id": s.get("project_id", project["id"]),
                "sequence_name": s.get("sequence_name") or seq.get("name", ""),
                "episode_name": ep_name,
            })
        return out

    def assets(self, project: dict, episode: dict | None) -> list[dict]:
        c = self._client()
        if episode:
            assets = gazu.asset.all_assets_for_episode(episode, client=c)
        else:
            assets = gazu.asset.all_assets_for_project(project, client=c)
        types = {
            t["id"]: t["name"]
            for t in gazu.asset.all_asset_types_for_project(project, client=c)
        }
        return [
            {
                "id": a["id"],
                "name": a["name"],
                "project_id": a.get("project_id", project["id"]),
                "asset_type_name": a.get("asset_type_name")
                or types.get(a.get("entity_type_id"), ""),
                "episode_name": (episode or {}).get("name", ""),
                "sequence_name": "",
            }
            for a in active(assets)
        ]

    def task_types(self, project: dict, kind: str) -> list[dict]:
        c = self._client()
        types = gazu.task.all_task_types_for_project(project, client=c)
        if not types:
            types = gazu.task.all_task_types(client=c)
        want = "Shot" if kind == "shot" else "Asset"
        return [t for t in types if t.get("for_entity", "Asset") == want]

    def statuses(self, project: dict) -> list[dict]:
        """All task statuses of the project (for display)."""
        c = self._client()
        statuses = gazu.task.all_task_statuses_for_project(project, client=c)
        if not statuses:
            statuses = gazu.task.all_task_statuses(client=c)
        return [s for s in statuses if not s.get("for_concept") and not s.get("archived")]

    def allowed_statuses(self, statuses: list[dict]) -> list[dict]:
        """The subset the logged-in user may set (artists can't 'Done' etc.)."""
        if self.role in ("user", "vendor"):
            statuses = [s for s in statuses if s.get("is_artist_allowed", True)]
        elif self.role == "client":
            statuses = [s for s in statuses if s.get("is_client_allowed", False)]
        return statuses

    def default_status_id(self, statuses: list[dict]) -> str | None:
        for s in statuses:
            if s.get("short_name", "").lower() == "wfa":
                return s["id"]
        return statuses[0]["id"] if statuses else None

    def tasks_by_entity(self, project: dict, task_type: dict, episode: dict | None) -> dict[str, dict]:
        # Deliberately not filtered by episode: callers look tasks up by
        # entity id over an entity list that is already episode-scoped, and
        # asset tasks don't belong to an episode.
        tasks = gazu.task.all_tasks_for_task_type(project, task_type, client=self._client())
        return {t["entity_id"]: t for t in tasks}

    def create_task(self, entity: dict, task_type: dict) -> dict:
        return gazu.task.new_task(entity, task_type, client=self._client())

    # -- upload -----------------------------------------------------------

    def publish(
        self,
        task_id: str,
        status_id: str,
        text: str,
        files: list[PublishFile],
        existing_comment_id: str | None = None,
        existing_preview_id: str | None = None,
        revision: int | None = None,
        set_thumbnail: bool = False,
        on_progress: Callable[[int, int, int], None] | None = None,
        on_file_done: Callable[[int, str | None], None] | None = None,
        on_comment_created: Callable[[str], None] | None = None,
    ) -> PublishResult:
        """Post one comment carrying several files; resumable.

        If ``existing_comment_id`` is set (a retry after a partial failure),
        no new comment is created: the remaining files are added to it, and
        remaining revisions join ``existing_preview_id``'s revision.
        ``on_progress(row_id, sent, total)``; ``on_file_done(row_id, comment_id)``;
        ``on_comment_created(comment_id)`` fires right after a new comment is
        posted, before any preview upload (so undo can find it even if the
        rest fails).
        """
        res = PublishResult(comment_id=existing_comment_id)
        try:
            self._publish(task_id, status_id, text, files, res, existing_preview_id,
                          revision, set_thumbnail, on_progress, on_file_done, on_comment_created)
        except Exception as exc:
            raise PublishError(describe_error(exc), res) from exc
        return res

    def _publish(self, task_id, status_id, text, files, res, existing_preview_id,
                 revision, set_thumbnail, on_progress, on_file_done, on_comment_created) -> None:
        c = self._client()
        task = {"id": task_id}
        attachments = [f for f in files if f.kind == "attachment"]
        previews = [f for f in files if f.kind == "revision"]

        def prog(row_id):
            if on_progress is None:
                return None
            return lambda sent, total: on_progress(row_id, sent, total)

        if res.comment_id is None:
            if attachments:
                # Upload attachments with the comment itself so a single
                # request either posts everything or nothing.
                first = attachments[0].row_id
                comment = gazu.task.add_comment(
                    task, {"id": status_id}, text,
                    attachments=[f.path for f in attachments],
                    client=c, progress_callback=prog(first),
                )
            else:
                comment = gazu.task.add_comment(task, {"id": status_id}, text, client=c)
            res.comment_id = comment["id"]
            if on_comment_created:
                on_comment_created(res.comment_id)
            for f in attachments:
                res.done_row_ids.append(f.row_id)
                if on_file_done:
                    on_file_done(f.row_id, res.comment_id)
        else:
            for f in attachments:
                gazu.task.add_attachment_files_to_comment(
                    task, {"id": res.comment_id}, [f.path],
                    client=c, progress_callback=prog(f.row_id),
                )
                res.done_row_ids.append(f.row_id)
                if on_file_done:
                    on_file_done(f.row_id, res.comment_id)

        comment_ref = {"id": res.comment_id}
        first_preview: dict | None = {"id": existing_preview_id} if existing_preview_id else None
        for f in previews:
            if first_preview is None:
                pf = gazu.task.create_preview(task, comment_ref, revision=revision, client=c)
            else:
                pf = gazu.task.create_extra_preview(task, comment_ref, first_preview, client=c)
            pf = gazu.task.upload_preview_file(pf, f.path, client=c, progress_callback=prog(f.row_id))
            if first_preview is None:
                first_preview = pf
            res.preview_ids[f.row_id] = pf["id"]
            res.done_row_ids.append(f.row_id)
            if on_file_done:
                on_file_done(f.row_id, res.comment_id)
        if set_thumbnail and first_preview is not None:
            try:
                gazu.task.set_main_preview(first_preview, client=c)
            except Exception:
                pass  # thumbnail is cosmetic; don't fail the upload over it

    # -- undo -------------------------------------------------------------

    def task(self, task_id: str) -> dict:
        return gazu.task.get_task(task_id, client=self._client())

    def entity_preview_id(self, entity_id: str) -> str | None:
        return gazu.entity.get_entity(entity_id, client=self._client()).get("preview_file_id")

    def delete_comment(self, comment_id: str) -> None:
        """Kitsu deletes the comment's previews and attachments with it."""
        gazu.task.remove_comment({"id": comment_id}, client=self._client())

    def set_task_status(self, task_id: str, status_id: str) -> None:
        """Set the status directly, without posting a comment."""
        gazu_client.put(f"data/tasks/{task_id}", {"task_status_id": status_id}, client=self._client())

    def set_main_preview(self, preview_id: str) -> None:
        gazu.task.set_main_preview({"id": preview_id}, client=self._client())

    # -- download ---------------------------------------------------------

    def comments(self, task_id: str) -> list[dict]:
        return gazu.task.all_comments_for_task({"id": task_id}, client=self._client())

    def preview_file(self, preview_id: str) -> dict:
        return gazu.files.get_preview_file(preview_id, client=self._client())

    def download_preview(self, preview: dict, path: str, progress: ProgressCb = None) -> None:
        gazu.files.download_preview_file(preview, path, client=self._client(), progress_callback=progress)

    def download_attachment(self, attachment: dict, path: str, progress: ProgressCb = None) -> None:
        gazu.files.download_attachment_file(attachment, path, client=self._client(), progress_callback=progress)

    def web_url(self, path: str = "") -> str:
        return self.host[: -len("/api")] + path
