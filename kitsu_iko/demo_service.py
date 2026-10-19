"""In-memory stand-in for ``KitsuService`` so the UI can be tried without a
server: ``python -m kitsu_iko --demo``. Uploads and downloads are simulated
with short sleeps; downloads write small placeholder files."""

from __future__ import annotations

import itertools
import os
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

from .service import KitsuService, PublishError, PublishFile, PublishResult, active

_ids = itertools.count(1)


def _id() -> str:
    return f"demo-{next(_ids)}"


class DemoService(KitsuService):
    is_demo = True

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._failed_once: set[str] = set()
        rnd = random.Random(7)
        self.statuses_all = [
            {"id": "st-todo", "name": "Todo", "short_name": "todo", "color": "#5e6a78", "is_artist_allowed": True},
            {"id": "st-wip", "name": "Work In Progress", "short_name": "wip", "color": "#3273dc", "is_artist_allowed": True},
            {"id": "st-wfa", "name": "Waiting For Approval", "short_name": "wfa", "color": "#ab26ff", "is_artist_allowed": True},
            {"id": "st-retake", "name": "Retake", "short_name": "retake", "color": "#ff3860", "is_artist_allowed": False},
            {"id": "st-done", "name": "Done", "short_name": "done", "color": "#22d160", "is_artist_allowed": False},
        ]
        self.task_types_all = [
            {"id": "tt-layout", "name": "Layout", "for_entity": "Shot"},
            {"id": "tt-anim", "name": "Animation", "for_entity": "Shot"},
            {"id": "tt-comp", "name": "Compositing", "for_entity": "Shot"},
            {"id": "tt-concept", "name": "Concept", "for_entity": "Asset"},
            {"id": "tt-model", "name": "Modeling", "for_entity": "Asset"},
            {"id": "tt-shading", "name": "Shading", "for_entity": "Asset"},
        ]
        self.projects_all = [
            {"id": "p-show", "name": "Iko Series", "production_type": "tvshow"},
            {"id": "p-film", "name": "Short Film", "production_type": "short"},
        ]
        self.episodes_all = {"p-show": [{"id": "ep-1", "name": "EP01"}, {"id": "ep-2", "name": "EP02"}]}
        self.shots_all: list[dict] = []
        # EP02 shares SQ010 with EP01 on purpose (same SQ/SH names in two
        # episodes is common and exercises "All episodes"); the rest differs.
        layouts = [
            ("p-show", "EP01", (10, 20, 30), range(10, 70, 10)),
            ("p-show", "EP02", (10, 40, 50), range(10, 50, 10)),
            ("p-film", "", (100, 200), range(10, 90, 10)),
        ]
        for pid, ep, seqs, shot_nums in layouts:
            for sq in seqs:
                for sh in shot_nums:
                        self.shots_all.append({
                            "id": _id(), "project_id": pid, "name": f"SH{sh:03d}",
                            "sequence_name": f"SQ{sq:03d}", "episode_name": ep,
                        })
        # Deleted-but-restorable entities (greyed out in Kitsu): EP01's
        # SQ020/SH060 and the Short Film's Sword prop. The tool must hide them.
        for s in self.shots_all:
            if (s["episode_name"], s["sequence_name"], s["name"]) == ("EP01", "SQ020", "SH060"):
                s["canceled"] = True
        self.assets_all: list[dict] = []
        for pid in ("p-show", "p-film"):
            for atype, names in (
                ("Character", ["Iko", "Hero_Bob", "Grandma"]),
                ("Prop", ["Tree", "TreeA", "Lantern", "Sword"]),
                ("Environment", ["Castle", "Forest"]),
            ):
                for n in names:
                    self.assets_all.append({
                        "id": _id(), "project_id": pid, "name": n,
                        "asset_type_name": atype, "episode_name": "", "sequence_name": "",
                        "canceled": pid == "p-film" and n == "Sword",
                    })
        self.tasks: dict[tuple[str, str], dict] = {}
        self.thumbnails: dict[str, str] = {}
        self.comments_by_task: dict[str, list[dict]] = {}
        now = datetime.now()
        for ent in self.shots_all + self.assets_all:
            for tt in self.task_types_all:
                is_shot = "sequence_name" in ent and ent["sequence_name"]
                if (tt["for_entity"] == "Shot") != bool(is_shot):
                    continue
                if rnd.random() < 0.1:
                    continue  # some entities lack the task, to show the warning
                task = {"id": _id(), "entity_id": ent["id"], "task_type_id": tt["id"],
                        "task_status_id": rnd.choice(self.statuses_all)["id"]}
                self.tasks[(ent["id"], tt["id"])] = task
                comments = []
                for rev in range(1, rnd.randint(0, 4) + 1):
                    when = now - timedelta(days=30 - rev * 5, hours=rnd.randint(0, 20))
                    c = {
                        "id": _id(), "created_at": when.isoformat(timespec="seconds"),
                        "text": f"Revision {rev}", "person": {"first_name": "Demo", "last_name": "Artist"},
                        "task_status": rnd.choice(self.statuses_all),
                        "previews": [{"id": _id(), "revision": rev, "extension": "mp4" if is_shot else "png"}],
                        "attachment_files": [],
                    }
                    if rnd.random() < 0.4:
                        c["attachment_files"].append({"id": _id(), "name": f"notes_v{rev:03d}.pdf", "extension": "pdf", "size": 120000})
                    comments.append(c)
                self.comments_by_task[task["id"]] = list(reversed(comments))

    # -- session ----------------------------------------------------------

    def login(self, host, email, password, ssl_verify=True, totp=None):
        self.host = "https://demo.kitsu.local/api"
        self.user = {"id": "me", "full_name": "Demo User", "email": email or "demo@example.com", "role": "manager"}
        self.server_version = "demo"
        return self.user

    def logout(self):
        self.user = {}

    # -- browsing ---------------------------------------------------------

    def projects(self):
        return list(self.projects_all)

    def episodes(self, project):
        return list(self.episodes_all.get(project["id"], []))

    def shots(self, project, episode):
        return active([s for s in self.shots_all if s["project_id"] == project["id"]
                       and (episode is None or s["episode_name"] == episode["name"])])

    def assets(self, project, episode):
        return active([dict(a, episode_name=(episode or {}).get("name", ""))
                       for a in self.assets_all if a["project_id"] == project["id"]])

    def task_types(self, project, kind):
        want = "Shot" if kind == "shot" else "Asset"
        return [t for t in self.task_types_all if t["for_entity"] == want]

    def statuses(self, project):
        return list(self.statuses_all)

    def tasks_by_entity(self, project, task_type, episode):
        time.sleep(0.2)
        return {eid: t for (eid, ttid), t in self.tasks.items() if ttid == task_type["id"]}

    def create_task(self, entity, task_type):
        with self._lock:
            task = {"id": _id(), "entity_id": entity["id"], "task_type_id": task_type["id"], "task_status_id": "st-todo"}
            self.tasks[(entity["id"], task_type["id"])] = task
            self.comments_by_task[task["id"]] = []
            return task

    # -- upload -----------------------------------------------------------

    def _fake_send(self, path, cb):
        size = os.path.getsize(path) if os.path.exists(path) else 1_000_000
        steps = 10
        for i in range(1, steps + 1):
            time.sleep(0.05)
            if cb:
                cb(size * i // steps, size)

    def publish(self, task_id, status_id, text, files: list[PublishFile],
                existing_comment_id=None, existing_preview_id=None, revision=None,
                set_thumbnail=False, on_progress=None, on_file_done=None,
                on_comment_created=None) -> PublishResult:
        res = PublishResult(comment_id=existing_comment_id)
        try:
            self._demo_publish(task_id, status_id, text, files, res, existing_preview_id,
                               revision, on_progress, on_file_done, on_comment_created)
        except Exception as exc:
            raise PublishError(str(exc), res) from exc
        return res

    def _demo_publish(self, task_id, status_id, text, files, res, existing_preview_id,
                      revision, on_progress, on_file_done, on_comment_created):
        with self._lock:
            comments = self.comments_by_task.setdefault(task_id, [])
            if res.comment_id is None:
                comment = {
                    "id": _id(), "created_at": datetime.now().isoformat(timespec="seconds"),
                    "text": text, "person": {"first_name": "Demo", "last_name": "User"},
                    "task_status": next(s for s in self.statuses_all if s["id"] == status_id),
                    "previews": [], "attachment_files": [],
                }
                comments.insert(0, comment)
                res.comment_id = comment["id"]
                # Like Kitsu: posting a comment sets the task status.
                self._task_by_id(task_id)["task_status_id"] = status_id
                if on_comment_created:
                    on_comment_created(res.comment_id)
            comment = next(c for c in comments if c["id"] == res.comment_id)
            # Same rule as Zou: create_preview starts a new revision,
            # create_extra_preview joins the given preview's revision.
            first_rev = next((p["revision"] for c in comments for p in c["previews"]
                              if p["id"] == existing_preview_id), None)
        for f in files:
            if "fail" in os.path.basename(f.path).lower() and f.path not in self._failed_once:
                # Lets you exercise the error / retry path in demo mode:
                # the first attempt fails, a retry succeeds.
                self._failed_once.add(f.path)
                self._fake_send(f.path, (lambda s, t, r=f.row_id: on_progress(r, s // 2, t)) if on_progress else None)
                raise RuntimeError("Simulated network error (filename contains 'fail')")
            self._fake_send(f.path, (lambda s, t, r=f.row_id: on_progress(r, s, t)) if on_progress else None)
            ext = f.path.rsplit(".", 1)[-1].lower() if "." in f.path else ""
            with self._lock:
                if f.kind == "attachment":
                    comment["attachment_files"].append({"id": _id(), "name": os.path.basename(f.path), "extension": ext})
                else:
                    if first_rev is None:
                        last = max((p["revision"] for c in comments for p in c["previews"]), default=0)
                        first_rev = revision or last + 1
                    pid = _id()
                    comment["previews"].append({"id": pid, "revision": first_rev, "extension": ext})
                    res.preview_ids[f.row_id] = pid
            res.done_row_ids.append(f.row_id)
            if on_file_done:
                on_file_done(f.row_id, res.comment_id)

    # -- undo -------------------------------------------------------------
    # Deleting a comment here does NOT recompute the task status, so the
    # tool's own status-restore step is what puts it back. Whether real Zou
    # recomputes it is not known; the tool handles both.

    def _task_by_id(self, task_id):
        return next(t for t in self.tasks.values() if t["id"] == task_id)

    def task(self, task_id):
        return dict(self._task_by_id(task_id))

    def entity_preview_id(self, entity_id):
        return self.thumbnails.get(entity_id)

    def delete_comment(self, comment_id):
        with self._lock:
            for comments in self.comments_by_task.values():
                comments[:] = [c for c in comments if c["id"] != comment_id]

    def set_task_status(self, task_id, status_id):
        self._task_by_id(task_id)["task_status_id"] = status_id

    def set_main_preview(self, preview_id):
        pass  # demo doesn't render thumbnails

    def add_foreign_comment(self, task_id, text="Looks good, one note"):
        """Demo/test helper: someone else comments on the task."""
        with self._lock:
            self.comments_by_task.setdefault(task_id, []).insert(0, {
                # "+z" sorts after a comment posted in the same second
                "id": _id(), "created_at": datetime.now().isoformat(timespec="seconds") + "z",
                "text": text, "person": {"first_name": "Other", "last_name": "Person"},
                "task_status": self.statuses_all[1], "previews": [], "attachment_files": [],
            })

    # -- download ---------------------------------------------------------

    def comments(self, task_id):
        time.sleep(0.03)
        return list(self.comments_by_task.get(task_id, []))

    def preview_file(self, preview_id):
        return {"id": preview_id, "original_name": f"preview_{preview_id}"}

    def _fake_download(self, path, label, cb):
        total = 400_000
        for i in range(1, 9):
            time.sleep(0.04)
            if cb:
                cb(total * i // 8, total)
        with open(path, "wb") as fh:
            fh.write(f"Kitsu Iko demo placeholder for {label}\n".encode())

    def download_preview(self, preview, path, progress=None):
        self._fake_download(path, f"preview {preview['id']}", progress)

    def download_attachment(self, attachment, path, progress=None):
        self._fake_download(path, f"attachment {attachment.get('name')}", progress)

    def web_url(self, path=""):
        return "https://demo.kitsu.local" + path


def demo_files(folder: str) -> list[str]:
    """Create a handful of empty sample files for trying the upload flow."""
    os.makedirs(folder, exist_ok=True)
    names = [
        "EP01_SQ010_SH010_anim_v003.mov", "EP01_SQ010_SH020_anim_v003.mov",
        "ep01_sq020_sh030_anim_v001.mp4", "SQ030-SH0040_anim_v002.mov",
        "SH050_anim_v001.mov", "reference_moodboard.jpg",
        "SQ010_SH030_anim_fail_v001.mov", "SQ010_SH030_anim_v003_notes.pdf",
        "EP01_SQ030_SH060_anim_v004.mov", "EP01_SQ030_SH060_anim_v004_fail_alt.mov",
    ]
    out = []
    for n in names:
        p = os.path.join(folder, n)
        if not os.path.exists(p):
            with open(p, "wb") as fh:
                fh.write(uuid.uuid4().bytes * 1000)
        out.append(p)
    return out
