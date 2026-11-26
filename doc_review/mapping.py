"""Pure position mapping logic for Unicode code-point ranges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class RangeValidationError(ValueError):
    """Raised when submitted ranges are empty, invalid, or overlap."""


@dataclass(frozen=True)
class Replacement:
    """A half-open replacement interval ``[start, end)``."""

    start: int
    end: int
    replacement: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise RangeValidationError("range positions must be integers")
        if self.start < 0 or self.end < 0:
            raise RangeValidationError("range positions must be non-negative")
        if self.start > self.end:
            raise RangeValidationError("range start must not be greater than end")

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def delta(self) -> int:
        return len(self.replacement) - self.length


def assert_disjoint(
    ranges: Iterable[tuple[int, int] | Replacement],
    *,
    require_nonempty: bool,
) -> None:
    """Validate ranges and reject overlaps.

    Half-open ranges that merely touch at one coordinate are disjoint.
    """

    normalized: list[tuple[int, int]] = []
    for value in ranges:
        if isinstance(value, Replacement):
            start, end = value.start, value.end
        else:
            start, end = value
        if start < 0 or end < 0 or start > end:
            raise RangeValidationError(f"invalid range [{start}, {end})")
        if require_nonempty and start == end:
            raise RangeValidationError(f"suggestion range [{start}, {end}) is empty")
        normalized.append((start, end))

    normalized.sort()
    previous_end: int | None = None
    previous_start: int | None = None
    for start, end in normalized:
        if previous_end is not None and start < previous_end:
            raise RangeValidationError(
                f"ranges [{previous_start}, {previous_end}) and [{start}, {end}) overlap"
            )
        previous_start, previous_end = start, end


def apply_replacements(content: str, ops: Sequence[Replacement]) -> str:
    """Apply disjoint replacement operations expressed in one coordinate system."""

    assert_disjoint(ops, require_nonempty=False)
    result = content
    for op in sorted(ops, key=lambda item: item.start, reverse=True):
        if op.start > len(result) or op.end > len(result):
            # Operations are validated against their own revision before this.
            raise RangeValidationError("replacement range is outside the document")
        result = result[: op.start] + op.replacement + result[op.end :]
    return result


def _map_start_boundary(pos: int, ops: Sequence[Replacement]) -> int:
    """Map a range start.

    An insertion exactly at the start is treated as preceding the range, so the
    boundary moves to after that insertion.
    """

    shift = 0
    for op in sorted(ops, key=lambda item: (item.start, item.end)):
        delta = op.delta
        if op.end < pos:
            shift += delta
            continue
        if op.end == pos:
            # Includes a zero-length insertion at pos: right affinity.
            return op.end + shift + delta
        if op.start <= pos:
            # pos is inside a non-empty replacement, or at its start. The
            # useful review span begins at the replacement's beginning.
            return op.start + shift
        # The operation starts after this boundary.
        return pos + shift
    return pos + shift


def _map_end_boundary(pos: int, ops: Sequence[Replacement]) -> int:
    """Map a range end.

    An insertion exactly at the end is treated as following the range, so the
    boundary remains before that insertion.
    """

    shift = 0
    for op in sorted(ops, key=lambda item: (item.start, item.end)):
        delta = op.delta
        if op.end < pos:
            shift += delta
            continue
        if op.end == pos:
            if op.start == op.end:
                # Zero-length insertion at the end: left affinity.
                return op.start + shift
            # Replacement/deletion ends exactly at the boundary.
            return op.end + shift + delta
        if op.start < pos < op.end:
            # The boundary lies inside a replacement/deletion. Include the whole
            # replacement as the best current review interval.
            return op.end + shift + delta
        # op.start >= pos: operation begins at or after the end and does not
        # intersect the half-open range.
        return pos + shift
    return pos + shift


def map_range_through_revision(
    start: int,
    end: int,
    ops: Sequence[Replacement],
    *,
    revision: int,
    revision_source: str,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Map one anchored range through one revision's replacement operations.

    Returns the best current review interval and a list of conflict records.
    Existing conflicts are deliberately not forgotten by callers: history, not
    present-day text equality, determines whether an anchor was damaged.
    """

    ordered = sorted(ops, key=lambda item: (item.start, item.end))
    conflicts: list[dict[str, Any]] = []

    for ordinal, op in enumerate(ordered):
        # Boundary cases implement the requested affinity rules:
        # op.end <= start: before the original range, including insertion at start.
        # op.start >= end: after it, including insertion at end.
        # Anything else is an internal insertion or an intersecting edit.
        if op.end <= start or op.start >= end:
            continue

        if op.start == op.end:
            kind = "internal_insertion"
            message = (
                f"revision {revision} inserted text strictly inside "
                f"[{start}, {end}); the original anchor is no longer intact"
            )
        else:
            kind = "intersecting_edit"
            message = (
                f"revision {revision} changed/replaced [{op.start}, {op.end}), "
                f"which intersects anchored range [{start}, {end})"
            )

        conflicts.append(
            {
                "code": kind,
                "message": message,
                "revision": revision,
                "revision_source": revision_source,
                "op_index": ordinal,
                "op_range": {"start": op.start, "end": op.end},
                "op_replacement": op.replacement,
                "anchored_range_at_that_revision": {"start": start, "end": end},
            }
        )

    new_start = _map_start_boundary(start, ordered)
    new_end = _map_end_boundary(end, ordered)
    if new_end < new_start:  # defensive; boundary math should prevent this
        new_end = new_start
    return new_start, new_end, conflicts
