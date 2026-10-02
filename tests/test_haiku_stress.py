"""Haiku stress path: shipped prompt, Claude argv, parser, and report text."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_grade.matcher import kill_gate, score_findings
from review_grade.reviewer import (
    HAIKU_MODEL,
    REVIEWER_MODEL,
    SONNET_MODEL,
    CredentialError,
    claude_command,
    parse_claude_stdout,
    review_diff,
    review_diff_claude,
)
from review_grade.rubric import assemble_prompt, load_prefix
from review_grade.stress import (
    claude_model_for,
    format_provider_error,
    format_report,
    format_summary,
    load_subset,
    main,
    run_stress,
)
from review_grade.subset import planted_count


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> object:
    return type(
        "Proc",
        (),
        {"returncode": returncode, "stdout": stdout, "stderr": stderr},
    )()


def _findings_stdout(findings: list[dict] | None = None) -> str:
    body = {"findings": [] if findings is None else findings}
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": json.dumps(body),
            "structured_output": body,
        }
    )


def _report_fields(report: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_excerpt = False
    for line in report.splitlines():
        if line.startswith("prompt_excerpt<<"):
            in_excerpt = True
            continue
        if in_excerpt:
            if line == ">>":
                in_excerpt = False
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    return fields


class HaikuStressTests(unittest.TestCase):
    def test_prompt_argv_parser_and_model_ids(self) -> None:
        subset = load_subset()
        item = subset[0]
        planted = item["issues"][0]
        prompt = assemble_prompt(item["diff"])
        prefix = load_prefix()
        self.assertTrue(prompt.startswith(prefix))
        self.assertEqual(prompt[len(prefix) :], item["diff"])
        self.assertNotIn(planted["title"], prompt)
        self.assertNotIn(planted["description"], prompt)

        self.assertEqual(HAIKU_MODEL, "claude-haiku-4-5-20251001")
        self.assertEqual(claude_model_for("haiku"), HAIKU_MODEL)
        argv = claude_command(claude_model_for("haiku"))
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-4-5-20251001")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        sonnet_argv = claude_command()
        self.assertEqual(sonnet_argv[sonnet_argv.index("--model") + 1], SONNET_MODEL)
        self.assertEqual(
            [part if part != SONNET_MODEL else HAIKU_MODEL for part in sonnet_argv],
            argv,
        )

        self.assertEqual(REVIEWER_MODEL, "grok-4.7-build-fast")
        self.assertEqual(SONNET_MODEL, "claude-sonnet-5-5")
        self.assertIsNone(claude_model_for("grok"))
        self.assertEqual(claude_model_for("sonnet"), "claude-sonnet-5-5")
        self.assertEqual(review_diff.__defaults__, None)
        self.assertEqual(
            review_diff.__kwdefaults__["model"],
            "grok-4.7-build-fast",
        )
        self.assertEqual(
            review_diff_claude.__kwdefaults__["model"],
            "claude-sonnet-5-5",
        )

        raw = json.dumps(
            {
                "is_error": True,
                "result": (
                    "Failed to authenticate: OAuth session expired "
                    "and could not be refreshed"
                ),
            }
        )
        with self.assertRaises(CredentialError) as caught:
            parse_claude_stdout(raw)
        self.assertIn("Failed to authenticate", str(caught.exception))

        finding = {
            "file": "apps/web/page.tsx",
            "line": 12,
            "what_breaks": "navigation throws before the page renders",
            "fix": "Import the router the page already calls.",
        }
        body = {"findings": [finding]}
        parsed = parse_claude_stdout(
            json.dumps(
                {
                    "is_error": False,
                    "result": "not the findings object",
                    "structured_output": body,
                }
            )
        )
        self.assertEqual(parsed, body)
        self.assertNotIn("is_error", parsed)

        blocked = format_provider_error("claude cli is not installed")
        self.assertEqual(blocked, "provider_error=claude cli is not installed\n")
        self.assertNotIn("recall=", blocked)
        self.assertNotIn("caught=", blocked)
        self.assertNotIn("planted=", blocked)

        summary = format_summary(subset)
        self.assertIn(f"pull_requests={len(subset)}\n", summary)
        self.assertIn(f"planted={planted_count(subset)}\n", summary)
        for row in subset:
            for issue in row["issues"]:
                if issue["title"]:
                    self.assertNotIn(issue["title"], summary)
                if issue["description"]:
                    self.assertNotIn(issue["description"], summary)

    def test_empty_and_invalid_findings_stay_in_planted(self) -> None:
        subset = load_subset()
        issues = subset[0]["issues"]
        for payload in ({"findings": []}, {"nope": True}, "4 of 6", None):
            scored = score_findings(payload, issues)
            self.assertEqual(scored.caught, 0)
            self.assertEqual(scored.planted, len(issues))

        result = run_stress(subset, lambda prompt: {"findings": []})
        self.assertEqual(result["planted"], planted_count(subset))
        self.assertEqual(result["caught"], 0)
        self.assertEqual(result["recall"], result["caught"] / result["planted"])
        self.assertEqual(result["verdict"], kill_gate(result["recall"], result["passes"]))
        self.assertIn(result["passes"], (1, 2))
        self.assertNotEqual(result["verdict"], "second_pass")
        report = format_report(subset, result, model=HAIKU_MODEL)
        self.assertIn(f"model={HAIKU_MODEL}\n", report)
        self.assertIn("key_title_absent=yes\n", report)
        self.assertIn("key_description_absent=yes\n", report)
        self.assertTrue(report.split("prompt_excerpt<<\n", 1)[1].startswith(load_prefix()))

    def test_haiku_main_scores_the_stored_subset(self) -> None:
        subset = load_subset()
        seen: list[list[str]] = []
        prompts: list[str] = []

        def fake_run(argv: list[str], **kwargs: object) -> object:
            seen.append(list(argv))
            prompts.append(str(kwargs.get("input")))
            return _proc(_findings_stdout())

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            summary = Path(tmp) / "summary.txt"
            with mock.patch("review_grade.reviewer.subprocess.run", fake_run):
                code = main(
                    ["--reviewer", "haiku", "--out", str(out), "--summary", str(summary)]
                )
            report = out.read_text(encoding="utf-8")
            summary_text = summary.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertEqual(len(seen), len(subset) * result_passes(report))
        for argv in seen:
            self.assertEqual(argv[0], "claude")
            self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-4-5-20251001")
            self.assertEqual(argv[argv.index("--tools") + 1], "")
        prefix = load_prefix()
        for index, prompt in enumerate(prompts):
            item = subset[index % len(subset)]
            self.assertTrue(prompt.startswith(prefix))
            self.assertEqual(prompt[len(prefix) :], item["diff"])
            for issue in item["issues"]:
                self.assertNotIn(issue["title"], prompt)
                self.assertNotIn(issue["description"], prompt)
        fields = _report_fields(report)
        planted = int(fields["planted"])
        caught = int(fields["caught"])
        recall = ast.literal_eval(fields["recall"])
        passes = int(fields["passes"])
        self.assertEqual(planted, planted_count(subset))
        self.assertEqual(caught, 0)
        self.assertEqual(recall, caught / planted)
        self.assertEqual(fields["verdict"], kill_gate(recall, passes))
        self.assertIn(passes, (1, 2))
        self.assertNotEqual(fields["verdict"], "second_pass")
        self.assertEqual(fields["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(fields["key_title_absent"], "yes")
        self.assertEqual(fields["key_description_absent"], "yes")
        self.assertIn(f"pull_requests={len(subset)}\n", summary_text)
        self.assertIn(f"planted={planted_count(subset)}\n", summary_text)

    def test_provider_failure_does_not_invent_recall(self) -> None:
        def missing_cli(argv: list[str], **kwargs: object) -> object:
            raise FileNotFoundError(argv[0])

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            summary = Path(tmp) / "summary.txt"
            with mock.patch("review_grade.reviewer.subprocess.run", missing_cli):
                code = main(
                    ["--reviewer", "haiku", "--out", str(out), "--summary", str(summary)]
                )
            text = out.read_text(encoding="utf-8")
        self.assertEqual(code, 2)
        self.assertEqual(text, "provider_error=claude cli is not installed\n")
        self.assertNotIn("recall=", text)
        self.assertNotIn("caught=", text)
        self.assertNotIn("planted=", text)

        def auth_fail(argv: list[str], **kwargs: object) -> object:
            return _proc(
                json.dumps(
                    {
                        "is_error": True,
                        "result": "Failed to authenticate: OAuth session expired",
                    }
                ),
                returncode=1,
            )

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            summary = Path(tmp) / "summary.txt"
            with mock.patch("review_grade.reviewer.subprocess.run", auth_fail):
                code = main(
                    ["--reviewer", "haiku", "--out", str(out), "--summary", str(summary)]
                )
            text = out.read_text(encoding="utf-8")
        self.assertEqual(code, 2)
        self.assertTrue(text.startswith("provider_error="))
        self.assertIn("Failed to authenticate", text)
        self.assertNotIn("recall=", text)
        self.assertNotIn("caught=", text)
        self.assertNotIn("planted=", text)

    def test_one_review_error_stays_in_the_planted_count(self) -> None:
        subset = load_subset()
        calls = {"n": 0}

        def flaky(argv: list[str], **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                return _proc(_findings_stdout())
            return _proc("not json", returncode=1, stderr="claude failed")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            summary = Path(tmp) / "summary.txt"
            with mock.patch("review_grade.reviewer.subprocess.run", flaky):
                code = main(
                    ["--reviewer", "haiku", "--out", str(out), "--summary", str(summary)]
                )
            fields = _report_fields(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(int(fields["planted"]), planted_count(subset))
        self.assertEqual(int(fields["caught"]), 0)
        self.assertEqual(int(fields["review_errors"]), calls["n"] - 1)
        self.assertGreater(int(fields["review_errors"]), 0)

    def test_grok_and_sonnet_commands_keep_their_models(self) -> None:
        cases = (
            ("grok", "grok", "-m", "grok-4.7-build-fast"),
            ("sonnet", "claude", "--model", "claude-sonnet-5-5"),
        )
        for reviewer, program, flag, model in cases:
            seen: list[list[str]] = []

            def fake_run(argv: list[str], **kwargs: object) -> object:
                seen.append(list(argv))
                if argv[0] == "claude":
                    return _proc(_findings_stdout())
                return _proc(json.dumps({"structuredOutput": {"findings": []}}))

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out.txt"
                summary = Path(tmp) / "summary.txt"
                with mock.patch("review_grade.reviewer.subprocess.run", fake_run):
                    code = main(
                        [
                            "--reviewer",
                            reviewer,
                            "--out",
                            str(out),
                            "--summary",
                            str(summary),
                        ]
                    )
            self.assertEqual(code, 0, reviewer)
            self.assertGreater(len(seen), 0, reviewer)
            for argv in seen:
                self.assertEqual(argv[0], program, reviewer)
                self.assertEqual(argv[argv.index(flag) + 1], model, reviewer)


def result_passes(report: str) -> int:
    return int(_report_fields(report)["passes"])


if __name__ == "__main__":
    unittest.main()
