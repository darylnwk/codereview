"""Cached reviewer prefix.

The prefix is one literal: rubric plus findings JSON shape. It has no per-call
data. Callers concatenate the diff after it. Do not format the prefix; it
contains JSON braces and must stay byte-identical across calls.
"""

from __future__ import annotations

# One literal. A cache breakpoint can sit on these bytes later.
PREFIX = """Review the diff that follows. Return findings for defects that break behavior or safety. Return JSON only.

Report a defect that breaks behavior or safety: incorrect logic, missed edge cases, broken error handling, races, resource leaks, and auth or security holes.
- incorrect logic: a condition, calculation, or control path produces the wrong result
- missed edge cases: empty input, a missing value, a boundary, a missing branch, or a zero or negative amount is handled wrongly
- broken error handling: an error is ignored, a failure is reported as success, or the caller continues after a failed step
- races: shared state is read and written without the ordering the program assumes
- resource leaks: a handle, connection, lock, or buffer is taken and not released on every path, including failure
- auth or security holes: a check can be skipped, a secret or token is compared or logged unsafely, or untrusted input reaches a sink that runs or reveals it

Do not report style, formatting, naming, or docs-only notes. A note that does not change what the program does, or who can reach it, is not a finding.

Do not invent a finding or a target defect count when nothing breaks. Return no finding in that case.

Each finding carries file, line, what breaks, and a one-line fix. Use exactly these JSON keys and no others:
- file: the path as written in the diff
- line: the 1-based line in that file where the break is visible. The line must be present in the diff. If you cannot point at such a line, do not emit the finding.
- what_breaks: what breaks
- fix: the one-line fix, with no line break inside it

Return one JSON object. Its only key is findings. findings is an array of those objects. One finding per break.
When nothing breaks, the findings list is empty. Return {"findings":[]}

Do not wrap the JSON in a code fence. Do not add text before or after the object.

Diff:
"""


def load_prefix() -> str:
    """Return the rubric prefix. Every call returns the same bytes."""
    return PREFIX


def assemble_prompt(diff: str) -> str:
    """Place the diff strictly after the prefix.

    The prefix is a leading substring. The diff is the exact suffix.
    """
    if not isinstance(diff, str):
        raise TypeError("diff must be a string")
    if diff == "":
        raise ValueError("diff must be non-empty")
    return load_prefix() + diff
