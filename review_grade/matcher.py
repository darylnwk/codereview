"""Score findings against a planted key. Not a model call.

A defect is caught once, and only when the file matches, the line sits inside
the defect span, and the finding text describes that defect.
"""

from __future__ import annotations

import re

from review_grade.findings import check_findings

_STOP = frozenset(
    """
    a an the of to in on for and or with when that this is are was were be been
    being by from as it its at if not no do does did can will into than then so
    but we they their them our your you without within which who what where how
    has have had may might must should would could also only more other such
    there here over under after before between through during both each any all
    these those using used use via per about onto because while
    """.split()
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class Score:
    def __init__(self, planted: int, caught: int) -> None:
        if planted < 0 or caught < 0:
            raise ValueError("counts must be non-negative")
        if caught > planted:
            raise ValueError("caught cannot exceed planted")
        self.planted = planted
        self.caught = caught

    @property
    def recall(self) -> float:
        if self.planted == 0:
            raise ValueError("recall is undefined when nothing is planted")
        return self.caught / self.planted


def content_tokens(text: str) -> set[str]:
    found: set[str] = set()
    for raw in _WORD.findall(text or ""):
        split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).replace("_", " ")
        for part in split.split():
            token = part.lower()
            if len(token) < 4 or token in _STOP:
                continue
            found.add(token)
    return found


def files_match(finding_file: str, defect_path: str) -> bool:
    left = finding_file.replace("\\", "/").strip().lstrip("./")
    right = defect_path.replace("\\", "/").strip().lstrip("./")
    if not left or not right:
        return False
    return left == right or left.endswith("/" + right) or right.endswith("/" + left)


def line_in_span(line: int, defect: dict) -> bool:
    return int(defect["start_line"]) <= line <= int(defect["end_line"])


def describes(finding: dict, defect: dict) -> bool:
    """True when the finding text shares the defect's distinctive words."""
    said = content_tokens(f"{finding.get('what_breaks', '')} {finding.get('fix', '')}")
    title = content_tokens(str(defect.get("title") or ""))
    if len(title) >= 2 and len(title & said) >= 2:
        return True
    if len(title) == 1 and title <= said:
        return True
    description = content_tokens(str(defect.get("description") or ""))
    return len(description & said) >= 4


def _finding_list(payload: object) -> list[dict]:
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and "findings" in payload:
        return [item for item in payload["findings"] if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def catches(finding: dict, defect: dict) -> bool:
    file_name = finding.get("file")
    line = finding.get("line")
    if not isinstance(file_name, str) or isinstance(line, bool) or not isinstance(line, int):
        return False
    if not files_match(file_name, str(defect.get("file_path") or "")):
        return False
    if not line_in_span(line, defect):
        return False
    return describes(finding, defect)


def score_findings(payload: object, defects: list[dict]) -> Score:
    """Caught defects over this planted list. A rejected payload catches nothing."""
    planted = len(defects)
    if not check_findings(payload):
        return Score(planted, 0)
    caught: set[int] = set()
    for index, defect in enumerate(defects):
        for finding in _finding_list(payload):
            if catches(finding, defect):
                caught.add(index)
                break
    return Score(planted, len(caught))


def kill_gate(recall: float, passes: int) -> str:
    """Kill-gate label after `passes` finished review passes.

    Under 70% does not stop on the first pass. 85% or better ships.
    From 70% up to but not including 85%, one more pass, then a human decision.
    """
    if passes < 1:
        raise ValueError("passes must be at least 1")
    # Thousandths keep 0.70 and 0.85 on the published side of the cut.
    thousandths = int(round(float(recall) * 1000))
    if thousandths >= 850:
        return "ship"
    if thousandths >= 700:
        return "human decision" if passes >= 2 else "second_pass"
    return "stop" if passes >= 2 else "second_pass"
