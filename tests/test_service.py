import unittest
from unittest import mock

from kitsu_iko.service import KitsuService, active

PROJECT = {"id": "p1", "name": "Show", "production_type": "tvshow"}
EPISODE = {"id": "ep1", "name": "EP01"}


class CanceledEntitiesTest(unittest.TestCase):
    """Shots/assets deleted in Kitsu's web UI are only flagged ``canceled``;
    they must not show up anywhere in the tool."""

    def test_active(self):
        self.assertEqual([e["id"] for e in active([{"id": 1}, {"id": 2, "canceled": True},
                                                   {"id": 3, "canceled": False}])], [1, 3])

    def test_real_service_hides_canceled_shots_and_assets(self):
        shots = [{"id": "s1", "name": "SH010", "parent_id": "q1"},
                 {"id": "s2", "name": "SH020", "parent_id": "q1", "canceled": True}]
        assets = [{"id": "a1", "name": "Tree", "entity_type_id": "t"},
                  {"id": "a2", "name": "Sword", "entity_type_id": "t", "canceled": True}]
        svc = KitsuService()
        with mock.patch("gazu.shot.all_shots_for_episode", return_value=shots), \
             mock.patch("gazu.shot.all_sequences_for_episode", return_value=[{"id": "q1", "name": "SQ010"}]), \
             mock.patch("gazu.asset.all_assets_for_project", return_value=assets), \
             mock.patch("gazu.asset.all_asset_types_for_project", return_value=[{"id": "t", "name": "Prop"}]), \
             mock.patch.object(KitsuService, "_client", return_value=None):
            self.assertEqual([s["name"] for s in svc.shots(PROJECT, EPISODE)], ["SH010"])
            self.assertEqual([a["name"] for a in svc.assets(PROJECT, None)], ["Tree"])


if __name__ == "__main__":
    unittest.main()
