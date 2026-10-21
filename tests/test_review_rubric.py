"""Contract tests for the shipped rubric prefix and findings checker."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_grade.findings import REQUIRED_KEYS, check_findings
from review_grade.rubric import (
    BREAK_NAMES,
    CLASS_LIST,
    FINDINGS_SHAPE,
    PREFIX,
    SILENCE,
    assemble_prompt,
    load_prefix,
)

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
    "ahead of schedule",
    "fixed literal",
    "wrapped again",
    "trimming the sent",
    "clears search input",
    "high-volume threshold",
    "acceptance audit",
    "one expression to a block",
    "constant string",
    "string array",
    "cite the protocol",
    "does not return its count",
    "chained step",
    "built on every call",
    "cite the import",
    "the schedule",
    "optional chaining",
    "retry limit",
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

    def test_rubric_names_error_paths_checks_and_branches_and_stays_silent(self) -> None:
        text = load_prefix()
        lowered = text.lower()
        self.assertIn("removed error path", lowered)
        self.assertIn("skipped check", lowered)
        self.assertIn("wrong branch", lowered)
        self.assertIn("stay silent when behavior is unchanged", lowered)
        self.assertIn("when you are not sure", lowered)
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
        self.assertNotIn(
            "Report a finding only when a removed line and the added line that "
            "replaced it disagree about the value that is returned, stored, "
            "checked, or sent.",
            text,
        )
        self.assertIn("await", lowered)
        self.assertIn("floor", lowered)
        self.assertIn("cache", lowered)
        self.assertIn("the removed line is the old behavior", lowered)
        self.assertIn("do not assume the added line is a fix", lowered)
        self.assertIn(
            "a changed condition, calculation, type, call, or returned value "
            "is its own finding when the result is wrong",
            lowered,
        )
        self.assertIn(
            "when the diff shows the old behavior, the new behavior, and the "
            "wrong result, each change is its own finding",
            lowered,
        )
        self.assertNotIn("url.protocol", lowered)
        self.assertNotIn("retry attempt", lowered)
        self.assertNotIn("feature image", lowered)
        self.assertNotIn(".then", lowered)
        self.assertNotIn("string array", lowered)
        self.assertNotIn("high volume", lowered)
        self.assertNotIn("onclose", lowered)
        self.assertNotIn("search input", lowered)
        self.assertNotIn("invalidation", lowered)
        self.assertNotIn("hardcoded", lowered)
        self.assertNotIn("read tests", lowered)

    def test_prefix_is_four_capped_sections_without_the_key(self) -> None:
        self.assertEqual(
            CLASS_LIST,
            "Review the diff that follows. Return findings for defects that break behavior or safety. Return JSON only.\n"
            "\n"
            "Report a defect that breaks behavior or safety: incorrect logic, missed edge cases, broken error handling, races, resource leaks, and auth or security holes.\n"
            "- incorrect logic: a condition, calculation, or path produces the wrong result.\n"
            "- missed edge cases: empty input, a boundary including the equal case.\n"
            "- broken error handling: a failure is no longer caught, cleaned up, or reset.\n"
            "- races: shared state is read and written without the order the program assumes.\n"
            "- resource leaks: a resource is acquired and not released.\n"
            "- auth or security holes: a check can be skipped, or untrusted input reaches a sink.\n"
            "\n",
        )
        self.assertEqual(
            SILENCE,
            "Do not report style, formatting, naming, or docs-only notes.\n"
            "\n"
            "Do not invent a finding or a target defect count when nothing breaks. No finding when nothing breaks.\n"
            "\n"
            "Stay silent when behavior is unchanged or when you are not sure. The removed line is the old behavior. Do not assume the added line is a fix. When the diff shows the old behavior, the new behavior, and the wrong result, each change is its own finding.\n"
            "\n"
            "A floor that became round, or a round that became floor, is its own finding. Report both. A changed call, condition, type, order, import, return, field, or cleanup is a finding even when the edit looks intentional. Cite the identifier and the unchanged line a type leaves wrong.\n"
            "\n",
        )
        self.assertEqual(
            BREAK_NAMES,
            "A removed error path, a skipped check, and a wrong branch are findings.\n"
            "\n"
            "A call that is not awaited is a finding. A floor, ceil, or round that changes the result is a finding. A stale or wrong cache or field is a finding. A changed condition, calculation, type, call, or returned value is its own finding when the result is wrong.\n"
            "\n"
            "Cite each on its own changed line, not only the last, and not the line above it or below it. Each is a finding. A call that dropped an argument, option, or field. An operator that lost or gained an equal sign, or a condition that flipped. A number that became text, or text that became a number. An awaited call whose result is not returned. Steps that swapped order, a flush inside a loop, or a cleanup or init that moved. An import or require path whose text changed, or a removed import still used, on that import line. A value placed in an extra object. A fallback the old line had and the new line dropped, or a trim the new line added. A memory leak or a race condition. A counter compared with >= to its maximum. A check that treats zero or empty as missing. A handler that clears a kept value. A container created inside the function. A test whose timeout, wait, assertion, setup, or cleanup changed. A wrong identifier where the old code named another.\n"
            "\n",
        )
        self.assertEqual(
            FINDINGS_SHAPE,
            "Set line from the hunk header. Use the number after the +, never the number after the -. A line that starts with + or a space advances the number. A line that starts with - does not. Put line on the line whose text contains the identifier you quote, not the line before it and not a blank line. Do not report a line this diff does not show.\n"
            "\n"
            "Example of counting only, not a finding: @@ -4,3 +8,3 @@ so ` function save(row) {` is line 8 and `+  await write(row);` is line 10.\n"
            "\n"
            "Ignore stories, snapshots, package manifests, lockfiles, and config. Review tests.\n"
            "\n"
            "Each finding carries file, line, what breaks, and a one-line fix. Keys: file, line, what_breaks, fix.\n"
            "\n"
            'Return one JSON object whose only key is findings, one finding per break. When nothing breaks, return {"findings":[]}\n'
            "\n"
            "Read every hunk.\n"
            "\n"
            "Cite the changed line, not the blank line above it and not a later line. Quote the identifier on that line in what_breaks. Do not stop after the first finding in a hunk. Do not skip a change because the new behavior looks helpful.\n"
            "\n"
            "Quote the changed operator, call, field, type, or path on its own line. A dropped argument is the call that lost it. A moved step is the line that now runs too early or too late. A removed fallback is the line that no longer supplies the old value. A renamed path is the import that still names the old path. A test finding cites the changed wait, assertion, or cleanup. Do not cite the next statement.\n"
            "\n"
            "Diff:\n",
        )
        self.assertEqual(load_prefix(), CLASS_LIST + SILENCE + BREAK_NAMES + FINDINGS_SHAPE)
        self.assertEqual(PREFIX, load_prefix())
        self.assertLessEqual(len(CLASS_LIST), 1200)
        self.assertLessEqual(len(SILENCE), 700)
        self.assertLessEqual(len(BREAK_NAMES), 1300)
        self.assertLessEqual(len(FINDINGS_SHAPE), 1600)
        for section in (CLASS_LIST, SILENCE, BREAK_NAMES, FINDINGS_SHAPE):
            for paragraph in section.split("\n\n"):
                self.assertLessEqual(len(paragraph), 1000)
        import json
        from pathlib import Path

        subset = json.loads(
            (Path(__file__).resolve().parents[1] / "bench" / "subset.json").read_text(
                encoding="utf-8"
            )
        )
        prefix = load_prefix()
        for item in subset:
            for issue in item["issues"]:
                title = issue.get("title") or ""
                description = issue.get("description") or ""
                if title:
                    self.assertNotIn(title, prefix)
                if description:
                    self.assertNotIn(description, prefix)


if __name__ == "__main__":
    unittest.main()
