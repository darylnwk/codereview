"""Score findings against a planted key. Not a model call.

A defect is caught once, and only when the file matches, the line sits inside
the defect span, and the finding text describes that defect.
"""

from __future__ import annotations

import re
from decimal import Decimal

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


# Published Graphite Diamond bars. Recall is Macroscope, Sep 2025:
# 21/115 self-contained runtime bugs. Precision is the Entelligence
# benchmark figure 0.667. Both comparisons are strict.
GRAPHITE_CAUGHT = 21
GRAPHITE_PLANTED = 115
GRAPHITE_PRECISION = "0.667"


class Score:
    def __init__(
        self,
        planted: int,
        caught: int,
        emitted: int = 0,
        true_findings: int = 0,
    ) -> None:
        if planted < 0 or caught < 0 or emitted < 0 or true_findings < 0:
            raise ValueError("counts must be non-negative")
        if caught > planted:
            raise ValueError("caught cannot exceed planted")
        if true_findings > emitted:
            raise ValueError("true findings cannot exceed emitted")
        self.planted = planted
        self.caught = caught
        self.emitted = emitted
        self.true_findings = true_findings

    @property
    def recall(self) -> float:
        if self.planted == 0:
            raise ValueError("recall is undefined when nothing is planted")
        return self.caught / self.planted

    @property
    def precision(self) -> float:
        """True findings over emitted findings. Zero emitted findings are a fail."""
        if self.emitted == 0:
            return 0.0
        return self.true_findings / self.emitted


def recall_beats_graphite(caught: int, planted: int) -> bool:
    """True when caught/planted is strictly above 21/115.

    Cross-multiplication stays exact. A float compare would move the bar.
    """
    if planted <= 0:
        raise ValueError("planted must be greater than zero")
    if caught < 0:
        raise ValueError("counts must be non-negative")
    if caught > planted:
        raise ValueError("caught cannot exceed planted")
    return caught * GRAPHITE_PLANTED > GRAPHITE_CAUGHT * planted


def precision_beats_graphite(true_findings: int, emitted: int) -> bool:
    """True when true_findings/emitted is strictly above 0.667.

    Zero emitted findings fail. Decimal keeps 667/1000 on the bar.
    """
    if emitted < 0 or true_findings < 0:
        raise ValueError("counts must be non-negative")
    if true_findings > emitted:
        raise ValueError("true findings cannot exceed emitted")
    if emitted == 0:
        return False
    return Decimal(true_findings) / Decimal(emitted) > Decimal(GRAPHITE_PRECISION)


def beats_graphite(
    *,
    caught: int,
    planted: int,
    true_findings: int,
    emitted: int,
) -> bool:
    """Whether these counts beat Graphite on both bars.

    Recall is caught/planted. Precision is true findings over emitted
    findings. A finding counts once. Zero emitted findings fail.
    """
    return recall_beats_graphite(caught, planted) and precision_beats_graphite(
        true_findings, emitted
    )


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
    """True when what_breaks names this defect.

    Title words count only in what_breaks. A fix can repeat the scene
    (Bluesky, confirmation) without describing the break. Description words
    count only together with a title word, so cache/confirm/account overlap
    does not match a different defect in the same span.
    """
    what = content_tokens(str(finding.get("what_breaks") or ""))
    title = content_tokens(str(defect.get("title") or ""))
    if len(title) >= 2 and len(title & what) >= 2:
        return True
    if len(title) == 1 and title <= what:
        return True
    description = content_tokens(str(defect.get("description") or ""))
    return len(title & what) >= 1 and len(description & what) >= 4


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


def accepted_findings(payload: object) -> list[dict]:
    """Findings that passed the shape check. A rejected payload emits none."""
    if not check_findings(payload):
        return []
    return _finding_list(payload)


def score_findings(payload: object, defects: list[dict]) -> Score:
    """Caught defects over this planted list.

    A rejected payload catches nothing and emits nothing. Precision uses
    emitted findings, not caught defects: a finding that catches one or
    more defects counts once.
    """
    planted = len(defects)
    findings = accepted_findings(payload)
    if not findings and not check_findings(payload):
        return Score(planted, 0, 0, 0)
    caught: set[int] = set()
    true_findings = 0
    for finding in findings:
        hit = False
        for index, defect in enumerate(defects):
            if catches(finding, defect):
                caught.add(index)
                hit = True
        if hit:
            true_findings += 1
    return Score(planted, len(caught), len(findings), true_findings)


def diff_new_lines(diff: str) -> dict[str, set[int]]:
    """New-file line numbers present in a unified diff, keyed by path."""
    lines: dict[str, set[int]] = {}
    path: str | None = None
    new_line: int | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = None
            new_line = None
            continue
        if raw.startswith("+++ "):
            name = raw[4:].split("\t", 1)[0].strip()
            if name == "/dev/null":
                path = None
            else:
                if name.startswith("b/"):
                    name = name[2:]
                path = name
                lines.setdefault(path, set())
            new_line = None
            continue
        if raw.startswith("@@"):
            try:
                plus = raw.split("+", 1)[1]
                start = plus.split(" ", 1)[0].split(",", 1)[0]
                new_line = int(start)
            except (IndexError, ValueError):
                new_line = None
            continue
        if path is None or new_line is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+") or raw.startswith(" "):
            lines[path].add(new_line)
            new_line += 1
    return lines


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,}")


def _added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Added new-file lines, in order, keyed by path."""
    files: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    new_line: int | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = None
            new_line = None
            continue
        if raw.startswith("+++ "):
            name = raw[4:].split("\t", 1)[0].strip()
            if name == "/dev/null":
                path = None
            else:
                if name.startswith("b/"):
                    name = name[2:]
                path = name
                files.setdefault(path, [])
            new_line = None
            continue
        if raw.startswith("@@"):
            try:
                plus = raw.split("+", 1)[1]
                start = plus.split(" ", 1)[0].split(",", 1)[0]
                new_line = int(start)
            except (IndexError, ValueError):
                new_line = None
            continue
        if path is None or new_line is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            files[path].append((new_line, raw[1:]))
            new_line += 1
            continue
        if raw.startswith(" "):
            new_line += 1
    return files


def _code_identifiers(text: str) -> list[str]:
    """CamelCase or snake_case names, longest first. Plain words do not count."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in _IDENT.findall(text or ""):
        if raw in seen:
            continue
        has_break = "_" in raw or any(char.isupper() for char in raw[1:])
        if not has_break:
            continue
        seen.add(raw)
        found.append(raw)
    found.sort(key=len, reverse=True)
    return found


def anchor_line(diff: str, file_name: str, line: object, what_breaks: str) -> object:
    """Point a finding at the added line it names.

    A line the diff already shows stays put. A line the diff does not show
    moves only when one added line in that file contains a code name from
    what_breaks and no other added line does.
    """
    if not isinstance(line, int) or isinstance(line, bool):
        return line
    if line_present_in_diff(diff, file_name, line):
        return line
    rows: list[tuple[int, str]] = []
    for path, added in _added_lines(diff).items():
        if files_match(file_name, path):
            rows.extend(added)
    for ident in _code_identifiers(what_breaks):
        hits = [number for number, text in rows if ident in text]
        if len(hits) == 1:
            return hits[0]
    return line


def keep_lines_in_diff(diff: str, payload: object) -> object:
    """Drop findings whose line is not a new-file line in the diff.

    The key is not used. A rejected payload is unchanged.
    """
    findings = accepted_findings(payload)
    if not findings:
        return payload
    kept: list[dict] = []
    changed = False
    for finding in findings:
        line = finding.get("line")
        file_name = finding.get("file")
        if (
            isinstance(file_name, str)
            and isinstance(line, int)
            and not isinstance(line, bool)
            and line_present_in_diff(diff, file_name, line)
        ):
            kept.append(finding)
        else:
            changed = True
    if not changed:
        return payload
    if isinstance(payload, list):
        return kept
    return {"findings": kept}


def anchor_findings(diff: str, payload: object) -> object:
    """Return the payload with absent lines anchored. The key is not used."""
    findings = accepted_findings(payload)
    if not findings:
        return payload
    anchored: list[dict] = []
    changed = False
    for finding in findings:
        new_line = anchor_line(
            diff,
            str(finding.get("file") or ""),
            finding.get("line"),
            str(finding.get("what_breaks") or ""),
        )
        if new_line != finding.get("line"):
            changed = True
            anchored.append({**finding, "line": new_line})
        else:
            anchored.append(finding)
    if not changed:
        return payload
    if isinstance(payload, list):
        return anchored
    if isinstance(payload, dict):
        return {"findings": anchored}
    return payload


def line_present_in_diff(diff: str, file_name: str, line: int) -> bool:
    """True when `line` is a new-file line of `file_name` in this diff."""
    if not isinstance(file_name, str) or isinstance(line, bool) or not isinstance(line, int):
        return False
    for path, numbers in diff_new_lines(diff).items():
        if files_match(file_name, path) and line in numbers:
            return True
    return False


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
