"""Sonnet rerun: shipped prompt, matcher, and claude-sonnet-5-5 setting."""

from __future__ import annotations

import json
import unittest

from review_grade.matcher import catches, line_in_span, score_findings
from review_grade.reviewer import (
    SONNET_MODEL,
    CredentialError,
    ProviderError,
    claude_command,
    parse_claude_stdout,
)
from review_grade.rubric import assemble_prompt, load_prefix
from review_grade.stress import diagnose, format_diagnosis, load_subset


def _finding(issue: dict, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "file": issue["file_path"],
        "line": issue["start_line"],
        "what_breaks": issue["title"],
        "fix": "Apply a one-line correction.",
    }
    item.update(overrides)
    return item


def _invalidation_defect() -> dict:
    for item in load_subset():
        for issue in item["issues"]:
            if (
                str(issue["file_path"]).endswith("use-activity-pub-queries.ts")
                and issue["start_line"] == 2820
                and issue["end_line"] == 2834
            ):
                return issue
    raise AssertionError("stored subset is missing the Ghost pull 10 invalidation defect")


class SonnetRerunTests(unittest.TestCase):
    def test_prompt_matcher_and_sonnet_model(self) -> None:
        subset = load_subset()
        item = subset[0]
        planted = item["issues"][0]
        prompt = assemble_prompt(item["diff"])
        prefix = load_prefix()
        self.assertTrue(prompt.startswith(prefix))
        self.assertEqual(prompt[len(prefix) :], item["diff"])
        self.assertNotIn(planted["title"], prompt)
        self.assertNotIn(planted["description"], prompt)

        named = _finding(planted)
        self.assertTrue(line_in_span(int(named["line"]), planted))
        hit = score_findings([named], [planted])
        self.assertEqual(hit.caught, 1)
        self.assertEqual(hit.planted, 1)
        self.assertEqual(hit.recall, hit.caught / hit.planted)

        defect = _invalidation_defect()
        scene = {
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
        self.assertTrue(line_in_span(scene["line"], defect))
        self.assertFalse(catches(scene, defect))
        missed = score_findings([scene], [defect])
        self.assertEqual(missed.caught, 0)

        empty = score_findings([], [planted])
        self.assertEqual(empty.caught, 0)
        self.assertEqual(empty.planted, 1)

        self.assertEqual(SONNET_MODEL, "claude-sonnet-5-5")
        command = claude_command()
        self.assertEqual(command[command.index("--model") + 1], SONNET_MODEL)
        self.assertEqual(command[command.index("--tools") + 1], "")

    def test_diagnosis_labels_follow_the_caught_count(self) -> None:
        self.assertEqual(diagnose(51, "ship"), "model issue")
        self.assertEqual(diagnose(42, "human decision"), "model issue")
        self.assertEqual(diagnose(28, "stop"), "both")
        self.assertEqual(diagnose(27, "stop"), "rubric can be improved")
        self.assertEqual(diagnose(22, "stop"), "rubric can be improved")
        blocked = format_diagnosis(provider_error="Failed to authenticate")
        self.assertIn("grok_planted=60\n", blocked)
        self.assertIn("grok_caught=22\n", blocked)
        self.assertIn("grok_recall=0.36666666666666664\n", blocked)
        self.assertIn("grok_verdict=stop\n", blocked)
        self.assertIn("grok_model=grok-4.7-build-fast\n", blocked)
        self.assertTrue(blocked.endswith("provider_error\n"))
        self.assertNotIn("model issue", blocked)
        self.assertNotIn("rubric can be improved", blocked)
        self.assertNotIn("conclusion=both", blocked)
        self.assertNotIn("sonnet_caught", blocked)
        self.assertNotIn("sonnet_recall", blocked)

    def test_auth_envelope_is_a_credential_error(self) -> None:
        raw = json.dumps(
            {
                "is_error": True,
                "result": (
                    "Failed to authenticate: OAuth session expired "
                    "and could not be refreshed"
                ),
                "type": "result",
            }
        )
        with self.assertRaises(CredentialError) as caught:
            parse_claude_stdout(raw)
        self.assertIn("Failed to authenticate", str(caught.exception))

    def test_weekly_limit_is_a_credential_error(self) -> None:
        raw = json.dumps(
            {
                "is_error": True,
                "result": (
                    "You've hit your weekly limit · resets Oct 11 at 6pm "
                    "(Asia/Singapore)"
                ),
            }
        )
        with self.assertRaises(CredentialError) as caught:
            parse_claude_stdout(raw)
        self.assertIn("weekly limit", str(caught.exception))

    def test_other_model_error_stays_a_provider_error(self) -> None:
        raw = json.dumps({"is_error": True, "result": "model overloaded"})
        with self.assertRaises(ProviderError) as caught:
            parse_claude_stdout(raw)
        self.assertNotIsInstance(caught.exception, CredentialError)

    def test_structured_output_is_the_findings_object(self) -> None:
        raw = json.dumps(
            {
                "is_error": False,
                "type": "result",
                "result": '{"findings":[]}',
                "structured_output": {"findings": []},
            }
        )
        self.assertEqual(parse_claude_stdout(raw), {"findings": []})


if __name__ == "__main__":
    unittest.main()
