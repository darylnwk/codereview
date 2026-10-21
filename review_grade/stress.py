"""Run the reviewer on the stored subset and apply the kill gate.

A second pass runs only when the gate asks for one. Recall is caught/planted
on this subset. It is not a score on a user diff.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from review_grade.matcher import (
    Score,
    accepted_findings,
    beats_graphite,
    kill_gate,
    line_present_in_diff,
    score_findings,
)
from review_grade.reviewer import (
    HAIKU_MODEL,
    SONNET_MODEL,
    CredentialError,
    ProviderError,
    review_diff,
    review_diff_claude,
)
from review_grade.rubric import assemble_prompt, load_prefix
from review_grade.slices import (
    DEFAULT_BUDGET,
    added_line_findings,
    merge_findings,
    pack_slices,
)
from review_grade.subset import default_subset_path, load_rows, planted_count, select_subset

# One hunk per call. A budget of 1 keeps the first hunk with its header and
# starts a new slice at every later hunk. Larger budgets bury the changed line.
SLICE_BUDGET = 1

# Completed Grok stress run. Do not launch another Grok run to recreate it.
GROK_BASELINE_PLANTED = 60
GROK_BASELINE_CAUGHT = 22
GROK_BASELINE_RECALL = 0.36666666666666664
GROK_BASELINE_VERDICT = "stop"
GROK_BASELINE_MODEL = "grok-4.7-build-fast"


def load_subset(path: Path | None = None) -> list[dict]:
    source = path or default_subset_path()
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("subset must be a list")
    return data


def _one_line_fix(fix: object) -> bool:
    return isinstance(fix, str) and fix.strip() != "" and "\n" not in fix and "\r" not in fix


def run_stress(subset: list[dict], review) -> dict:
    """Review every diff. Pass 2 runs only when pass 1 is not yet ship.

    The reported counts are one pass. A failed review emits nothing and its
    planted defects stay in the denominator.
    """

    def one_pass() -> dict:
        planted = 0
        caught = 0
        emitted = 0
        true_findings = 0
        counted: list[dict] = []
        for item in subset:
            diff = item.get("diff") or ""
            if diff == "":
                raise ValueError(f"missing diff for {item.get('pr_url')}")
            payload = review(assemble_prompt(diff))
            part = score_findings(payload, item["issues"])
            print(
                f"scored {item.get('pr_url')} caught={part.caught}/{part.planted} "
                f"emitted={part.emitted} true={part.true_findings}",
                flush=True,
            )
            planted += part.planted
            caught += part.caught
            emitted += part.emitted
            true_findings += part.true_findings
            for finding in accepted_findings(payload):
                counted.append(
                    {
                        "file": finding.get("file"),
                        "line": finding.get("line"),
                        "what_breaks": finding.get("what_breaks"),
                        "fix": finding.get("fix"),
                        "in_diff": line_present_in_diff(
                            diff, str(finding.get("file") or ""), finding.get("line")
                        ),
                    }
                )
        score = Score(planted, caught, emitted, true_findings)
        return {
            "planted": score.planted,
            "caught": score.caught,
            "emitted": score.emitted,
            "true_findings": score.true_findings,
            "recall": score.recall,
            "precision": score.precision,
            "counted_findings": counted,
        }

    first = one_pass()
    passes = 1
    final = first
    label = kill_gate(final["recall"], passes)
    if label == "second_pass":
        final = one_pass()
        passes = 2
        label = kill_gate(final["recall"], passes)
    if label == "second_pass":
        raise RuntimeError("kill gate asked for a third pass")
    return {
        "planted": final["planted"],
        "caught": final["caught"],
        "emitted": final["emitted"],
        "true_findings": final["true_findings"],
        "recall": final["recall"],
        "precision": final["precision"],
        "counted_findings": final["counted_findings"],
        "verdict": label,
        "passes": passes,
    }


def format_summary(subset: list[dict]) -> str:
    repos: list[str] = []
    for item in subset:
        repo = item.get("repo") or ""
        if repo and repo not in repos:
            repos.append(repo)
    return (
        f"pull_requests={len(subset)}\n"
        f"planted={planted_count(subset)}\n"
        f"repositories={', '.join(repos)}\n"
    )


def diagnose(caught: int, verdict: str) -> str:
    """One label from the Sonnet caught count and verdict. Not a model call."""
    if verdict in ("ship", "human decision"):
        return "model issue"
    if verdict != "stop":
        raise ValueError(f"unexpected verdict {verdict}")
    if caught >= GROK_BASELINE_CAUGHT + 6:
        return "both"
    return "rubric can be improved"


def format_diagnosis(*, sonnet: dict | None = None, provider_error: str | None = None) -> str:
    """Grok baseline plus either the Sonnet result and one label, or provider_error."""
    lines = [
        f"grok_planted={GROK_BASELINE_PLANTED}",
        f"grok_caught={GROK_BASELINE_CAUGHT}",
        f"grok_recall={GROK_BASELINE_RECALL!r}",
        f"grok_verdict={GROK_BASELINE_VERDICT}",
        f"grok_model={GROK_BASELINE_MODEL}",
    ]
    if provider_error is not None:
        lines.append("provider_error")
        return "\n".join(lines) + "\n"
    if sonnet is None:
        raise ValueError("sonnet result or provider error required")
    if sonnet["recall"] != sonnet["caught"] / sonnet["planted"]:
        raise ValueError("sonnet recall is not caught/planted")
    label = diagnose(int(sonnet["caught"]), str(sonnet["verdict"]))
    lines.extend(
        [
            f"sonnet_planted={sonnet['planted']}",
            f"sonnet_caught={sonnet['caught']}",
            f"sonnet_recall={sonnet['recall']!r}",
            f"sonnet_verdict={sonnet['verdict']}",
            f"sonnet_model={SONNET_MODEL}",
            f"conclusion={label}",
        ]
    )
    return "\n".join(lines) + "\n"


def format_report(subset: list[dict], result: dict, model: str | None = None) -> str:
    planted = result["planted"]
    caught = result["caught"]
    emitted = result["emitted"]
    true_findings = result["true_findings"]
    recall = result["recall"]
    if planted <= 0:
        raise ValueError("planted must be greater than zero")
    if planted != planted_count(subset):
        raise ValueError("planted dropped a review")
    if recall != caught / planted:
        raise ValueError("recall is not caught/planted")
    if true_findings > emitted:
        raise ValueError("true findings cannot exceed emitted")
    if emitted == 0:
        if true_findings != 0:
            raise ValueError("true findings cannot exceed emitted")
        precision_text = "undefined"
    else:
        precision = true_findings / emitted
        if result["precision"] != precision:
            raise ValueError("precision is not true findings over emitted")
        precision_text = repr(precision)
    counted = result["counted_findings"]
    if len(counted) != emitted:
        raise ValueError("counted findings do not match emitted")
    one_line_fixes = 0
    lines_in_diff = 0
    rendered: list[str] = []
    for finding in counted:
        fix = finding.get("fix")
        if _one_line_fix(fix):
            one_line_fixes += 1
        if finding.get("in_diff") is True:
            lines_in_diff += 1
        rendered.append(
            json.dumps(
                {
                    "file": finding.get("file"),
                    "line": finding.get("line"),
                    "what_breaks": finding.get("what_breaks"),
                    "fix": fix,
                    "one_line_fix": _one_line_fix(fix),
                    "in_diff": finding.get("in_diff") is True,
                },
                separators=(",", ":"),
            )
        )
    beat = beats_graphite(
        caught=caught,
        planted=planted,
        true_findings=true_findings,
        emitted=emitted,
    )
    item = subset[0]
    prompt = assemble_prompt(item["diff"])
    prefix = load_prefix()
    if not prompt.startswith(prefix) or prompt[len(prefix) :] != item["diff"]:
        raise ValueError("prompt is not the prefix plus the diff")
    diff_head = "\n".join(item["diff"].splitlines()[:20])
    excerpt = f"{prefix}{diff_head}"
    titles = [
        str(issue.get("title") or "")
        for row in subset
        for issue in row.get("issues") or []
    ]
    descriptions = [
        str(issue.get("description") or "")
        for row in subset
        for issue in row.get("issues") or []
    ]
    title_absent = all(not title or title not in excerpt for title in titles)
    description_absent = all(
        not description or description not in excerpt for description in descriptions
    )
    body = (
        f"planted={planted}\n"
        f"caught={caught}\n"
        f"emitted={emitted}\n"
        f"true_findings={true_findings}\n"
        f"recall={recall!r}\n"
        f"precision={precision_text}\n"
        f"one_line_fixes={one_line_fixes}\n"
        f"lines_in_diff={lines_in_diff}\n"
        f"misses_kept=yes\n"
        f"verdict={result['verdict']}\n"
        f"passes={result['passes']}\n"
        + (f"model={model}\n" if model else "")
        + (
            f"beats_graphite={'yes' if beat else 'no'}\n"
            f"key_title_absent={'yes' if title_absent else 'no'}\n"
            f"key_description_absent={'yes' if description_absent else 'no'}\n"
            "prompt_excerpt<<\n"
            f"{excerpt}\n"
            ">>\n"
            "counted_findings<<\n"
            + "".join(line + "\n" for line in rendered)
            + ">>\n"
        )
    )
    return body


def format_provider_error(message: str) -> str:
    return f"provider_error={message.strip()}\n"


def claude_model_for(reviewer: str) -> str | None:
    """Claude model id for a stress reviewer. Grok stays on the default reviewer."""
    if reviewer == "haiku":
        return HAIKU_MODEL
    if reviewer == "sonnet":
        return SONNET_MODEL
    if reviewer == "grok":
        return None
    raise ValueError(f"unknown reviewer {reviewer}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--subset", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--reviewer", choices=("grok", "sonnet", "haiku"), default="grok")
    parser.add_argument("--diagnosis", type=Path, default=None)
    args = parser.parse_args(argv)

    rows = load_rows()
    selected = select_subset(rows)
    again = select_subset(rows)
    if [item["pr_url"] for item in selected] != [item["pr_url"] for item in again]:
        raise SystemExit("subset selection was not stable")
    subset = load_subset(args.subset)
    if [item["pr_url"] for item in selected] != [item["pr_url"] for item in subset]:
        raise SystemExit("stored subset does not match the selector")

    state = {"ok": 0, "err": 0, "last": ""}
    model = claude_model_for(args.reviewer)
    cache: list[dict] = []
    if args.cache is not None and args.cache.exists():
        cache = json.loads(args.cache.read_text(encoding="utf-8"))
    cursor = {"n": 0}

    def remember(entry: dict) -> None:
        cache.append(entry)
        if args.cache is not None:
            args.cache.write_text(json.dumps(cache), encoding="utf-8")

    def review(prompt: str) -> object:
        index = cursor["n"]
        cursor["n"] += 1
        url = subset[index % len(subset)]["pr_url"]
        if index < len(cache):
            entry = cache[index]
            print(f"cached {index + 1} {url}", flush=True)
            if entry.get("ok"):
                state["ok"] += 1
                return entry["payload"]
            state["err"] += 1
            state["last"] = str(entry.get("error") or "reviewer failed")
            return {"findings": []}
        prefix = load_prefix()
        diff = prompt[len(prefix) :] if prompt.startswith(prefix) else prompt
        pieces = pack_slices(diff, budget=SLICE_BUDGET)
        print(f"review {index + 1} {url} slices={len(pieces)}", flush=True)
        payloads: list[object] = []
        any_ok = False
        for piece_index, piece in enumerate(pieces, start=1):
            print(f"slice {piece_index}/{len(pieces)} {url}", flush=True)
            try:
                if model is None:
                    part = review_diff(assemble_prompt(piece))
                else:
                    part = review_diff_claude(assemble_prompt(piece), model=model)
            except CredentialError:
                raise
            except ProviderError as exc:
                state["err"] += 1
                state["last"] = str(exc)
                print(f"error {url} {exc}", flush=True)
                continue
            any_ok = True
            payloads.append(part)
        if not any_ok and pieces:
            remember({"ok": False, "error": state["last"] or "reviewer failed"})
            return {"findings": []}
        payload = merge_findings([*payloads, added_line_findings(diff)])
        state["ok"] += 1
        remember({"ok": True, "payload": payload})
        return payload

    def write_provider_failure(message: str) -> None:
        args.out.write_text(format_provider_error(message), encoding="utf-8")
        if args.diagnosis is not None and args.reviewer == "sonnet":
            args.diagnosis.write_text(
                format_diagnosis(provider_error=message),
                encoding="utf-8",
            )

    args.summary.write_text(format_summary(subset), encoding="utf-8")
    try:
        result = run_stress(subset, review)
    except CredentialError as exc:
        write_provider_failure(str(exc))
        return 2
    if state["ok"] == 0:
        write_provider_failure(state["last"] or "reviewer failed")
        return 2
    text = format_report(subset, result, model=model)
    if state["err"]:
        text += f"review_errors={state['err']}\n"
    args.out.write_text(text, encoding="utf-8")
    if args.diagnosis is not None and args.reviewer == "sonnet":
        args.diagnosis.write_text(format_diagnosis(sonnet=result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
