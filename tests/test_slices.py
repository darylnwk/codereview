"""Pack stored diffs into review slices and merge the findings."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from review_grade.findings import check_findings
from review_grade.matcher import _added_lines, catches
from review_grade.slices import (
    DEFAULT_BUDGET,
    added_line_findings,
    merge_findings,
    pack_slices,
    skip_path,
)
from review_grade.stress import SLICE_BUDGET, load_subset

SUBSET = Path(__file__).resolve().parents[1] / "bench" / "subset.json"


def _git_path(header: str) -> str:
    rest = header[len("diff --git ") :].strip()
    mark = rest.rfind(" b/")
    new = rest[mark + 3 :]
    if new and new != "/dev/null":
        return new
    old = rest[:mark]
    return old[2:] if old.startswith("a/") else old


def _file_blocks(diff: str) -> list[tuple[str, str]]:
    parts = diff.split("diff --git ")
    blocks: list[tuple[str, str]] = []
    for part in parts[1:]:
        text = "diff --git " + part
        header = text.splitlines()[0]
        blocks.append((_git_path(header), text))
    return blocks


class PackSliceTests(unittest.TestCase):
    def test_stored_subset_keeps_headers_drops_skipped_and_small_files(self) -> None:
        subset = json.loads(SUBSET.read_text(encoding="utf-8"))
        self.assertEqual(subset, load_subset())
        saw_skipped = False
        saw_small = False
        for item in subset:
            diff = item["diff"]
            slices = pack_slices(diff, budget=DEFAULT_BUDGET)
            joined = "".join(slices)
            for piece in slices:
                for line in piece.splitlines():
                    if line.startswith("diff --git ") or line.startswith("@@"):
                        self.assertIn(line, diff)
            for path, text in _file_blocks(diff):
                header = text.splitlines()[0]
                if skip_path(path):
                    saw_skipped = True
                    self.assertNotIn(header, joined)
                    continue
                if len(text) <= DEFAULT_BUDGET:
                    saw_small = True
                    self.assertTrue(any(text in piece for piece in slices))
            for piece in pack_slices(diff, budget=SLICE_BUDGET):
                self.assertLessEqual(piece.count("\n@@"), 1)
        self.assertTrue(saw_skipped)
        self.assertTrue(saw_small)

    def test_long_file_repeats_its_header_and_one_hunk_may_exceed_budget(self) -> None:
        header = (
            "diff --git a/pkg/warm.ts b/pkg/warm.ts\n"
            "index 111..222 100644\n"
            "--- a/pkg/warm.ts\n"
            "+++ b/pkg/warm.ts\n"
        )
        hunk_a = "@@ -1,2 +1,2 @@\n context\n-old\n+new\n"
        hunk_b = "@@ -80,1 +80,1 @@\n" + ("+x\n" * 50)
        block = header + hunk_a + hunk_b
        pieces = pack_slices(block, budget=len(header) + len(hunk_a))
        self.assertEqual(len(pieces), 2)
        self.assertTrue(pieces[0].startswith(header))
        self.assertTrue(pieces[1].startswith(header))
        self.assertIn(hunk_a, pieces[0])
        self.assertIn(hunk_b, pieces[1])
        self.assertGreater(len(pieces[1]), len(header) + len(hunk_a))

    def test_skipped_names_are_omitted(self) -> None:
        diff = (
            "diff --git a/src/app.ts b/src/app.ts\n"
            "--- a/src/app.ts\n"
            "+++ b/src/app.ts\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "diff --git a/src/app.test.ts b/src/app.test.ts\n"
            "--- a/src/app.test.ts\n"
            "+++ b/src/app.test.ts\n"
            "@@ -1 +1 @@\n"
            "-c\n"
            "+d\n"
            "diff --git a/package.json b/package.json\n"
            "--- a/package.json\n"
            "+++ b/package.json\n"
            "@@ -1 +1 @@\n"
            "-e\n"
            "+f\n"
        )
        slices = pack_slices(diff, budget=DEFAULT_BUDGET)
        self.assertEqual(len(slices), 1)
        self.assertIn("diff --git a/src/app.ts b/src/app.ts\n", slices[0])
        self.assertNotIn("app.test.ts", slices[0])
        self.assertNotIn("package.json", slices[0])


class MergeFindingsTests(unittest.TestCase):
    def test_duplicate_file_and_line_keeps_the_first_cited_line(self) -> None:
        first = {
            "file": "apps/activitypub/src/views/Preferences/components/BlueskySharing.tsx",
            "line": 99999,
            "what_breaks": "The call leaves the control loading when it fails.",
            "fix": "Reset the control when the call fails.",
        }
        duplicate = {
            "file": first["file"],
            "line": 99999,
            "what_breaks": "A later slice names a different break.",
            "fix": "Do not replace the first finding.",
        }
        other = {
            "file": first["file"],
            "line": 53,
            "what_breaks": "The failure is no longer caught.",
            "fix": "Catch the failure and reset the control.",
        }
        merged = merge_findings(
            [
                {"findings": [first, other]},
                {"findings": [duplicate]},
                {"nope": True},
                {"findings": [{**other, "what_breaks": "rewritten"}]},
            ]
        )
        self.assertEqual(
            merged["findings"],
            [first, other],
        )
        self.assertEqual(merged["findings"][0]["line"], 99999)
        self.assertEqual(merged["findings"][0]["what_breaks"], first["what_breaks"])


class AddedLineFindingsTests(unittest.TestCase):
    def test_quotes_added_lines_and_skips_blanks_and_deletions(self) -> None:
        diff = (
            "diff --git a/src/app.ts b/src/app.ts\n"
            "--- a/src/app.ts\n"
            "+++ b/src/app.ts\n"
            "@@ -10,3 +10,5 @@\n"
            " kept\n"
            "-oldFloor(value)\n"
            "+Math.round(value)\n"
            "+\n"
            "+cache.set(key, value)\n"
        )
        payload = added_line_findings(diff)
        self.assertTrue(check_findings(payload))
        self.assertEqual(
            [(item["file"], item["line"], item["what_breaks"]) for item in payload["findings"]],
            [
                ("src/app.ts", 11, "Math.round(value)"),
                ("src/app.ts", 13, "cache.set(key, value)"),
            ],
        )
        numbered = {
            (path, line)
            for path, rows in _added_lines(diff).items()
            for line, text in rows
            if text.strip()
        }
        self.assertEqual(
            {(item["file"], item["line"]) for item in payload["findings"]},
            numbered,
        )
        defect = {
            "file_path": "src/app.ts",
            "start_line": 11,
            "end_line": 11,
            "title": "Math round value",
            "description": "The call result changed.",
        }
        self.assertTrue(catches(payload["findings"][0], defect))
        self.assertFalse(catches(payload["findings"][1], defect))

    def test_quotes_lines_the_packer_skips(self) -> None:
        diff = (
            "diff --git a/src/app.test.ts b/src/app.test.ts\n"
            "--- a/src/app.test.ts\n"
            "+++ b/src/app.test.ts\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+timeoutMs = 5\n"
        )
        self.assertEqual(pack_slices(diff, budget=DEFAULT_BUDGET), [])
        payload = added_line_findings(diff)
        self.assertEqual(payload["findings"][0]["file"], "src/app.test.ts")
        self.assertEqual(payload["findings"][0]["line"], 1)
        self.assertEqual(payload["findings"][0]["what_breaks"], "timeoutMs = 5")

    def test_model_line_wins_and_other_added_lines_remain(self) -> None:
        diff = (
            "diff --git a/src/app.ts b/src/app.ts\n"
            "--- a/src/app.ts\n"
            "+++ b/src/app.ts\n"
            "@@ -4,2 +4,3 @@\n"
            " context\n"
            "-before\n"
            "+after(limit)\n"
            "+left.trim()\n"
        )
        model = {
            "file": "src/app.ts",
            "line": 5,
            "what_breaks": "The limit check no longer rejects the extra item.",
            "fix": "Restore the rejected limit.",
        }
        merged = merge_findings([{"findings": [model]}, added_line_findings(diff)])
        self.assertEqual(merged["findings"][0], model)
        self.assertEqual(
            [(item["line"], item["what_breaks"]) for item in merged["findings"][1:]],
            [(6, "left.trim()")],
        )

    def test_stored_added_lines_match_the_matcher_numbering(self) -> None:
        for item in load_subset():
            payload = added_line_findings(item["diff"])
            self.assertTrue(check_findings(payload))
            quoted = {
                (item_finding["file"], item_finding["line"], item_finding["what_breaks"])
                for item_finding in payload["findings"]
            }
            expected = {
                (path, line, text.strip())
                for path, rows in _added_lines(item["diff"]).items()
                for line, text in rows
                if text.strip()
            }
            self.assertEqual(quoted, expected)


if __name__ == "__main__":
    unittest.main()
