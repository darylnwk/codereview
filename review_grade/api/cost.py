"""Project the review price from a diff. No network and no clock."""

from __future__ import annotations

from review_grade.api.gate import changed_line_count

# Hundred-thousandths of a dollar. One changed line is $0.00002.
_UNITS_PER_LINE = 2
_UNITS_PER_DOLLAR = 100_000
# $0.15. A projection strictly above this is refused.
_CAP_UNITS = 15_000


def project_cost(diff: str) -> float:
    return _units(diff) / _UNITS_PER_DOLLAR


def over_cap(diff: str) -> bool:
    return _units(diff) > _CAP_UNITS


def _units(diff: str) -> int:
    return changed_line_count(diff) * _UNITS_PER_LINE
