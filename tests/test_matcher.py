import unittest

from kitsu_iko.matcher import match_asset, match_shot, parse_version, tokenize

SHOTS = [
    {"id": "s1", "name": "SH010", "sequence_name": "SQ010"},
    {"id": "s2", "name": "SH020", "sequence_name": "SQ010"},
    {"id": "s3", "name": "SH010", "sequence_name": "SQ020"},
    {"id": "s4", "name": "SH0100", "sequence_name": "SQ020"},
]

ASSETS = [
    {"id": "a1", "name": "Tree", "asset_type_name": "Props"},
    {"id": "a2", "name": "TreeA", "asset_type_name": "Props"},
    {"id": "a3", "name": "Hero_Bob", "asset_type_name": "Characters"},
    {"id": "a4", "name": "Castle", "asset_type_name": "Environment"},
]


class TokenizeTest(unittest.TestCase):
    def test_strips_padding_and_splits(self):
        self.assertEqual(
            tokenize("EP01_SQ010-SH0020_anim_v003"),
            ["ep1", "sq10", "sh20", "anim", "v3"],
        )

    def test_glued_tokens(self):
        self.assertEqual(tokenize("sq010sh020"), ["sq10", "sh20"])

    def test_version(self):
        self.assertEqual(parse_version("SQ010_SH020_anim_v012.mov"), 12)
        self.assertEqual(parse_version("SQ010_SH020_anim.mov"), None)
        self.assertEqual(parse_version("hero_v2_v7.png"), 7)


class ShotMatchTest(unittest.TestCase):
    def test_seq_and_shot(self):
        r = match_shot("SQ010_SH020_anim_v003.mov", SHOTS)
        self.assertEqual(r.entity_id, "s2")

    def test_disambiguates_by_sequence(self):
        self.assertEqual(match_shot("sq20_sh10.mov", SHOTS).entity_id, "s3")
        self.assertEqual(match_shot("SQ010-SH010.mp4", SHOTS).entity_id, "s1")

    def test_ambiguous_without_sequence_goes_to_pool(self):
        r = match_shot("SH010_comp.mov", SHOTS)
        self.assertIsNone(r.entity_id)
        self.assertEqual({c.entity_id for c in r.candidates[:2]}, {"s1", "s3"})

    def test_padding_does_not_confuse_10_and_100(self):
        self.assertEqual(match_shot("SQ020_SH0100.mov", SHOTS).entity_id, "s4")

    def test_no_match(self):
        r = match_shot("random_reference.jpg", SHOTS)
        self.assertIsNone(r.entity_id)
        self.assertEqual(r.candidates, [])

    def test_numeric_only_shot_names(self):
        shots = [{"id": "n1", "name": "0010", "sequence_name": "010"},
                 {"id": "n2", "name": "0020", "sequence_name": "010"}]
        self.assertEqual(match_shot("010_0020_v001.mov", shots).entity_id, "n2")


class EmbeddedShotNameTest(unittest.TestCase):
    def test_shot_name_contains_sequence(self):
        shots = [{"id": "x1", "name": "SQ010_SH010", "sequence_name": "SQ010"},
                 {"id": "x2", "name": "SQ020_SH010", "sequence_name": "SQ020"}]
        self.assertEqual(match_shot("sq020_sh010_comp_v2.exr", shots).entity_id, "x2")


class EpisodeTest(unittest.TestCase):
    SHOTS = [{"id": "e1", "name": "SH010", "sequence_name": "SQ010", "episode_name": "EP01"},
             {"id": "e2", "name": "SH010", "sequence_name": "SQ010", "episode_name": "EP02"}]

    def test_episode_token_disambiguates(self):
        self.assertEqual(match_shot("EP02_SQ010_SH010_v1.mov", self.SHOTS).entity_id, "e2")

    def test_no_episode_token_goes_to_pool(self):
        self.assertIsNone(match_shot("SQ010_SH010_v1.mov", self.SHOTS).entity_id)


class AssetMatchTest(unittest.TestCase):
    def test_exact_tokens(self):
        self.assertEqual(match_asset("hero_bob_model_v002.png", ASSETS).entity_id, "a3")

    def test_squashed(self):
        self.assertEqual(match_asset("HeroBob_turntable.mov", ASSETS).entity_id, "a3")

    def test_longest_wins(self):
        self.assertEqual(match_asset("TreeA_v001.png", ASSETS).entity_id, "a2")
        self.assertEqual(match_asset("Tree_v001.png", ASSETS).entity_id, "a1")

    def test_unmatched(self):
        self.assertIsNone(match_asset("moodboard.jpg", ASSETS).entity_id)


if __name__ == "__main__":
    unittest.main()
