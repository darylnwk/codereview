"""Beat decision on constructed counts. No model call and no review text."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from review_grade.matcher import (
    Score,
    anchor_findings,
    beats_graphite,
    diff_new_lines,
    files_match,
    keep_lines_in_diff,
    line_in_span,
    line_present_in_diff,
    score_findings,
)
from review_grade.reviewer import SONNET_MODEL, claude_command
from review_grade.rubric import assemble_prompt, load_prefix
from review_grade.stress import claude_model_for, format_report, load_subset, run_stress
from review_grade.subset import planted_count

FIXTURE = Path(__file__).parent / "fixtures" / "sample.diff"


def _line_in_diff(diff: str, issue: dict) -> int:
    for path, numbers in diff_new_lines(diff).items():
        if files_match(issue["file_path"], path):
            for number in range(int(issue["start_line"]), int(issue["end_line"]) + 1):
                if number in numbers:
                    return number
    return int(issue["start_line"])


def _reachable(subset: list[dict]) -> int:
    count = 0
    for item in subset:
        for issue in item["issues"]:
            line = _line_in_diff(item["diff"], issue)
            if line_present_in_diff(item["diff"], issue["file_path"], line) and line_in_span(
                line, issue
            ):
                count += 1
    return count


class BeatDecisionTests(unittest.TestCase):
    def test_counts_above_both_bars_pass_and_boundaries_fail(self) -> None:
        above = Score(planted=60, caught=11, emitted=4, true_findings=3)
        self.assertTrue(
            beats_graphite(
                caught=above.caught,
                planted=above.planted,
                true_findings=above.true_findings,
                emitted=above.emitted,
            )
        )
        self.assertGreater(above.recall, 21 / 115)
        self.assertGreater(above.precision, 0.667)

        just_above = Score(planted=115, caught=22, emitted=1000, true_findings=668)
        self.assertTrue(
            beats_graphite(
                caught=just_above.caught,
                planted=just_above.planted,
                true_findings=just_above.true_findings,
                emitted=just_above.emitted,
            )
        )

        recall_equal = Score(planted=115, caught=21, emitted=1, true_findings=1)
        self.assertFalse(
            beats_graphite(
                caught=recall_equal.caught,
                planted=recall_equal.planted,
                true_findings=recall_equal.true_findings,
                emitted=recall_equal.emitted,
            )
        )
        recall_below = Score(planted=60, caught=10, emitted=1, true_findings=1)
        self.assertFalse(
            beats_graphite(
                caught=recall_below.caught,
                planted=recall_below.planted,
                true_findings=recall_below.true_findings,
                emitted=recall_below.emitted,
            )
        )
        precision_equal = Score(planted=60, caught=11, emitted=1000, true_findings=667)
        self.assertFalse(
            beats_graphite(
                caught=precision_equal.caught,
                planted=precision_equal.planted,
                true_findings=precision_equal.true_findings,
                emitted=precision_equal.emitted,
            )
        )
        precision_below = Score(planted=60, caught=11, emitted=3, true_findings=2)
        self.assertFalse(
            beats_graphite(
                caught=precision_below.caught,
                planted=precision_below.planted,
                true_findings=precision_below.true_findings,
                emitted=precision_below.emitted,
            )
        )
        empty = Score(planted=60, caught=11, emitted=0, true_findings=0)
        self.assertFalse(
            beats_graphite(
                caught=empty.caught,
                planted=empty.planted,
                true_findings=empty.true_findings,
                emitted=empty.emitted,
            )
        )
        self.assertEqual(empty.precision, 0.0)

    def test_sonnet_stress_path_selects_claude_sonnet_5_5(self) -> None:
        self.assertEqual(SONNET_MODEL, "claude-sonnet-5-5")
        self.assertEqual(claude_model_for("sonnet"), "claude-sonnet-5-5")
        command = claude_command(claude_model_for("sonnet"))
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(command[0], "claude")

    def test_scorer_counts_feed_the_decision(self) -> None:
        subset = load_subset()
        defect = subset[0]["issues"][0]
        matched = {
            "file": defect["file_path"],
            "line": defect["start_line"],
            "what_breaks": defect["title"],
            "fix": "Restore the check the removed line had.",
        }
        unmatched = {
            "file": defect["file_path"],
            "line": defect["start_line"],
            "what_breaks": "Rename this symbol so the house style stays consistent.",
            "fix": "Rename the symbol.",
        }
        hit = score_findings([matched], [defect])
        self.assertEqual(hit.caught, 1)
        self.assertEqual(hit.emitted, 1)
        self.assertEqual(hit.true_findings, 1)
        self.assertEqual(hit.precision, hit.true_findings / hit.emitted)

        miss = score_findings([unmatched], [defect])
        self.assertEqual(miss.caught, 0)
        self.assertEqual(miss.emitted, 1)
        self.assertEqual(miss.true_findings, 0)

        both = score_findings([matched, unmatched], [defect])
        self.assertEqual(both.caught, 1)
        self.assertEqual(both.emitted, 2)
        self.assertEqual(both.true_findings, 1)
        self.assertFalse(
            beats_graphite(
                caught=both.caught,
                planted=both.planted,
                true_findings=both.true_findings,
                emitted=both.emitted,
            )
        )

        none = score_findings([], [defect])
        self.assertEqual(none.emitted, 0)
        self.assertEqual(none.true_findings, 0)
        self.assertFalse(
            beats_graphite(
                caught=none.caught,
                planted=none.planted,
                true_findings=none.true_findings,
                emitted=none.emitted,
            )
        )

        twice = score_findings([matched, matched], [defect])
        self.assertEqual(twice.caught, 1)
        self.assertEqual(twice.true_findings, 2)
        self.assertEqual(twice.emitted, 2)

    def test_stress_scores_the_cited_line_and_does_not_move_it_into_the_span(self) -> None:
        item = next(
            row
            for row in load_subset()
            if any(
                issue["file_path"].endswith("BlueskySharing.tsx")
                and issue["start_line"] == 48
                for issue in row["issues"]
            )
        )
        defect = next(
            issue
            for issue in item["issues"]
            if issue["file_path"].endswith("BlueskySharing.tsx") and issue["start_line"] == 48
        )
        self.assertTrue(defect["file_path"].endswith("BlueskySharing.tsx"))
        self.assertEqual(defect["start_line"], 48)
        self.assertEqual(defect["end_line"], 55)
        finding = {
            "file": defect["file_path"],
            "line": 99999,
            "what_breaks": "enableBlueskyMutation leaves the UI loading when the call fails.",
            "fix": "Restore the try/catch so setLoading(false) runs when the call fails.",
        }
        raw = score_findings({"findings": [finding]}, item["issues"])
        self.assertEqual(raw.caught, 0)
        cited_inside = dict(finding, line=53)
        self.assertTrue(line_present_in_diff(item["diff"], defect["file_path"], 53))
        self.assertTrue(line_in_span(53, defect))
        self.assertEqual(score_findings({"findings": [cited_inside]}, item["issues"]).caught, 1)

        def review(prompt: str) -> dict:
            self.assertNotIn(defect["title"], prompt)
            self.assertNotIn(defect["description"], prompt)
            return {"findings": [dict(finding)]}

        result = run_stress([item], review)
        self.assertEqual(result["caught"], 0)
        self.assertEqual(result["true_findings"], 0)
        self.assertEqual(result["emitted"], 1)
        self.assertEqual(result["planted"], len(item["issues"]))
        self.assertEqual(len(result["counted_findings"]), 1)
        counted = result["counted_findings"][0]
        self.assertEqual(counted["line"], 99999)
        self.assertEqual(counted["file"], finding["file"])
        self.assertEqual(counted["what_breaks"], finding["what_breaks"])
        self.assertFalse(counted["in_diff"])

        def review_both(prompt: str) -> dict:
            self.assertNotIn(defect["title"], prompt)
            return {"findings": [dict(finding), dict(cited_inside)]}

        mixed = run_stress([item], review_both)
        self.assertEqual(mixed["caught"], 1)
        self.assertEqual(mixed["emitted"], 2)
        self.assertEqual(mixed["true_findings"], 1)
        self.assertEqual(mixed["precision"], 0.5)
        self.assertFalse(
            beats_graphite(
                caught=mixed["caught"],
                planted=mixed["planted"],
                true_findings=mixed["true_findings"],
                emitted=mixed["emitted"],
            )
        )
        self.assertEqual(
            [row["line"] for row in mixed["counted_findings"]],
            [99999, 53],
        )

    def test_absent_line_anchors_to_the_named_added_line(self) -> None:
        diff = FIXTURE.read_text(encoding="utf-8")
        payload = {
            "findings": [
                {
                    "file": "pkg/auth.py",
                    "line": 99,
                    "what_breaks": "accept compares token.expires_at with now and still returns true.",
                    "fix": "Return false when expires_at is before now.",
                }
            ]
        }
        anchored = anchor_findings(diff, payload)
        self.assertEqual(anchored["findings"][0]["line"], 16)
        self.assertTrue(line_present_in_diff(diff, "pkg/auth.py", anchored["findings"][0]["line"]))
        kept = anchor_findings(
            diff,
            {
                "findings": [
                    {
                        "file": "pkg/auth.py",
                        "line": 14,
                        "what_breaks": "accept compares token.expires_at with now and still returns true.",
                        "fix": "Return false when expires_at is before now.",
                    }
                ]
            },
        )
        self.assertEqual(kept["findings"][0]["line"], 14)
        dropped = keep_lines_in_diff(
            diff,
            {
                "findings": [
                    {
                        "file": "pkg/auth.py",
                        "line": 99,
                        "what_breaks": "accept compares token.expires_at with now and still returns true.",
                        "fix": "Return false when expires_at is before now.",
                    },
                    {
                        "file": "pkg/auth.py",
                        "line": 16,
                        "what_breaks": "accept compares token.expires_at with now and still returns true.",
                        "fix": "Return false when expires_at is before now.",
                    },
                ]
            },
        )
        self.assertEqual([item["line"] for item in dropped["findings"]], [16])

    def test_sample_diff_lines_are_the_new_file_numbers(self) -> None:
        diff = FIXTURE.read_text(encoding="utf-8")
        self.assertTrue(line_present_in_diff(diff, "pkg/auth.py", 16))
        self.assertTrue(line_present_in_diff(diff, "pkg/auth.py", 14))
        self.assertFalse(line_present_in_diff(diff, "pkg/auth.py", 13))
        self.assertFalse(line_present_in_diff(diff, "other.py", 16))

    def test_stress_report_counts_and_keeps_the_key_out(self) -> None:
        subset = load_subset()
        planted = planted_count(subset)

        def review_nothing(prompt: str) -> dict:
            self.assertTrue(prompt.startswith(load_prefix()))
            self.assertNotIn(subset[0]["issues"][0]["title"], prompt)
            return {"findings": []}

        empty = run_stress(subset, review_nothing)
        self.assertEqual(empty["planted"], planted)
        self.assertEqual(empty["emitted"], 0)
        self.assertEqual(empty["true_findings"], 0)
        self.assertEqual(empty["caught"], 0)
        self.assertFalse(
            beats_graphite(
                caught=empty["caught"],
                planted=empty["planted"],
                true_findings=empty["true_findings"],
                emitted=empty["emitted"],
            )
        )
        empty_report = format_report(subset, empty)
        self.assertIn("precision=undefined\n", empty_report)
        self.assertIn("beats_graphite=no\n", empty_report)
        self.assertIn("misses_kept=yes\n", empty_report)
        self.assertIn("one_line_fixes=0\n", empty_report)
        self.assertIn("key_title_absent=yes\n", empty_report)
        self.assertIn("key_description_absent=yes\n", empty_report)

        by_prompt = {assemble_prompt(item["diff"]): item for item in subset}

        def review_all(prompt: str) -> dict:
            item = by_prompt[prompt]
            return {
                "findings": [
                    {
                        "file": issue["file_path"],
                        "line": _line_in_diff(item["diff"], issue),
                        "what_breaks": issue["title"],
                        "fix": "Restore the behavior the removed line had.",
                    }
                    for issue in item["issues"]
                ]
            }

        reachable = _reachable(subset)
        full = run_stress(subset, review_all)
        self.assertEqual(full["planted"], planted)
        self.assertEqual(full["caught"], planted)
        self.assertEqual(full["emitted"], planted)
        self.assertEqual(full["true_findings"], planted)
        self.assertLess(reachable, planted)
        self.assertEqual(
            sum(1 for row in full["counted_findings"] if row["in_diff"]),
            reachable,
        )
        self.assertEqual(full["recall"], full["caught"] / full["planted"])
        self.assertEqual(full["precision"], full["true_findings"] / full["emitted"])
        self.assertTrue(
            beats_graphite(
                caught=full["caught"],
                planted=full["planted"],
                true_findings=full["true_findings"],
                emitted=full["emitted"],
            )
        )
        report = format_report(subset, full)
        fields = _fields(report)
        self.assertEqual(int(fields["planted"]), planted)
        self.assertEqual(int(fields["caught"]), planted)
        self.assertEqual(int(fields["emitted"]), planted)
        self.assertEqual(int(fields["true_findings"]), planted)
        self.assertEqual(ast.literal_eval(fields["recall"]), full["caught"] / full["planted"])
        self.assertEqual(
            ast.literal_eval(fields["precision"]),
            full["true_findings"] / full["emitted"],
        )
        self.assertEqual(int(fields["one_line_fixes"]), planted)
        self.assertEqual(int(fields["lines_in_diff"]), reachable)
        self.assertEqual(fields["beats_graphite"], "yes")
        self.assertEqual(fields["misses_kept"], "yes")
        self.assertEqual(fields["key_title_absent"], "yes")
        self.assertEqual(fields["key_description_absent"], "yes")
        excerpt = report.split("prompt_excerpt<<\n", 1)[1].split("\n>>\n", 1)[0]
        self.assertTrue(excerpt.startswith(load_prefix()))
        for item in subset:
            for issue in item["issues"]:
                self.assertNotIn(issue["title"], excerpt)
                self.assertNotIn(issue["description"], excerpt)
        block = report.split("counted_findings<<\n", 1)[1].split("\n>>\n", 1)[0]
        rows = [json.loads(line) for line in block.splitlines() if line]
        self.assertEqual(len(rows), planted)
        for row in rows:
            self.assertTrue(row["one_line_fix"])
            self.assertNotIn("\n", row["fix"])
            self.assertTrue(row["fix"].strip())
            self.assertIsInstance(row["what_breaks"], str)
            self.assertTrue(row["what_breaks"].strip())
            self.assertNotIn("\n", row["what_breaks"])

    def test_key_title_absent_scans_every_title_in_the_excerpt(self) -> None:
        planted = "unique planted title in the diff head"
        subset = [
            {
                "diff": f"diff --git a/a.ts b/a.ts\n@@ -1 +1 @@\n-old\n+{planted}\n",
                "issues": [
                    {
                        "title": "first title stays out of the excerpt",
                        "description": "first description stays out",
                        "file_path": "a.ts",
                        "start_line": 1,
                        "end_line": 1,
                    },
                    {
                        "title": planted,
                        "description": "later description stays out",
                        "file_path": "a.ts",
                        "start_line": 1,
                        "end_line": 1,
                    },
                ],
            }
        ]
        result = {
            "planted": 2,
            "caught": 0,
            "emitted": 0,
            "true_findings": 0,
            "recall": 0.0,
            "counted_findings": [],
            "verdict": "stop",
            "passes": 2,
        }
        report = format_report(subset, result)
        self.assertIn("key_title_absent=no\n", report)
        self.assertIn("key_description_absent=yes\n", report)
        excerpt = report.split("prompt_excerpt<<\n", 1)[1].split("\n>>\n", 1)[0]
        self.assertIn(planted, excerpt)
        self.assertNotIn(subset[0]["issues"][0]["title"], excerpt)


def _fields(report: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_block = False
    for line in report.splitlines():
        if line.endswith("<<"):
            in_block = True
            continue
        if in_block:
            if line == ">>":
                in_block = False
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    return fields


if __name__ == "__main__":
    unittest.main()
