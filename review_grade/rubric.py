"""Cached reviewer prefix.

The prefix is four literals joined in order: class list, silence, break
names, and the findings JSON shape. It has no per-call data. Callers
concatenate the diff after it. Do not format the prefix; it contains JSON
braces and must stay byte-identical across calls.
"""

from __future__ import annotations

# Class list. One definition line per class. No planted-shape checklist.
CLASS_LIST = """Review the diff that follows. Return findings for defects that break behavior or safety. Return JSON only.

Report a defect that breaks behavior or safety: incorrect logic, missed edge cases, broken error handling, races, resource leaks, and auth or security holes.
- incorrect logic: a condition, calculation, or path produces the wrong result.
- missed edge cases: empty input, a boundary including the equal case.
- broken error handling: a failure is no longer caught, cleaned up, or reset.
- races: shared state is read and written without the order the program assumes.
- resource leaks: a resource is acquired and not released.
- auth or security holes: a check can be skipped, or untrusted input reaches a sink.

"""

# When to stay silent, and the style refusal.
SILENCE = """Do not report style, formatting, naming, or docs-only notes.

Do not invent a finding or a target defect count when nothing breaks. No finding when nothing breaks.

Stay silent when behavior is unchanged or when you are not sure. The removed line is the old behavior. Do not assume the added line is a fix. When the diff shows the old behavior, the new behavior, and the wrong result, each change is its own finding.

A floor that became round, or a round that became floor, is its own finding. Report both. A changed call, condition, type, order, import, return, field, or cleanup is a finding even when the edit looks intentional. Cite the identifier and the unchanged line a type leaves wrong.

"""

# Named breaks, then shapes of a wrong result. No planted-defect titles.
BREAK_NAMES = """A removed error path, a skipped check, and a wrong branch are findings.

A call that is not awaited is a finding. A floor, ceil, or round that changes the result is a finding. A stale or wrong cache or field is a finding. A changed condition, calculation, type, call, or returned value is its own finding when the result is wrong.

Cite each on its own changed line, not only the last, and not the line above it or below it. Each is a finding. A call that dropped an argument, option, or field. An operator that lost or gained an equal sign, or a condition that flipped. A number that became text, or text that became a number. An awaited call whose result is not returned. Steps that swapped order, a flush inside a loop, or a cleanup or init that moved. An import or require path whose text changed, or a removed import still used, on that import line. A value placed in an extra object. A fallback the old line had and the new line dropped, or a trim the new line added. A memory leak or a race condition. A counter compared with >= to its maximum. A check that treats zero or empty as missing. A handler that clears a kept value. A container created inside the function. A test whose timeout, wait, assertion, setup, or cleanup changed. A wrong identifier where the old code named another.

"""

# Line rule, counting example, and the findings object. Ends at the diff.
FINDINGS_SHAPE = """Set line from the hunk header. Use the number after the +, never the number after the -. A line that starts with + or a space advances the number. A line that starts with - does not. Put line on the line whose text contains the identifier you quote, not the line before it and not a blank line. Do not report a line this diff does not show.

Example of counting only, not a finding: @@ -4,3 +8,3 @@ so ` function save(row) {` is line 8 and `+  await write(row);` is line 10.

Ignore stories, snapshots, package manifests, lockfiles, and config. Review tests.

Each finding carries file, line, what breaks, and a one-line fix. Keys: file, line, what_breaks, fix.

Return one JSON object whose only key is findings, one finding per break. When nothing breaks, return {"findings":[]}

Read every hunk.

Cite the changed line, not the blank line above it and not a later line. Quote the identifier on that line in what_breaks. Do not stop after the first finding in a hunk. Do not skip a change because the new behavior looks helpful.

Quote the changed operator, call, field, type, or path on its own line. A dropped argument is the call that lost it. A moved step is the line that now runs too early or too late. A removed fallback is the line that no longer supplies the old value. A renamed path is the import that still names the old path. A test finding cites the changed wait, assertion, or cleanup. Do not cite the next statement.

Diff:
"""

PREFIX = CLASS_LIST + SILENCE + BREAK_NAMES + FINDINGS_SHAPE


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
