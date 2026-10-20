"""Select the Qodo pull-request subset. Pure: no download and no model call.

Functional bugs in the published JSONL have rule_name null. The string NONE is
accepted too. Style, formatting, naming, and docs rules are not planted.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SUBSET_LIMIT = 100
_BEHAVIOR_RULES = frozenset({"", "NONE"})
_STYLE_RULE = re.compile(
    r"style|format|naming|docs|documentation|docstring",
    re.IGNORECASE,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_jsonl() -> Path:
    return repo_root() / "bench" / "git_code_review_bench_100_w_open_prs.jsonl"


def default_subset_path() -> Path:
    return repo_root() / "bench" / "subset.json"


def load_rows(path: Path | None = None) -> list[dict]:
    source = path or default_jsonl()
    rows = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def is_style_rule(issue: dict) -> bool:
    """True when the rule name is style, formatting, naming, or docs."""
    rule = issue.get("rule_name")
    if not isinstance(rule, str) or not rule.strip():
        return False
    return _STYLE_RULE.search(rule) is not None


def is_behavior_defect(issue: dict) -> bool:
    """True for a functional bug. Null and NONE are that marker in this dataset."""
    rule = issue.get("rule_name")
    if rule is None:
        return True
    if isinstance(rule, str) and rule.strip().upper() in _BEHAVIOR_RULES:
        return True
    return False


def is_planted_defect(issue: dict) -> bool:
    """Behavior defect with a file and a line span. Style rules are out."""
    if is_style_rule(issue) or not is_behavior_defect(issue):
        return False
    path = issue.get("file_path")
    if not isinstance(path, str) or not path.strip():
        return False
    start = issue.get("start_line")
    end = issue.get("end_line")
    if isinstance(start, bool) or isinstance(end, bool):
        return False
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    if start < 1 or end < start:
        return False
    return True


def select_subset(rows: list[dict], *, limit: int = SUBSET_LIMIT) -> list[dict]:
    """Return the same pull requests for the same rows.

    Sort by pull-request URL. Keep a pull request when it has at least one
    planted defect. Stop at `limit` when more qualify.
    """
    chosen = []
    ordered = sorted(
        rows,
        key=lambda row: (row.get("pr_url_to_review") or "", row.get("repo") or ""),
    )
    for row in ordered:
        issues = [dict(issue) for issue in row.get("issues") or [] if is_planted_defect(issue)]
        if not issues:
            continue
        url = row.get("pr_url_to_review") or ""
        if not url:
            continue
        chosen.append({"repo": row.get("repo") or "", "pr_url": url, "issues": issues})
        if len(chosen) >= limit:
            break
    return chosen


def planted_count(subset: list[dict]) -> int:
    return sum(len(item["issues"]) for item in subset)


def materialize(subset: list[dict], diffs: dict[str, str]) -> list[dict]:
    """Attach fetched diffs. A pull request with no diff is left out."""
    materialized = []
    for item in subset:
        diff = diffs.get(item["pr_url"]) or ""
        if diff == "":
            continue
        materialized.append({**item, "diff": diff})
    return materialized


class DiffFetchError(RuntimeError):
    pass


def fetch_diff(pr_url: str) -> str:
    """Download one public pull-request diff. The ground-truth key is not added."""
    parts = pr_url.rstrip("/").split("/")
    if len(parts) < 5 or parts[-2] != "pull":
        raise DiffFetchError(f"not a pull request url: {pr_url}")
    number = parts[-1]
    repo = parts[-3]
    owner = parts[-4]
    proc = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.v3.diff",
            f"/repos/{owner}/{repo}/pulls/{number}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or proc.stdout or "empty diff").strip()
        raise DiffFetchError(detail)
    return proc.stdout


def format_selection(subset: list[dict]) -> str:
    lines = [f"planted={planted_count(subset)}"]
    lines.extend(item["pr_url"] for item in subset)
    return "\n".join(lines) + "\n"


def main() -> None:
    subset = select_subset(load_rows())
    print(format_selection(subset), end="")


if __name__ == "__main__":
    main()
