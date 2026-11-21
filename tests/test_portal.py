import unittest

from airmedia_share.backends import portal


class RequestPathTest(unittest.TestCase):
    def test_unique_name_becomes_the_senders_path_segment(self):
        self.assertEqual(portal.request_path(":1.42", "tok"),
                         "/org/freedesktop/portal/desktop/request/1_42/tok")

    def test_longer_names(self):
        self.assertEqual(portal.request_path(":1.5349", "airmedia_ab12"),
                         "/org/freedesktop/portal/desktop/request/1_5349/airmedia_ab12")


class SourceTypesTest(unittest.TestCase):
    def test_each_kind_offers_what_it_says(self):
        self.assertEqual(portal.source_types("monitor"), portal.MONITOR)
        self.assertEqual(portal.source_types("window"), portal.WINDOW)
        self.assertEqual(portal.source_types("any"), portal.MONITOR | portal.WINDOW)

    def test_unknown_kinds_offer_both(self):
        self.assertEqual(portal.source_types("anything else"), portal.MONITOR | portal.WINDOW)


class StartResultsTest(unittest.TestCase):
    def test_a_monitor(self):
        results = {"streams": [(57, {"source_type": 1, "size": (2560, 1440)})],
                   "restore_token": "abc"}
        self.assertEqual(portal.parse_start_results(results),
                         (57, portal.MONITOR, (2560, 1440), "abc"))

    def test_a_window_without_size_or_token(self):
        results = {"streams": [(61, {"source_type": 2})]}
        self.assertEqual(portal.parse_start_results(results),
                         (61, portal.WINDOW, None, None))

    def test_nothing_shared_is_an_error(self):
        for results in ({}, {"streams": []}):
            with self.assertRaises(portal.PortalError):
                portal.parse_start_results(results)


if __name__ == "__main__":
    unittest.main()
