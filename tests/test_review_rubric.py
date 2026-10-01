"""Contract tests for the shipped rubric prefix and findings checker."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_grade.findings import REQUIRED_KEYS, check_findings
from review_grade.rubric import assemble_prompt, load_prefix

FIXTURE = Path(__file__).parent / "fixtures" / "sample.diff"

# Phrases the rubric must not contain. Matched case-insensitively.
_FORBIDDEN = (
    "recall",
    "answer key",
    "benchmark",
    "seeded",
    "ask the user",
    "write a review",
    "your review",
)


def _finding(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "file": "pkg/auth.py",
        "line": 17,
        "what_breaks": "The expiry check is skipped, so an expired token still passes.",
        "fix": "Return false when expires_at is missing or not after now.",
    }
    item.update(overrides)
    return item


class PrefixTests(unittest.TestCase):
    def test_prefix_is_byte_identical_and_leads_the_prompt(self) -> None:
        first = load_prefix()
        second = load_prefix()
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        # ASCII so character length and byte length are the same stable number.
        first.encode("ascii")

        diff = FIXTURE.read_text(encoding="utf-8")
        self.assertNotEqual(diff, "")
        prompt = assemble_prompt(diff)
        prefix = load_prefix()

        self.assertEqual(prefix.encode("utf-8"), first.encode("utf-8"))
        self.assertTrue(prompt.startswith(prefix))
        self.assertGreater(len(prompt), len(prefix))
        self.assertNotIn(diff, prefix)
        self.assertEqual(prompt[len(prefix) :], diff)
        self.assertGreaterEqual(prompt.find(diff), len(prefix))
        for token in ("{diff}", "{timestamp}", "%s"):
            self.assertNotIn(token, prefix)

    def test_empty_diff_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            assemble_prompt("")


class FindingsCheckerTests(unittest.TestCase):
    def test_three_inputs_accept_finding_and_empty_and_reject_recall(self) -> None:
        conforming = check_findings(_finding())
        empty = check_findings([])
        recall = check_findings("4 of 6")

        self.assertTrue(conforming.accepted)
        self.assertTrue(empty.accepted)
        self.assertFalse(recall.accepted)

    def test_document_shape_and_rejects_scores(self) -> None:
        self.assertTrue(check_findings({"findings": [_finding()]}).accepted)
        self.assertTrue(check_findings({"findings": []}).accepted)
        self.assertTrue(
            check_findings(
                '{"findings":[{"file":"pkg/auth.py","line":17,'
                '"what_breaks":"Expired tokens still pass.",'
                '"fix":"Return false when expires_at is not after now."}]}'
            ).accepted
        )

        self.assertFalse(check_findings({"count": 4}).accepted)
        self.assertFalse(check_findings({"caught": 4, "planted": 6}).accepted)
        self.assertFalse(check_findings({"recall": "4 of 6"}).accepted)
        self.assertFalse(check_findings({"score": "4 of 6"}).accepted)
        self.assertFalse(check_findings(4).accepted)
        self.assertFalse(check_findings("4").accepted)
        self.assertFalse(check_findings('{"findings":"4 of 6"}').accepted)
        self.assertFalse(check_findings({"findings": [], "recall": "4 of 6"}).accepted)

        missing = _finding()
        del missing["fix"]
        self.assertFalse(check_findings(missing).accepted)
        self.assertFalse(
            check_findings(_finding(fix="Return false.\nAlso log the token.")).accepted
        )
        self.assertFalse(check_findings(_finding(line=0)).accepted)
        self.assertFalse(check_findings(_finding(line=True)).accepted)


class RubricPolicyTests(unittest.TestCase):
    def test_rubric_names_classes_and_omits_forbidden_instructions(self) -> None:
        text = load_prefix()
        lowered = text.lower()

        self.assertIn(
            "Report a defect that breaks behavior or safety: incorrect logic, "
            "missed edge cases, broken error handling, races, resource leaks, "
            "and auth or security holes.",
            text,
        )
        self.assertIn(
            "Do not report style, formatting, naming, or docs-only notes.",
            text,
        )
        self.assertIn(
            "Do not invent a finding or a target defect count when nothing breaks.",
            text,
        )
        self.assertIn("nothing breaks", lowered)
        self.assertIn("no finding", lowered)
        self.assertIn('{"findings":[]}', text)
        self.assertIn("what breaks", lowered)
        self.assertIn("one-line fix", lowered)
        for key in REQUIRED_KEYS:
            self.assertIn(key, text)

        for phrase in _FORBIDDEN:
            self.assertNotIn(phrase, lowered)
        self.assertIsNone(re.search(r"\d+\s+of\s+\d+", lowered))
        self.assertNotIn("write a review", lowered)
        self.assertNotIn("ask the user for a review", lowered)


if __name__ == "__main__":
    unittest.main()
