import os
import tempfile
import unittest

from kitsu_iko.history import CommentRecord, UploadJournal, inspect, undo_record
from kitsu_iko.models import ATTACHMENT, UploadDefaults, UploadItem, mode_counts, plan_upload


class FakeServer:
    """Comments per task and task statuses. Deleting a comment does not
    recompute the status (like the demo), so restore is the tool's job."""

    def __init__(self):
        self.statuses = {"t1": "wfa"}
        self.by_task = {"t1": []}
        self.deleted = []
        self.thumb = None
        self._clock = 0

    def post(self, task_id, cid, status, replies=0):
        self._clock += 1
        self.by_task[task_id].append({"id": cid, "created_at": f"2026-01-01T00:00:{self._clock:02d}",
                                      "replies": [{}] * replies})
        self.statuses[task_id] = status

    # -- the service API used by kitsu_iko.history
    def comments(self, task_id):
        return list(self.by_task[task_id])

    def task(self, task_id):
        return {"id": task_id, "task_status_id": self.statuses[task_id]}

    def delete_comment(self, cid):
        self.deleted.append(cid)
        for lst in self.by_task.values():
            lst[:] = [c for c in lst if c["id"] != cid]

    def set_task_status(self, task_id, status_id):
        self.statuses[task_id] = status_id

    def set_main_preview(self, preview_id):
        self.thumb = preview_id


def make_server():
    s = FakeServer()
    return s, s


def rec(cid, prev="wip", new="wfa", **kw):
    return CommentRecord(comment_id=cid, task_id="t1", entity_label="SH010",
                         prev_status_id=prev, new_status_id=new, files=["a.mov"], **kw)


NAME = str.upper


class JournalTest(unittest.TestCase):
    def test_persists_and_extends_by_comment(self):
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        j = UploadJournal(path, "host|me")
        b1 = j.new_batch("Show - Anim")
        j.record(b1, rec("c1"))
        b2 = j.new_batch("Show - Anim")  # a retry adding a file to c1
        j.record(b2, CommentRecord("c1", "t1", "SH010", None, "wfa", files=["b.mov"]))
        j2 = UploadJournal(path, "host|me")
        self.assertEqual(j2.records["c1"].files, ["a.mov", "b.mov"])
        self.assertEqual(j2.records["c1"].prev_status_id, "wip")  # original kept
        self.assertEqual([b.id for b in j2.recent()], [b1])  # b2 posted nothing new
        self.assertEqual(UploadJournal(path, "other|user").recent(), [])

    def test_mark_undone(self):
        j = UploadJournal(None, "x")
        b = j.new_batch("ctx")
        j.record(b, rec("c1"))
        j.mark_undone("c1", "deleted")
        self.assertEqual(j.pending_undo(j.recent()[0]), [])


class UndoTest(unittest.TestCase):
    def test_restores_status_when_ours_is_newest(self):
        s, srv = make_server()
        s.post("t1", "old", "wip")
        s.post("t1", "c1", "wfa")
        note = undo_record(srv, rec("c1"), set(), NAME)
        self.assertEqual(s.deleted, ["c1"])
        self.assertEqual(s.statuses["t1"], "wip")
        self.assertIn("status back to WIP", note)

    def test_leaves_status_when_someone_commented_after(self):
        s, srv = make_server()
        s.post("t1", "c1", "wfa")
        s.post("t1", "theirs", "retake", replies=0)
        checks = inspect(srv, [rec("c1")])
        self.assertEqual(checks[0].newer_foreign, 1)
        note = undo_record(srv, rec("c1"), set(), NAME)
        self.assertEqual(s.statuses["t1"], "retake")
        self.assertIn("left as is", note)

    def test_two_comments_on_one_task_unwind_to_original(self):
        s, srv = make_server()
        s.post("t1", "c1", "wfa")
        s.post("t1", "c2", "retake")
        r1, r2 = rec("c1", prev="wip", new="wfa"), rec("c2", prev="wfa", new="retake")
        undo_record(srv, r2, {"c1"}, NAME)  # newest first, c1 still pending
        undo_record(srv, r1, set(), NAME)
        self.assertEqual(s.statuses["t1"], "wip")

    def test_already_deleted(self):
        s, srv = make_server()
        self.assertFalse(inspect(srv, [rec("c1")])[0].exists)
        self.assertEqual(undo_record(srv, rec("c1"), set(), NAME), "already deleted in Kitsu")
        self.assertEqual(s.deleted, [])

    def test_replies_counted_and_thumbnail_restored(self):
        s, srv = make_server()
        s.post("t1", "c1", "wfa", replies=2)
        self.assertEqual(inspect(srv, [rec("c1")])[0].replies, 2)
        undo_record(srv, rec("c1", thumb_set=True, thumb_prev_preview_id="p0"), set(), NAME)
        self.assertEqual(s.thumb, "p0")


class ModeCountsTest(unittest.TestCase):
    def test_flags_media_sent_as_attachment(self):
        d = UploadDefaults(status_id="wfa", mode=ATTACHMENT)
        items = [UploadItem(1, "/x/a.mov", entity_id="e1"), UploadItem(2, "/x/n.pdf", entity_id="e1"),
                 UploadItem(3, "/x/b.mov", entity_id="e1", mode="revision")]
        plan = plan_upload(items, d, {"e1": {"id": "t1"}}, {})
        self.assertEqual(mode_counts(plan, d), (1, 2, 1))


if __name__ == "__main__":
    unittest.main()
