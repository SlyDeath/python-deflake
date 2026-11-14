import os
import unittest

STATE = {"polluted": False}


class OrderTest(unittest.TestCase):
    def test_pollutes(self):
        STATE["polluted"] = True
        os.environ["DEFLAKE_FIXTURE_FLAG"] = "1"

    def test_victim(self):
        self.assertFalse(STATE["polluted"])

    def test_unrelated(self):
        self.assertEqual(1 + 1, 2)


class OtherTest(unittest.TestCase):
    def test_one(self):
        pass

    def test_skipped(self):
        self.skipTest("not today")
