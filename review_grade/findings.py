"""Validate reviewer findings JSON. Nothing else.

Accepts one finding, a list of findings, or {"findings": [...]}, including an
empty list. Rejects a count or a recall fraction, and any other shape.
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple


class FindingsCheck(NamedTuple):
    accepted: bool
    reason: str

    def __bool__(self) -> bool:
        return self.accepted


# Keys every finding carries. The rubric names these and no others.
REQUIRED_KEYS = ("file", "line", "what_breaks", "fix")

_RECALL_FRACTION = re.compile(r"^\s*\d+\s+of\s+\d+\s*$", re.IGNORECASE)
_COUNT = re.compile(r"^\s*\d+\s*$")


def check_findings(payload: object) -> FindingsCheck:
    """Return accepted=True only for the findings shape."""
    try:
        _parse(payload)
    except ValueError as exc:
        return FindingsCheck(False, str(exc))
    return FindingsCheck(True, "ok")


def _parse(payload: object) -> None:
    if isinstance(payload, str):
        _reject_score_text(payload)
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError("not findings json") from None
        if isinstance(decoded, str):
            _reject_score_text(decoded)
            raise ValueError("not findings json")
        payload = decoded

    if isinstance(payload, bool) or isinstance(payload, (int, float)):
        raise ValueError("count")

    if isinstance(payload, list):
        _require_findings(payload)
        return

    if isinstance(payload, dict):
        if "findings" in payload:
            if set(payload) != {"findings"}:
                raise ValueError("extra fields")
            findings = payload["findings"]
            if isinstance(findings, str):
                _reject_score_text(findings)
                raise ValueError("findings is not a list")
            if isinstance(findings, bool) or isinstance(findings, (int, float)):
                raise ValueError("count")
            if not isinstance(findings, list):
                raise ValueError("findings is not a list")
            _require_findings(findings)
            return
        _require_finding(payload)
        return

    raise ValueError("not findings")


def _reject_score_text(text: str) -> None:
    if _RECALL_FRACTION.match(text):
        raise ValueError("recall fraction")
    if _COUNT.match(text):
        raise ValueError("count")


def _require_findings(items: list[object]) -> None:
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("finding is not an object")
        _require_finding(item)


def _require_finding(item: dict[object, object]) -> None:
    if set(item) != set(REQUIRED_KEYS):
        raise ValueError("finding fields")
    file_path = item["file"]
    if (
        not isinstance(file_path, str)
        or file_path.strip() == ""
        or "\n" in file_path
        or "\r" in file_path
    ):
        raise ValueError("file")
    line = item["line"]
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ValueError("line")
    what = item["what_breaks"]
    if not isinstance(what, str) or what.strip() == "":
        raise ValueError("what_breaks")
    fix = item["fix"]
    if not isinstance(fix, str) or fix.strip() == "" or "\n" in fix or "\r" in fix:
        raise ValueError("fix")
