import json
import logging
import os
import tempfile
import unittest

from airmedia_share.settings import Settings, merge_site_file


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "settings.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_fresh_defaults(self):
        s = Settings(self.path)
        self.assertEqual(s["tvs"], [])
        self.assertIsNone(s["default"])
        self.assertTrue(s["sound"])

    def test_first_tv_becomes_default_and_urls_are_cleaned(self):
        s = Settings(self.path)
        host = s.add_tv("http://10.0.0.5/", "Office")
        self.assertEqual(host, "10.0.0.5")
        self.assertEqual(s["default"], "10.0.0.5")
        s.add_tv("tv2.example.edu")
        self.assertEqual(s["default"], "10.0.0.5")
        self.assertEqual(s.tv("tv2.example.edu")["label"], "tv2.example.edu")

    def test_round_trip_rename_remove(self):
        s = Settings(self.path)
        s.add_tv("10.0.0.5", "Office")
        s.add_tv("10.0.0.6", "Lab")
        s["default"] = "10.0.0.6"
        s.rename_tv("10.0.0.5", "Big room")
        s.note_receiver_name("10.0.0.5", "ROOM-AM3200")
        s.save()
        t = Settings(self.path)
        self.assertEqual(t.tv("10.0.0.5"), {"host": "10.0.0.5", "label": "Big room",
                                            "name": "ROOM-AM3200"})
        self.assertEqual(t["default"], "10.0.0.6")
        t.remove_tv("10.0.0.6")
        self.assertEqual(t["default"], "10.0.0.5")  # default moves to a TV that exists

    def test_duplicates_and_junk_are_tidied(self):
        with open(self.path, "w") as f:
            json.dump({"tvs": [{"host": "a"}, {"host": "a"}, "junk", {"label": "no host"}],
                       "default": "gone"}, f)
        s = Settings(self.path)
        self.assertEqual([tv["host"] for tv in s["tvs"]], ["a"])
        self.assertEqual(s["default"], "a")

    def test_corrupt_file_starts_fresh(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        with self.assertLogs("airmedia_share.settings", logging.ERROR):
            self.assertEqual(Settings(self.path)["tvs"], [])

    def test_site_file_adds_tvs_without_overriding_the_user(self):
        site = os.path.join(self.dir.name, "site.json")
        with open(site, "w") as f:
            json.dump({"tvs": [{"host": "10.0.0.5", "label": "Site name"},
                               {"host": "10.0.0.7", "label": "Other"}],
                       "default": "10.0.0.7"}, f)
        s = Settings(self.path)
        s.add_tv("10.0.0.5", "My name")
        s.add_tv("10.0.0.9", "Mine")
        s["default"] = "10.0.0.9"
        self.assertEqual(merge_site_file(s, site), 1)
        self.assertEqual(s.tv("10.0.0.5")["label"], "My name")
        self.assertEqual(s["default"], "10.0.0.9")


if __name__ == "__main__":
    unittest.main()
