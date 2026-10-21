"""Deliberate failure used only to prove the CI gate goes red."""

from __future__ import annotations

import unittest


class CiGateFailTests(unittest.TestCase):
    def test_deliberately_fails(self) -> None:
        self.fail("deliberate CI gate failure; revert this commit")
