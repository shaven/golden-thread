import unittest

from ledgerlite.config import Config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = Config()
        self.assertEqual(c.get("terms_days"), 30)
        self.assertEqual(c.get("bucket_edges"), (30, 60, 90))

    def test_override_and_unset(self):
        c = Config(terms_days=14)
        self.assertEqual(c.get("terms_days"), 14)
        c.unset("terms_days")
        self.assertEqual(c.get("terms_days"), 30)


if __name__ == "__main__":
    unittest.main()
