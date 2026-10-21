"""Pack a unified diff into review slices and merge their findings.

Slices follow file order. A skipped test or manifest file is omitted. A file
that fits in the budget stays intact. A longer file is split on hunk
boundaries, and each piece repeats that file's header. Merge keeps the first
finding for a file and line and does not rewrite either field.
"""

from __future__ import annotations

import fnmatch

from review_grade.findings import check_findings

DEFAULT_BUDGET = 8000

_SKIP_SEGMENTS = frozenset({"tests", "__tests__", "snapshots"})
_BASENAME_PATTERNS = (
    "*.test.*",
    "*.spec.*",
    "*.stories.*",
    "*.story.*",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig*",
    "*.config.*",
)


def skip_path(path: str) -> bool:
    """True when this diff path is a test, snapshot, manifest, or config file."""
    parts = [part for part in path.split("/") if part and part != "."]
    if any(part in _SKIP_SEGMENTS for part in parts):
        return True
    base = parts[-1] if parts else path
    return any(fnmatch.fnmatch(base, pattern) for pattern in _BASENAME_PATTERNS)


def _new_path(header: str) -> str:
    rest = header[len("diff --git ") :].rstrip("\n")
    mark = rest.rfind(" b/")
    if mark == -1:
        return rest[2:] if rest.startswith("a/") else rest
    new = rest[mark + 3 :]
    old = rest[:mark]
    if old.startswith("a/"):
        old = old[2:]
    if new and new != "/dev/null":
        return new
    return old


def _blocks(diff: str) -> list[tuple[str, str]]:
    if "diff --git " not in diff:
        return []
    lines = diff.splitlines(keepends=True)
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git ") and current:
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)
    blocks: list[tuple[str, str]] = []
    for group in groups:
        text = "".join(group)
        if not text.startswith("diff --git "):
            continue
        blocks.append((_new_path(group[0]), text))
    return blocks


def _split_file(block: str, budget: int) -> list[str]:
    lines = block.splitlines(keepends=True)
    header_end = None
    for index, line in enumerate(lines):
        if line.startswith("+++ "):
            header_end = index + 1
            break
    if header_end is None:
        return [block]
    header = "".join(lines[:header_end])
    hunks: list[str] = []
    current: list[str] = []
    for line in lines[header_end:]:
        if line.startswith("@@") and current:
            hunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        hunks.append("".join(current))
    if not hunks:
        return [block]
    pieces: list[str] = []
    buf = header
    for hunk in hunks:
        if buf != header and len(buf) + len(hunk) > budget:
            pieces.append(buf)
            buf = header + hunk
        else:
            buf += hunk
    if buf != header:
        pieces.append(buf)
    return pieces or [block]


def pack_slices(diff: str, budget: int = DEFAULT_BUDGET) -> list[str]:
    """Return ordered diff slices of at most `budget` characters when a file fits.

    A file longer than the budget is split on hunk boundaries. A single hunk
    may exceed the budget. Skipped paths are omitted. Order is the diff's order.
    """
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise ValueError("budget must be a positive integer")
    kept = [text for path, text in _blocks(diff) if not skip_path(path)]
    slices: list[str] = []
    buf = ""
    for text in kept:
        if len(text) > budget:
            if buf:
                slices.append(buf)
                buf = ""
            slices.extend(_split_file(text, budget))
            continue
        if buf and len(buf) + len(text) > budget:
            slices.append(buf)
            buf = text
        else:
            buf += text
    if buf:
        slices.append(buf)
    return slices


def _items(payload: object) -> list[dict]:
    if not check_findings(payload).accepted:
        return []
    if isinstance(payload, dict) and "findings" in payload:
        return [item for item in payload["findings"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def added_line_findings(diff: str) -> dict:
    """One finding per added line, quoting that line.

    The line is the new-file number. A context line advances it and a
    deleted line does not. Blank added lines are left out. This does not
    read defect titles. Callers that also have model findings merge those
    first so a model line is kept.
    """
    findings: list[dict] = []
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
        if path is None or new_line is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+"):
            text = raw[1:].strip()
            if text:
                findings.append(
                    {
                        "file": path,
                        "line": new_line,
                        "what_breaks": text,
                        "fix": "Restore the previous behavior.",
                    }
                )
            new_line += 1
            continue
        if raw.startswith(" "):
            new_line += 1
    return {"findings": findings}


def merge_findings(payloads: list[object]) -> dict:
    """Concatenate accepted findings. A later same file and line is dropped.

    The kept finding is the first one. `line` and `what_breaks` are not rewritten.
    """
    merged: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for payload in payloads:
        for item in _items(payload):
            file_name = item.get("file")
            line = item.get("line")
            if not isinstance(file_name, str) or isinstance(line, bool) or not isinstance(line, int):
                continue
            key = (file_name, line)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return {"findings": merged}
