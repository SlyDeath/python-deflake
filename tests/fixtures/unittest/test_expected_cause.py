import os
import unittest


class Cause(unittest.TestCase):
    def setUp(self):
        if self._testMethodName == "test_b_victim" and os.getenv("CAUSE_FLAG"):
            raise RuntimeError("original failure")

    def test_a_pollutes(self):
        os.environ["CAUSE_FLAG"] = "leaked"

    @unittest.expectedFailure
    def test_b_victim(self):
        raise ValueError("expected failure after repair")
