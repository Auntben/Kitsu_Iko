import os
import unittest

from kitsu_iko.models import (
    ATTACHMENT, DONE, REVISION, UploadDefaults, UploadItem, build_choices,
    plan_upload, render_comment, render_path,
)

TASKS = {"e1": {"id": "t1"}, "e2": {"id": "t2"}}
NAMES = {"e1": "SH010", "e2": "SH020", "e3": "SH030"}


def item(row, name, entity, **kw):
    return UploadItem(row_id=row, path=f"/x/{name}", entity_id=entity, **kw)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.d = UploadDefaults(status_id="wfa", comment="Update {version}")

    def test_combines_same_entity(self):
        items = [item(1, "a_v001.mov", "e1"), item(2, "b_v001.mov", "e1"), item(3, "c.mov", "e2")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual([len(g.items) for g in plan.groups], [2, 1])
        self.assertEqual(plan.groups[0].text, "Update v001")

    def test_override_splits_group_and_warns(self):
        items = [item(1, "a.mov", "e1"), item(2, "b.mov", "e1", status_id="retake")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual(len(plan.groups), 2)
        self.assertTrue(any("2 separate comments" in w for w in plan.warnings))

    def test_no_combine(self):
        self.d.combine = False
        items = [item(1, "a.mov", "e1"), item(2, "b.mov", "e1")]
        self.assertEqual(len(plan_upload(items, self.d, TASKS, NAMES).groups), 2)

    def test_missing_task_blocks_unless_create(self):
        items = [item(1, "a.mov", "e3")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertIn(1, plan.blocked)
        self.d.create_missing_tasks = True
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual(plan.tasks_to_create, 1)
        self.assertIsNone(plan.groups[0].task_id)

    def test_unassigned_excluded_done(self):
        items = [item(1, "a.mov", None), item(2, "b.mov", "e1", include=False),
                 item(3, "c.mov", "e1", state=DONE), item(4, "d.mov", "e1")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual((plan.unassigned, plan.excluded, plan.file_count), (1, 1, 1))

    def test_resume_keeps_comment(self):
        items = [item(1, "a.mov", "e1", comment_id="c9"), item(2, "b.mov", "e1")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual(len(plan.groups), 2)
        self.assertEqual(plan.groups[0].comment_id, "c9")

    def test_resume_joins_posted_revision(self):
        items = [item(1, "a.mov", "e1", comment_id="c9", preview_id="p1", state=DONE),
                 item(2, "b.mov", "e1", comment_id="c9", state="error")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual(len(plan.groups), 1)
        self.assertEqual((plan.groups[0].comment_id, plan.groups[0].preview_id), ("c9", "p1"))

    def test_non_viewable_revision_warns(self):
        items = [item(1, "a.exr", "e1"), item(2, "b.exr", "e2", mode=ATTACHMENT)]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertTrue(any("1 revision file" in w for w in plan.warnings))

    def test_revision_from_filename(self):
        self.d.revision_from_filename = True
        items = [item(1, "a_v004.mov", "e1"), item(2, "a_v005.png", "e1")]
        plan = plan_upload(items, self.d, TASKS, NAMES)
        self.assertEqual(plan.groups[0].revision, 5)
        self.assertTrue(any("different versions" in w for w in plan.warnings))


class HelpersTest(unittest.TestCase):
    def test_render_comment_unknown_placeholder(self):
        it = UploadItem(row_id=1, path="/x/a.mov")
        self.assertEqual(render_comment("{oops} {filename}", [it]), "{oops} a.mov")

    def test_group_text_uses_highest_version(self):
        d = UploadDefaults(status_id="wfa", comment="{version}")
        items = [item(1, "a_v004.mov", "e1"), item(2, "a_v005.png", "e1")]
        self.assertEqual(plan_upload(items, d, TASKS, NAMES).groups[0].text, "v005")

    def test_render_path_drops_empty(self):
        p = render_path("{project}/{episode}/{entity}", {"project": "Show", "episode": "", "entity": "SQ/010"})
        self.assertEqual(p, os.path.join("Show", "SQ_010"))

    def test_build_choices(self):
        comments = [
            {"created_at": "2026-01-01T10:00:00", "previews": [{"id": "p1", "revision": 1}], "attachment_files": []},
            {"created_at": "2026-01-03T10:00:00", "previews": [{"id": "p2", "revision": 2}, {"id": "p3", "revision": 2}],
             "attachment_files": [{"id": "a1", "name": "n.pdf"}]},
        ]
        revs = build_choices(comments, REVISION)
        self.assertEqual([c.revision for c in revs], [2, 1])
        self.assertEqual(len(revs[0].files), 2)
        atts = build_choices(comments, ATTACHMENT)
        self.assertEqual(len(atts), 1)


if __name__ == "__main__":
    unittest.main()
