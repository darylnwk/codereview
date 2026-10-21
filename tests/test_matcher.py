"""Matcher and kill gate, driven on real planted defects."""

from __future__ import annotations

import unittest

from review_grade.matcher import (
    catches,
    describes,
    diff_new_lines,
    files_match,
    kill_gate,
    line_in_span,
    score_findings,
)
from review_grade.rubric import load_prefix
from review_grade.stress import format_summary, load_subset, run_stress
from review_grade.subset import default_jsonl, load_rows, select_subset


def _line_in_diff(diff: str, issue: dict) -> int:
    """An in-span line the diff shows, or the span start when none does."""
    for path, numbers in diff_new_lines(diff).items():
        if files_match(issue["file_path"], path):
            for number in range(int(issue["start_line"]), int(issue["end_line"]) + 1):
                if number in numbers:
                    return number
    return int(issue["start_line"])


def _finding(issue: dict, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "file": issue["file_path"],
        "line": issue["start_line"],
        "what_breaks": issue["title"],
        "fix": "Apply a one-line correction.",
    }
    item.update(overrides)
    return item


class MatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.defect = select_subset(load_rows(default_jsonl()))[0]["issues"][0]

    def test_hit_miss_empty_and_recall_fraction(self) -> None:
        defect = self.defect
        hit = score_findings([_finding(defect)], [defect])
        self.assertEqual(hit.caught, 1)
        self.assertEqual(hit.planted, 1)
        self.assertEqual(hit.recall, hit.caught / hit.planted)

        missed = score_findings(
            [_finding(defect, file="other/place.py")],
            [defect],
        )
        self.assertEqual(missed.caught, 0)
        outside = score_findings(
            [_finding(defect, line=defect["end_line"] + 1)],
            [defect],
        )
        self.assertEqual(outside.caught, 0)

        empty = score_findings([], [defect])
        self.assertEqual(empty.caught, 0)
        self.assertEqual(empty.planted, 1)
        self.assertEqual(empty.recall, 0)

        unrelated = score_findings(
            [
                _finding(
                    defect,
                    what_breaks="Rename this symbol so the house style stays consistent.",
                    fix="Rename the symbol.",
                )
            ],
            [defect],
        )
        self.assertEqual(unrelated.caught, 0)

        for payload in ("4 of 6", {"count": 4}, {"recall": "4 of 6"}, 4):
            rejected = score_findings(payload, [defect])
            self.assertEqual(rejected.caught, 0)
            self.assertEqual(rejected.planted, 1)

        twice = score_findings([_finding(defect), _finding(defect)], [defect])
        self.assertEqual(twice.caught, 1)

    def test_scene_words_do_not_catch_a_different_defect_in_the_same_span(self) -> None:
        # Ghost pull 10 plants a missing query invalidation at this span.
        # The cached pass-2 finding at line 2823 is a different break
        # (a late confirm overwrites a disable) and only shares scene words.
        defect = None
        for item in load_subset():
            for issue in item["issues"]:
                path = issue["file_path"]
                if (
                    path.endswith("use-activity-pub-queries.ts")
                    and issue["start_line"] == 2820
                    and issue["end_line"] == 2834
                ):
                    defect = issue
        self.assertIsNotNone(defect)
        self.assertIn("invalidation", defect["title"])
        finding = {
            "file": "apps/activitypub/src/hooks/use-activity-pub-queries.ts",
            "line": 2823,
            "what_breaks": (
                "confirm onSuccess writes enabled+confirmed into the shared "
                "account cache with no generation check, so a late success "
                "overwrites a completed disable."
            ),
            "fix": (
                "Only apply the confirm cache update if Bluesky is still "
                "enabled and this confirmation attempt has not been cancelled."
            ),
        }
        self.assertTrue(files_match(finding["file"], defect["file_path"]))
        self.assertTrue(line_in_span(finding["line"], defect))
        self.assertFalse(describes(finding, defect))
        self.assertFalse(catches(finding, defect))
        missed = score_findings([finding], [defect])
        self.assertEqual(missed.caught, 0)
        self.assertEqual(missed.planted, 1)
        named = score_findings([_finding(defect, line=2823)], [defect])
        self.assertEqual(named.caught, 1)

    def test_kill_gate_bands(self) -> None:
        self.assertEqual(kill_gate(0.69, 2), "stop")
        self.assertNotEqual(kill_gate(0.69, 1), "stop")
        self.assertEqual(kill_gate(0.69, 1), "second_pass")
        self.assertEqual(kill_gate(0.70, 2), "human decision")
        self.assertEqual(kill_gate(0.849, 2), "human decision")
        self.assertEqual(kill_gate(0.70, 1), "second_pass")
        self.assertEqual(kill_gate(0.85, 1), "ship")
        self.assertEqual(kill_gate(1.0, 1), "ship")
        self.assertEqual(kill_gate(42 / 60, 2), "human decision")
        self.assertEqual(kill_gate(51 / 60, 1), "ship")
        self.assertEqual(kill_gate(41 / 60, 2), "stop")


class StressScheduleTests(unittest.TestCase):
    def test_second_pass_only_when_the_gate_requires_it(self) -> None:
        subset = load_subset()
        self.assertGreater(sum(len(item["issues"]) for item in subset), 0)
        summary = format_summary(subset)
        for item in subset:
            for issue in item["issues"]:
                self.assertNotIn(issue["title"], summary)
                self.assertNotIn(issue["description"], summary)

        calls = {"n": 0}

        def review_nothing(prompt: str) -> dict:
            calls["n"] += 1
            self.assertTrue(prompt.startswith(load_prefix()))
            self.assertNotIn(subset[0]["issues"][0]["title"], prompt)
            return {"findings": []}

        stopped = run_stress(subset, review_nothing)
        self.assertEqual(stopped["planted"], sum(len(item["issues"]) for item in subset))
        self.assertEqual(stopped["caught"], 0)
        self.assertEqual(stopped["recall"], 0)
        self.assertEqual(stopped["verdict"], "stop")
        self.assertEqual(stopped["passes"], 2)
        self.assertEqual(calls["n"], 2 * len(subset))

        calls["n"] = 0
        by_prompt = {}
        for item in subset:
            from review_grade.rubric import assemble_prompt

            by_prompt[assemble_prompt(item["diff"])] = item

        def review_all(prompt: str) -> dict:
            calls["n"] += 1
            item = by_prompt[prompt]
            return {
                "findings": [
                    _finding(issue, line=_line_in_diff(item["diff"], issue))
                    for issue in item["issues"]
                ]
            }

        shipped = run_stress(subset, review_all)
        self.assertEqual(shipped["caught"], shipped["planted"])
        self.assertEqual(shipped["recall"], shipped["caught"] / shipped["planted"])
        self.assertEqual(shipped["verdict"], "ship")
        self.assertEqual(shipped["passes"], 1)
        self.assertEqual(calls["n"], len(subset))


if __name__ == "__main__":
    unittest.main()
