"""Subset selection on real Qodo pull-request rows."""

from __future__ import annotations

import json
import re
import unittest

from review_grade.rubric import assemble_prompt, load_prefix
from review_grade.subset import default_jsonl, default_subset_path, load_rows, select_subset

_STYLE = re.compile(r"style|format|naming|docs", re.IGNORECASE)


class SubsetTests(unittest.TestCase):
    def test_selector_keeps_behavior_defects_and_is_stable(self) -> None:
        rows = load_rows(default_jsonl())
        self.assertGreater(len(rows), 0)
        self.assertIn("pr_url_to_review", rows[0])
        self.assertIn("issues", rows[0])

        first = select_subset(rows)
        second = select_subset(list(reversed(rows)))
        urls = [item["pr_url"] for item in first]
        self.assertEqual(urls, [item["pr_url"] for item in second])
        self.assertEqual(len(urls), len(set(urls)))
        self.assertGreater(len(first), 0)
        self.assertEqual(len(first), len(rows))
        self.assertEqual(len(first), 100)

        style_dropped = 0
        kept = {
            (item["pr_url"], issue["title"], issue["file_path"], issue["start_line"])
            for item in first
            for issue in item["issues"]
        }
        for row in rows:
            for issue in row["issues"]:
                rule = issue.get("rule_name") or ""
                if isinstance(rule, str) and _STYLE.search(rule):
                    key = (
                        row["pr_url_to_review"],
                        issue["title"],
                        issue["file_path"],
                        issue["start_line"],
                    )
                    self.assertNotIn(key, kept)
                    style_dropped += 1
        self.assertGreater(style_dropped, 0)

        self.assertEqual(
            sorted(item["pr_url"] for item in first),
            sorted(row["pr_url_to_review"] for row in rows),
        )
        for item in first:
            self.assertGreaterEqual(len(item["issues"]), 1)
            for issue in item["issues"]:
                path = issue["file_path"]
                self.assertIsInstance(path, str)
                self.assertTrue(path.strip())
                rule = issue.get("rule_name")
                self.assertTrue(rule is None or str(rule).strip().upper() in {"", "NONE"})
                if isinstance(rule, str):
                    self.assertIsNone(_STYLE.search(rule))
                self.assertGreaterEqual(issue["start_line"], 1)
                self.assertGreaterEqual(issue["end_line"], issue["start_line"])

    def test_prompt_is_prefix_plus_diff_and_omits_the_key(self) -> None:
        rows = load_rows(default_jsonl())
        selected = select_subset(rows)
        stored = json.loads(default_subset_path().read_text(encoding="utf-8"))
        self.assertEqual(
            [item["pr_url"] for item in selected],
            [item["pr_url"] for item in stored],
        )
        for live, saved in zip(selected, stored):
            self.assertEqual(live["issues"], saved["issues"])

        prefix = load_prefix()
        chosen = None
        for item in stored:
            prompt = assemble_prompt(item["diff"])
            hidden = True
            for issue in item["issues"]:
                if (
                    issue["title"] in prompt
                    or issue["description"] in prompt
                    or issue["problematic_code_snippet"] in prompt
                ):
                    hidden = False
                    break
            if hidden:
                chosen = item
                break
        self.assertIsNotNone(chosen)
        assert chosen is not None
        prompt = assemble_prompt(chosen["diff"])
        self.assertTrue(prompt.startswith(prefix))
        self.assertGreater(len(prompt), len(prefix))
        self.assertEqual(prompt[len(prefix) :], chosen["diff"])
        self.assertNotIn(chosen["diff"], prefix)
        for issue in chosen["issues"]:
            self.assertNotIn(issue["title"], prompt)
            self.assertNotIn(issue["description"], prompt)
            self.assertNotIn(issue["problematic_code_snippet"], prompt)
            self.assertIn(issue, chosen["issues"])
            self.assertTrue(issue["title"])
            self.assertTrue(issue["description"])
            self.assertTrue(issue["problematic_code_snippet"])


if __name__ == "__main__":
    unittest.main()
